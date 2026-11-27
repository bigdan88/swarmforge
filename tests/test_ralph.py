"""Ralph mode and the work log: one agent in fresh passes, code-decided keep/throw-away, and memory
that carries from one attempt to the next."""
import json
import re
import subprocess

import pytest

from swarmforge import ralph, worklog
from swarmforge.db import Store
from swarmforge.orchestrator import Orchestrator
from swarmforge.testing import TestEvidence

from .conftest import ISSUE, FakeProvider, tool_call

pytestmark = pytest.mark.slow

PLAN = "# Plan\n\n- [{a}] Add a `describe()` helper to Item\n- [{b}] Add a `restock()` helper to Item\n"
DESCRIBE = '''from inventory.models import Item


def describe(item: Item) -> str:
    return f"{item.sku}: {item.name} x{item.quantity}"
'''
DESCRIBE_TEST = '''from inventory.helpers import describe
from inventory.models import Item


def test_describe():
    assert describe(Item("AB-1", "Bolt", 3, 0.5)) == "AB-1: Bolt x3"
'''
RESTOCK = DESCRIBE + '''

def restock(item: Item, amount: int) -> Item:
    return Item(item.sku, item.name, item.quantity + amount, item.unit_price)
'''
RESTOCK_TEST = '''from inventory.helpers import restock
from inventory.models import Item


def test_restock():
    assert restock(Item("AB-1", "Bolt", 3, 0.5), 2).quantity == 5
'''


def _user(body):
    return next(m["content"] for m in body["messages"] if m["role"] == "user")


def _n(body):
    return sum(1 for m in body["messages"] if m["role"] == "assistant")


SEEN: dict[int, str] = {}


def ralph_dev(body):
    """Pass 1 writes the plan and does item 1; pass 2 breaks a passing test (thrown away) and falsely
    ticks item 2; pass 3 reads the log, then does item 2 properly."""
    u, n = _user(body), _n(body)
    p = int(re.search(r"## Ralph pass (\d+)", u).group(1))
    SEEN.setdefault(p, u)
    steps = {
        1: [("write_file", {"path": ".swarmforge/ralph/PLAN.md", "content": PLAN.format(a=" ", b=" ")}),
            ("write_file", {"path": "inventory/helpers.py", "content": DESCRIBE}),
            ("write_file", {"path": "tests/test_helpers.py", "content": DESCRIBE_TEST}),
            ("write_file", {"path": ".swarmforge/ralph/PLAN.md", "content": PLAN.format(a="x", b=" ")}),
            ("write_file", {"path": ".swarmforge/ralph/NOTES.md", "content": "Run pytest -q from the repo root.\n"})],
        2: [("edit_file", {"path": "inventory/models.py", "old_text": "round(self.quantity * self.unit_price, 2)",
                           "new_text": "0"}),
            ("write_file", {"path": ".swarmforge/ralph/PLAN.md", "content": PLAN.format(a="x", b="x")})],
        3: [("read_file", {"path": ".swarmforge/ralph/LOG.md"}),
            ("write_file", {"path": "inventory/helpers.py", "content": RESTOCK}),
            ("write_file", {"path": "tests/test_restock.py", "content": RESTOCK_TEST}),
            ("write_file", {"path": ".swarmforge/ralph/PLAN.md", "content": PLAN.format(a="x", b="x")})],
    }[p]
    if p == 3 and n == 1:
        log = body["messages"][-1]["content"]
        SEEN["log_seen_by_pass_3"] = log
    script = steps + [("finish", {"summary": f"pass {p} done", "tests_run": "pytest"})]
    name, args = script[min(n, len(script) - 1)]
    return {"content": "", "tool_calls": [tool_call(name, args, f"c{p}-{n}")]}


async def test_ralph_mode_keeps_good_passes_throws_away_bad_ones_and_finishes(cfg, sample_repo, browser_slot):
    cfg.knowledge.enabled = False
    SEEN.clear()
    store = Store(cfg.db_path)
    fake = FakeProvider({"deepseek-fake": ralph_dev})
    with browser_slot():
        orch = Orchestrator(cfg, store, mode="ralph", echo=None, llm_transport=fake.transport)
        run_id = await orch.execute(issue_file=str(ISSUE), repo=str(sample_repo))
    run = store.get_run(run_id)
    assert run["state"] == "COMPLETED", run["state_reason"]
    assert {r["model"] for r in fake.requests} == {"deepseek-fake"}  # one agent, nothing else

    summary = json.loads(store.kv_get("ralph_summary:" + run_id))
    assert summary == {"passes": 3, "kept": 2, "thrown_away": 1, "stopped": "every item in the plan is done", "open_items": 0}
    # Every pass started fresh: the first prompt only had the task; the third saw why pass 2 was thrown away.
    assert "There is no PLAN.md yet" in SEEN[1] and "1 open and 1 done" in SEEN[2]
    assert "Pass 2: thrown away" in SEEN["log_seen_by_pass_3"] and "test_models" in SEEN["log_seen_by_pass_3"]

    repo = orch.ws.repo
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", orch.ws.integration_branch], cwd=repo,
                           capture_output=True, text=True, check=True).stdout.split()
    assert "inventory/helpers.py" in files and "tests/test_restock.py" in files
    assert not any(f.startswith(".swarmforge/") for f in files)  # the plan files are never committed
    models = subprocess.run(["git", "show", f"{orch.ws.integration_branch}:inventory/models.py"], cwd=repo,
                            capture_output=True, text=True, check=True).stdout
    assert "round(self.quantity * self.unit_price, 2)" in models  # the bad pass left no trace

    states = {t["task_key"]: t["state"] for t in store.tasks(run_id)}
    assert states == {"pass1": "INTEGRATED", "pass2": "FAILED", "pass3": "INTEGRATED"}
    assert [t["label"] for t in store.test_runs(run_id)][-1] == "final"
    assert (orch.artifacts / "ralph-LOG.md").read_text().count("## Pass") == 3

    entry = worklog.entries(store, worklog.lineage(store, run_id))[-1]
    assert entry["run_id"] == run_id and entry["ralph"]["kept"] == 2
    assert "## Ralph loop" in (orch.run_dir / "report.md").read_text()


def test_acceptable_keeps_green_or_no_worse_and_rejects_new_failures():
    def ev(cases, exit_code=1, timed_out=False):
        passed = sum(v == "passed" for v in cases.values())
        failed = sum(v == "failed" for v in cases.values())
        return TestEvidence(label="x", command="pytest", exit_code=exit_code, timed_out=timed_out, duration_s=1,
                            passed=passed, failed=failed, junit_found=True, cases=cases)
    before = ev({"a": "passed", "b": "failed"})
    assert ralph.acceptable(ev({"a": "passed", "b": "passed"}, 0), before)[0]
    assert ralph.acceptable(ev({"a": "passed", "b": "failed", "c": "passed"}), before)[0]
    ok, why = ralph.acceptable(ev({"a": "failed", "b": "failed"}), before)
    assert not ok and "a" in why
    assert not ralph.acceptable(ev({"a": "passed"}, 0, timed_out=True), None)[0]
    assert not ralph.acceptable(ev({"a": "failed"}), None)[0]
    assert ralph.open_items("- [ ] one\n- [x] two\n  * [ ] three") == 2


def _run(store, rid, **fields):
    store.create_run(rid, mode="swarm", issue_url="x", run_dir="", budget_usd=1, max_runtime_s=60, **fields)


def test_work_log_follows_every_attempt_and_keeps_the_owners_reason(tmp_path):
    store = Store(tmp_path / "h.db")
    _run(store, "r1")
    store.update_run("r1", state="FAILED", state_reason="repair limit reached", finished_at=1.0,
                     plan_json=json.dumps({"summary": "Add exporters in one module"}))
    store.upsert_task("r1", "T1", title="Write exporters", state="FAILED", error="tests failed")
    store.record_finding("r1", cycle=0, source="reviewer", severity="major", file="a.py", title="Writes outside the folder")
    store._exec("INSERT INTO failures(run_id,created_at,category,root_cause,fix,retry) VALUES (?,?,?,?,?,1)",
                ("r1", 1.0, "test_failure", "CSV header order is wrong", "Sort the fields like the spec"))
    worklog.record(store, "r1")
    _run(store, "r2", heal_of="r1", heal_attempt=1)
    store.update_run("r2", state="COMPLETED", state_reason="draft PR created", finished_at=2.0)
    worklog.owner_said(store, "r2", "The button is in the wrong place; put it top right.")
    _run(store, "r3", retry_of="r2")

    assert worklog.lineage(store, "r3") == worklog.lineage(store, "r1") == "run:r1"
    text = worklog.for_run(store, "r3")
    assert text.index("Attempt 1") < text.index("Attempt 2")
    assert "CSV header order is wrong" in text and "Sort the fields like the spec" in text
    assert "Writes outside the folder" in text and "Write exporters (failed: tests failed)" in text
    assert "declined by the owner" in text and "put it top right" in text
    assert worklog.for_run(store, "r1").count("### Attempt") == 1  # a run never sees itself
    # Long logs keep the newest attempts in full and shorten the oldest.
    for i in range(4, 30):
        _run(store, f"r{i}", retry_of=f"r{i - 1}")
        store.update_run(f"r{i}", state="FAILED", state_reason="x " * 300, finished_at=float(i))
        worklog.record(store, f"r{i}")
    short = worklog.for_run(store, "r29", limit=4000)
    assert len(short) <= 4000 and "### Attempt" in short


def test_project_tasks_share_one_log_across_queue_items(tmp_path):
    store = Store(tmp_path / "h.db")
    for qid, rid in ((1, "a"), (2, "b")):
        _run(store, rid)
        store._exec("INSERT INTO queue_items(position,title,body,repo,created_at,status,run_id,project_id,milestone,project_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", (qid, "t", "b", "o/r", 0, "failed", rid, "p1", 0, "search"))
    store.update_run("a", state="FAILED", state_reason="first try failed", finished_at=1.0)
    worklog.record(store, "a")
    assert worklog.lineage(store, "b") == "project:p1:0:search"
    assert "first try failed" in worklog.for_run(store, "b")


def test_declining_with_a_reason_keeps_it_and_queues_a_follow_up(tmp_path, cfg):
    import uuid

    from swarmforge.runqueue import DECLINE_FOLLOW_UP_KEY, follow_up_after_decline
    from swarmforge.services.dashboard import DashboardActions

    store = Store(cfg.db_path)
    _run(store, "r1")
    store.update_run("r1", state="APPROVED")
    qid = store.queue_add("Export CSV", "Add CSV export", "o/r", run_id="r1", status="running", budget=2.0, mode="ralph")
    aid = store.request_approval("r1", "Push branch?")
    actions = DashboardActions(store, cfg)
    bad = actions.execute(str(uuid.uuid4()), "decide_approval", str(aid), {"approved": True, "follow_up": True})
    assert not bad["ok"]
    result = actions.execute(str(uuid.uuid4()), "decide_approval", str(aid),
                             {"approved": False, "reason": "Columns are in the wrong order.", "follow_up": True})
    assert result["ok"] and "follow-up" in result["message"]
    assert store.approval(aid)["status"] != "pending"
    entry = worklog.entries(store, "run:r1")[-1]
    assert entry["owner"] == "Columns are in the wrong order." and entry["outcome"] == "declined by the owner"
    assert store.kv_get(DECLINE_FOLLOW_UP_KEY + "r1") == "Columns are in the wrong order."

    store.update_run("r1", state="COMPLETED", state_reason="push declined")
    new = follow_up_after_decline(store, cfg, store.queue_item(qid), "r1")
    item = store.queue_item(new)
    assert item["status"] == "queued" and item["mode"] == "ralph" and item["retry_of"] == "r1"
    assert "## The owner declined the previous attempt\nColumns are in the wrong order." in item["body"]
    assert store.kv_get(DECLINE_FOLLOW_UP_KEY + "r1") is None
    assert follow_up_after_decline(store, cfg, store.queue_item(qid), "r1") is None  # only once


def test_a_project_can_switch_its_build_tasks_to_ralph_mode(tmp_path, cfg):
    import uuid

    from swarmforge.runqueue import project_workflow
    from swarmforge.services.dashboard import DashboardActions

    store = Store(cfg.db_path)
    store._exec("INSERT INTO projects(id,name,repo,status,definition,spec,spec_path,budget_usd,created_at,conversation_id) "
                "VALUES ('p1','P','o/r','running','{}','spec','SPEC.md',10,0,'c1')")
    actions = DashboardActions(store, cfg)
    assert project_workflow(store, "p1") == "team"
    r = actions.execute(str(uuid.uuid4()), "set_project_workflow", "p1", {"workflow": "ralph"})
    assert r["ok"] and "Ralph" in r["message"] and project_workflow(store, "p1") == "ralph"
    assert not actions.execute(str(uuid.uuid4()), "set_project_workflow", "p1", {"workflow": "solo"})["ok"]
    actions.execute(str(uuid.uuid4()), "set_project_workflow", "p1", {"workflow": "team"})
    assert project_workflow(store, "p1") == "team"
