"""Tests for swarmforge.state.repair_tasks_from_findings."""
import json
from typing import ClassVar

import pytest

from swarmforge.plan import ID_RE
from swarmforge.state import repair_tasks_from_findings


def finding(severity="major", file="src/app.py", line=10, title="crash",
            description="budget_usd is NULL crashes explain_run",
            recommendation="use `or 0`"):
    return {"severity": severity, "file": file, "line": line, "title": title,
            "description": description, "recommendation": recommendation}


def test_three_major_findings_create_three_tasks():
    findings = [
        finding(title=f"finding {i}", description=f"exact text {i}", line=i)
        for i in range(1, 4)
    ]
    tasks = repair_tasks_from_findings(findings, ["dev"], cycle=1)
    assert len(tasks) == 3
    assert [t.id for t in tasks] == ["R1F1", "R1F2", "R1F3"]
    for i, t in enumerate(tasks, start=1):
        assert t.depends_on == []
        assert t.title == f"finding {i}"
        assert f"exact text {i}" in t.description
        assert f"src/app.py:{i}" in t.description
        assert "MANDATORY repair for reviewer finding" in t.description
        assert "cannot be skipped" in t.description
        assert len(t.acceptance_criteria) == 1
        assert f'"exact text {i}"' in t.acceptance_criteria[0]
        assert ID_RE.match(t.id)
        assert t.expected_files == ["src/app.py"]


def test_severity_filtering():
    findings = [
        finding(severity="minor", title="m"),
        finding(severity="info", title="i"),
        finding(severity="critical", title="c"),
        finding(severity="major", title="M"),
    ]
    tasks = repair_tasks_from_findings(findings, ["dev"], cycle=2)
    assert [t.title for t in tasks] == ["c", "M"]
    # custom severities drop criticals
    tasks = repair_tasks_from_findings(findings, ["dev"], cycle=2, severities={"major"})
    assert [t.title for t in tasks] == ["M"]


def test_duplicate_findings_deduplicated():
    findings = [finding(), finding(), finding(title="other")]
    tasks = repair_tasks_from_findings(findings, ["dev"], cycle=1)
    assert len(tasks) == 2


def test_roles_round_robin():
    findings = [finding(title=f"f{i}") for i in range(3)]
    tasks = repair_tasks_from_findings(
        findings, ["developer_backend", "developer_frontend"], cycle=1)
    assert [t.role for t in tasks] == [
        "developer_backend", "developer_frontend", "developer_backend"]


def test_empty_findings_and_empty_roles():
    assert repair_tasks_from_findings([], ["dev"], cycle=1) == []
    assert repair_tasks_from_findings([finding(severity="minor")], [], cycle=1) == []
    with pytest.raises(ValueError):
        repair_tasks_from_findings([finding()], [], cycle=1)


def test_missing_file_and_line():
    f = finding()
    f["file"] = None
    f["line"] = None
    tasks = repair_tasks_from_findings([f], ["dev"], cycle=1)
    assert len(tasks) == 1
    assert "File: unknown" in tasks[0].description
    assert tasks[0].expected_files == []
    f2 = finding(file="a.py", line=None)
    tasks = repair_tasks_from_findings([f2], ["dev"], cycle=1)
    assert "File: a.py" in tasks[0].description


# ------------------------------------------------- full-pipeline wiring tests
MAJOR_FINDINGS = [
    {"severity": "major", "file": "inventory/exporters.py", "line": 395,
     "title": "budget_usd is NULL crash",
     "description": "explain_run crashes with TypeError: unsupported format string when budget_usd is NULL",
     "recommendation": "use `run.get('budget_usd') or 0`"},
    {"severity": "major", "file": "inventory/cli.py", "line": 567,
     "title": "missing null guard",
     "description": "dashboard renders a NULL budget without a null guard in the run table",
     "recommendation": "guard the value before formatting"},
    {"severity": "major", "file": "README.md", "line": 215,
     "title": "unhandled None budget",
     "description": "report generation raises on unhandled None budget values",
     "recommendation": "coalesce None to 0"},
]


class ThreeFindingReviewer:
    """Session 1 rejects with 3 major findings; every later session approves."""

    def __init__(self):
        self.sessions = 0

    def __call__(self, body):
        from . import fake_team
        n = fake_team._n_assistant(body)
        if n == 0:
            self.sessions += 1
            return {"content": "", "tool_calls": [
                fake_team.tool_call("read_file", {"path": "README.md"})]}
        if self.sessions > 1:
            rv = {"verdict": "approve", "summary": "All findings fixed.", "findings": []}
        else:
            rv = {"verdict": "request_changes", "summary": "3 blocking findings.",
                  "findings": MAJOR_FINDINGS}
        return {"content": "", "tool_calls": [fake_team.tool_call("submit_review", rv)]}


async def test_pipeline_repair_tasks_from_major_findings(cfg, sample_repo):
    """3 major findings -> 3 mandatory repair tasks that integrate before the re-review."""
    from swarmforge.db import Store
    from swarmforge.orchestrator import Orchestrator

    from . import fake_team
    from .conftest import ISSUE, FakeProvider

    cfg.limits.max_runtime_s = 600  # 4 parallel repair/dev agents need headroom
    store = Store(cfg.db_path)
    reviewer = ThreeFindingReviewer()
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

    # (a) exactly one repair task per major finding
    tasks = {t["task_key"]: t for t in store.tasks(run_id)}
    for i, f in enumerate(MAJOR_FINDINGS, start=1):
        key = f"R1F{i}"
        assert key in tasks, f"missing repair task {key}"
        t = tasks[key]
        assert t["depends_on"] == []
        assert t["kind"] == "fix"
        assert t["cycle"] == 1
        assert t["title"] == f["title"]
        assert f["description"] in t["acceptance_criteria"][0]
        assert f["description"] in t["description"]
        assert f"{f['file']}:{f['line']}" in t["description"]
    assert not any(k.startswith("R1F4") for k in tasks)

    # (b) all repair tasks integrated and the cycle-2 re-review ran afterwards
    assert all(tasks[f"R1F{i}"]["state"] == "INTEGRATED" for i in (1, 2, 3))
    assert reviewer.sessions == 2

    states = [e["data"] for e in store.events(run_id) if e["kind"] == "state"]
    path = [s.split('"to": "')[1].split('"')[0] for s in states]
    assert path[:8] == ["REPOSITORY_INSPECTION", "PLANNING", "TASK_VALIDATION", "TASK_EXECUTION",
                        "INTEGRATION", "TESTING", "CODE_REVIEW", "FIX_REQUIRED"]
    # second cycle: repair tasks execute, integrate, test, then re-review approves
    tail = path[8:]
    assert tail == ["TASK_VALIDATION", "TASK_EXECUTION", "INTEGRATION", "TESTING",
                    "CODE_REVIEW", "APPROVED", "COMPLETED"], path

    reviews = [e for e in store.events(run_id) if e["kind"] == "review"
               and "verdict=" in e["message"]]
    assert len(reviews) == 2
    first = json.loads(reviews[0]["data"]) if isinstance(reviews[0]["data"], str) else reviews[0]["data"]
    assert first["findings"] == MAJOR_FINDINGS
    second = json.loads(reviews[1]["data"]) if isinstance(reviews[1]["data"], str) else reviews[1]["data"]
    assert second["verdict"] == "approve"


def test_no_findings_registers_nothing():
    """Wiring invariant: empty findings (single-agent / clean review) -> no repair tasks."""
    assert repair_tasks_from_findings([], ["developer_backend"], cycle=1) == []


class MixedScopeReviewer:
    """Session 1 rejects with 1 in-scope + 1 out-of-scope major finding; later approves."""

    IN_SCOPE: ClassVar[dict] = {"severity": "major", "file": "inventory/cli.py", "line": 12,
                               "title": "cli export crash",
                               "description": "cli export crashes on empty inventory",
                               "recommendation": "guard the empty case"}
    OUT_OF_SCOPE: ClassVar[dict] = {"severity": "major", "file": "src/swarmforge/repos.py", "line": 99,
                                    "title": "GIT env leak",
                                    "description": "repos.py passes GIT env vars into subprocess",
                                    "recommendation": "strip GIT env vars"}

    def __init__(self):
        self.sessions = 0

    def __call__(self, body):
        from . import fake_team
        n = fake_team._n_assistant(body)
        if n == 0:
            self.sessions += 1
            return {"content": "", "tool_calls": [
                fake_team.tool_call("read_file", {"path": "README.md"})]}
        if self.sessions > 1:
            rv = {"verdict": "approve", "summary": "All findings fixed.", "findings": []}
        else:
            rv = {"verdict": "request_changes", "summary": "2 blocking findings.",
                  "findings": [self.IN_SCOPE, self.OUT_OF_SCOPE]}
        return {"content": "", "tool_calls": [fake_team.tool_call("submit_review", rv)]}


async def test_pipeline_out_of_scope_findings_skipped(cfg, sample_repo):
    """Out-of-scope blocking findings create no repair tasks and are logged as scope skips."""
    from swarmforge.db import Store
    from swarmforge.orchestrator import Orchestrator

    from . import fake_team
    from .conftest import ISSUE, FakeProvider

    cfg.limits.max_runtime_s = 600
    store = Store(cfg.db_path)
    reviewer = MixedScopeReviewer()
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

    tasks = store.tasks(run_id)
    repair = [t for t in tasks if t["task_key"].startswith("R1F")]
    assert len(repair) == 1
    assert repair[0]["title"] == MixedScopeReviewer.IN_SCOPE["title"]
    assert not any("repos.py" in (t["description"] or "") for t in tasks)

    scope_events = [e for e in store.events(run_id) if e["kind"] == "scope"
                    and "out-of-scope" in e["message"]]
    assert len(scope_events) == 1
    data = scope_events[0]["data"]
    if isinstance(data, str):
        data = json.loads(data)
    assert data["title"] == MixedScopeReviewer.OUT_OF_SCOPE["title"]


def test_gate_fails_open_without_plan(cfg):
    """A plan-less Orchestrator (empty scope) still blocks on every blocking finding."""
    from swarmforge.db import Store
    from swarmforge.orchestrator import Orchestrator
    from swarmforge.testing import TestEvidence

    store = Store(cfg.db_path)
    orch = Orchestrator(cfg, store, echo=None)
    assert orch._repair_scope() == set()
    ev = TestEvidence(label="final", command="pytest", exit_code=0, timed_out=False,
                      duration_s=1.0, passed=10, junit_found=True)
    review = {"verdict": "request_changes", "summary": "nope",
              "findings": [finding(file="src/swarmforge/repos.py", title="GIT env leak"),
                           finding(file="anything.py", severity="critical", title="boom")]}
    problems = orch._gate(0, ev, None, review, [])
    assert len(problems) == 2
    assert all(p["kind"] == "review" for p in problems)
