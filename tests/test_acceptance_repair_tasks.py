"""Acceptance tests: reviewer blocking findings become mandatory repair tasks.

Covers the issue "Convert reviewer [major] findings into mandatory repair
sub-tasks":

- AC1: each blocking ([major]/[critical]) finding becomes a discrete developer
  task, created deterministically (pure helper, no LLM), titled with the
  finding summary and described with the exact reviewer text plus file/line.
- AC2: the generated task's acceptance criteria quote the reviewer's exact text.
- AC3: repair tasks have depends_on == [], are included in the next cycle's
  executed task set, and a cycle cannot end while blocking findings remain
  (deterministic gate rejects them).
- AC4: finding->task conversion is covered here and in tests/test_state.py.
- AC5: single-agent mode (no review) produces no repair tasks.
"""
import asyncio
import subprocess
from dataclasses import asdict

from swarmforge.orchestrator import Orchestrator
from swarmforge.plan import ID_RE
from swarmforge.state import repair_tasks_from_findings
from swarmforge.testing import TestEvidence

BLOCKING = {"critical", "major"}


def make_finding(i, severity="major", file="src/app.py", line=None):
    line = line if line is not None else 100 + i
    return {
        "severity": severity,
        "file": file,
        "line": line,
        "title": f"[{severity}] finding {i}",
        "description": f"exact reviewer text number {i}: budget_usd is NULL crashes here",
        "recommendation": f"fix suggestion {i}",
    }


# ------------------------------------------------------------------ AC1 / AC2
def test_each_blocking_finding_becomes_one_discrete_task():
    findings = [
        make_finding(1, severity="major"),
        make_finding(2, severity="minor"),
        make_finding(3, severity="critical", file="src/b.py"),
        make_finding(4, severity="info"),
        make_finding(5, severity="major", file="src/c.py"),
    ]
    tasks = repair_tasks_from_findings(findings, ["dev"], cycle=1, severities=BLOCKING)
    # exactly one task per blocking finding, in order, non-blocking ones dropped
    assert len(tasks) == 3
    assert [t.role for t in tasks] == ["dev"] * 3
    assert [t.title for t in tasks] == [
        "[major] finding 1", "[critical] finding 3", "[major] finding 5"]
    for t in tasks:
        assert ID_RE.match(t.id)


def test_task_description_contains_exact_text_and_file_line():
    findings = [make_finding(1), make_finding(2, file="other.py", line=42)]
    tasks = repair_tasks_from_findings(findings, ["dev"], cycle=1, severities=BLOCKING)
    assert len(tasks) == 2
    for n, t in enumerate(tasks, start=1):
        assert findings[n - 1]["description"] in t.description
    assert "src/app.py:101" in tasks[0].description
    assert "other.py:42" in tasks[1].description
    # the dev agent is told it cannot skip the task
    assert "MANDATORY" in tasks[0].description
    assert "cannot be skipped" in tasks[0].description


def test_conversion_is_deterministic_pure_function():
    """AC1 'not by the LLM planner': same findings -> byte-identical tasks."""
    findings = [make_finding(i) for i in (1, 2, 3)]
    a = repair_tasks_from_findings(findings, ["backend", "frontend"], cycle=2,
                                   severities=BLOCKING)
    b = repair_tasks_from_findings(findings, ["backend", "frontend"], cycle=2,
                                   severities=BLOCKING)
    assert [asdict(t) for t in a] == [asdict(t) for t in b]
    assert [t.id for t in a] == ["R2F1", "R2F2", "R2F3"]
    assert [t.role for t in a] == ["backend", "frontend", "backend"]


def test_acceptance_criteria_quote_reviewer_text():
    """AC2: the criterion contains the reviewer's exact finding text in quotes."""
    findings = [make_finding(7)]
    tasks = repair_tasks_from_findings(findings, ["dev"], cycle=1, severities=BLOCKING)
    assert len(tasks) == 1
    criterion = tasks[0].acceptance_criteria[0]
    assert findings[0]["description"] in criterion
    assert '"' in criterion and "Reviewer said" in criterion
    assert findings[0]["title"] in criterion


# --------------------------------------------------------------------- AC3
def test_repair_tasks_have_no_dependencies():
    findings = [make_finding(i) for i in range(1, 4)]
    tasks = repair_tasks_from_findings(findings, ["dev"], cycle=1, severities=BLOCKING)
    assert len(tasks) == 3
    assert all(t.depends_on == [] for t in tasks)


def _evidence():
    return TestEvidence(label="final", command="pytest", exit_code=0, timed_out=False,
                        duration_s=1.0, passed=10, junit_found=True)


def _orch_with_run(cfg):
    from swarmforge.db import Store

    store = Store(cfg.db_path)
    orch = Orchestrator(cfg, store, echo=None)
    orch.run_id = "r-gate"
    store.create_run("r-gate", budget_usd=5, max_runtime_s=300, max_repair_cycles=2)
    return orch, store


def test_gate_blocks_cycle_while_blocking_findings_remain(cfg):
    """AC3: a cycle cannot end (approve) while [major]/[critical] findings are open."""
    orch, _ = _orch_with_run(cfg)
    review = {"verdict": "request_changes", "summary": "nope",
              "findings": [make_finding(1), make_finding(2, severity="critical")]}
    problems = orch._gate(0, _evidence(), None, review, [])
    assert len(problems) == 2
    assert all(p["kind"] == "review" for p in problems)
    assert any("finding 1" in p["summary"] for p in problems)
    # a review with only non-blocking findings lets the cycle end
    clean = {"verdict": "approve", "summary": "ok",
             "findings": [make_finding(3, severity="minor")]}
    assert orch._gate(0, _evidence(), None, clean, []) == []


def test_single_agent_no_review_yields_no_repair_tasks():
    """AC5: review is None in single-agent mode -> the orchestrator expression
    `(review or {}).get('findings') or []` must produce zero repair tasks."""
    review = None
    findings = (review or {}).get("findings") or []
    assert repair_tasks_from_findings(findings, ["dev"], cycle=1,
                                       severities=BLOCKING) == []


# --------------------------------------------------------- AC1/AC3/AC4 pipeline
FINDINGS = [
    {"severity": "critical", "file": "inventory/cli.py", "line": 12,
     "title": "crash on NULL budget",
     "description": "export_run raises TypeError when budget_usd is NULL",
     "recommendation": "coalesce NULL to 0"},
    {"severity": "major", "file": "inventory/exporters.py", "line": 30,
     "title": "unhandled OSError",
     "description": "OSError from a full disk escapes unhandled in export_csv",
     "recommendation": "wrap in ExportError"},
    {"severity": "major", "file": "README.md", "line": None,
     "title": "--format undocumented",
     "description": "README does not explain the --format flag",
     "recommendation": "document --format"},
    {"severity": "minor", "file": "inventory/models.py", "line": 5,
     "title": "nit", "description": "naming could be clearer",
     "recommendation": "rename"},
]


class RejectThenApprove:
    """First review rejects with the findings above; the re-review approves."""

    def __init__(self):
        self.sessions = 0

    def __call__(self, body):
        from . import fake_team

        n = fake_team._n_assistant(body)
        if n == 0:
            self.sessions += 1
            return {"content": "", "tool_calls": [
                fake_team.tool_call("read_file", {"path": "README.md"})]}
        rv = {"verdict": "approve", "summary": "findings addressed", "findings": []}
        if self.sessions == 1:
            rv = {"verdict": "request_changes", "summary": "blocking findings",
                  "findings": FINDINGS}
        return {"content": "", "tool_calls": [fake_team.tool_call("submit_review", rv)]}


async def test_pipeline_repair_tasks_run_and_integrate_before_rereview(cfg, sample_repo):
    """Full offline run: 3 blocking findings -> 3 mandatory tasks (AC1/AC4a);
    they execute and integrate before the cycle-2 re-review (AC3/AC4b); the
    minor finding gets no task."""
    from swarmforge.db import Store

    from . import fake_team
    from .conftest import ISSUE, FakeProvider

    cfg.limits.max_runtime_s = 600
    store = Store(cfg.db_path)
    reviewer = RejectThenApprove()
    fake = FakeProvider({
        "qwen-fake": fake_team.planner,
        "deepseek-fake": fake_team.developer,
        "kimi-fake": fake_team.developer,
        "minimax-fake": fake_team.tester,
        "glm-fake": reviewer,
    })
    orch = Orchestrator(cfg, store, echo=None, llm_transport=fake.transport)
    run_id = await orch.execute(issue_file=str(ISSUE), repo=str(sample_repo))

    run = store.get_run(run_id)
    assert run["state"] == "COMPLETED", run["state_reason"]
    assert reviewer.sessions == 2  # re-reviewed in cycle 2

    tasks = {t["task_key"]: t for t in store.tasks(run_id)}
    blocking = [f for f in FINDINGS if f["severity"] in BLOCKING]
    assert len(blocking) == 3
    for i, f in enumerate(blocking, start=1):
        key = f"R1F{i}"
        assert key in tasks, f"missing repair task {key}"
        t = tasks[key]
        assert t["kind"] == "fix" and t["cycle"] == 1
        assert t["depends_on"] == []
        assert t["title"] == f["title"]
        assert f["description"] in t["description"]
        assert f["description"] in t["acceptance_criteria"][0]
        # AC3: the task was actually executed and integrated, not just registered
        assert t["state"] == "INTEGRATED", t["state"]
        assert t["started_at"] and t["ended_at"]
    assert not any(k.startswith("R1F4") for k in tasks)  # minor finding: no task

    # repair work landed on the integration branch before the approving re-review
    log = (await asyncio.to_thread(
        subprocess.run, ["git", "log", "--oneline", run["integration_branch"]],
        cwd=cfg.workdir / "runs" / run_id / "repo",
        capture_output=True, text=True, check=True)).stdout
    for i in range(1, 4):
        assert f"Integrate R1F{i}" in log

    # deterministic registration is logged by the orchestrator (not the planner)
    assert any("mandatory repair task(s) created from reviewer findings" in e["message"]
               for e in store.events(run_id))
