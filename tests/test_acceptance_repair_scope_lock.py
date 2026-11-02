"""Acceptance tests for the repair-cycle scope lock (issue: skip out-of-scope findings).

* AC1: out-of-scope blocking findings produce NO repair task (issue example: a
  repos.py GIT-env and a Dockerfile ruff finding skipped in a theme-switch run).
* AC2: each skipped finding is logged as a kind-'scope' run event whose message
  contains 'out-of-scope' and whose data carries the original finding dict.
* AC3: in-scope findings still produce the exact same Tasks (shape, contiguous
  R<cycle>F<n> ids over kept findings, dedup, role round-robin); scope is additive.
* AC4: the deterministic gate does not block a cycle on out-of-scope findings and
  fails open (everything in scope) when there is no plan / no expected_files.
* AC5: dashboard caption is covered by tests/test_dashboard_scope_caption.py.
"""
from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from swarmforge.orchestrator import Orchestrator
from swarmforge.plan import Criterion, Plan
from swarmforge.plan import Task as PlanTask
from swarmforge.state import (
    out_of_scope_findings,
    repair_tasks_from_findings,
)
from swarmforge.testing import TestEvidence

# the theme-switch run of the issue: only these files were in the original plan
THEME_SCOPE = {"src/swarmforge/theme.py", "src/swarmforge/dashboard.py",
               "assets/theme.css", "tests/test_theme.py"}

REPOS_GIT_ENV = {"severity": "critical", "file": "src/swarmforge/repos.py", "line": 60,
                 "title": "GIT env leaks into agent subprocesses",
                 "description": "clone_at sets GIT_DIR without clearing GIT_WORK_TREE.",
                 "recommendation": "pop all GIT_* keys before spawning."}
DOCKERFILE_RUFF = {"severity": "major", "file": "Dockerfile", "line": 12,
                   "title": "ruff not pinned in the image",
                   "description": "pip install ruff without a version pin.",
                   "recommendation": "pin ruff."}
THEME_FINDING = {"severity": "major", "file": "src/swarmforge/theme.py", "line": 5,
                 "title": "theme switch KeyError",
                 "description": "missing guard on the palette lookup.",
                 "recommendation": "guard the lookup."}


def _finding(file="src/swarmforge/theme.py", severity="major", title="crash",
             description="d", recommendation="r"):
    return {"severity": severity, "file": file, "line": 10, "title": title,
            "description": description, "recommendation": recommendation}


def _review(*findings):
    return {"verdict": "request_changes", "summary": "blocking findings",
            "findings": list(findings)}


def _ev():
    return TestEvidence(label="final", command="pytest", exit_code=0, timed_out=False,
                        duration_s=1.0, passed=10, junit_found=True)


def _plan_orch(cfg, expected_files):
    from swarmforge.db import Store

    store = Store(cfg.db_path)
    orch = Orchestrator(cfg, store, echo=None)
    if expected_files is None:
        orch.plan = None
    else:
        orch.plan = Plan(summary="theme switching",
                         tasks=[PlanTask(id="T1", title="switch theme", description="d",
                                         role="developer_backend",
                                         expected_files=list(expected_files))],
                         acceptance_criteria=[Criterion(id="AC1", text="a theme switches")])
    return orch


# ----------------------------------------------------------------- AC1 (unit)
def test_issue_example_repos_and_dockerfile_findings_are_skipped():
    """The exact example from the issue: a theme-switch plan must not repair
    the navtools GIT-env (repos.py) or Dockerfile ruff findings."""
    findings = [REPOS_GIT_ENV, DOCKERFILE_RUFF, THEME_FINDING]
    tasks = repair_tasks_from_findings(findings, ["dev", "reviewer"], cycle=1,
                                       scope_files=THEME_SCOPE)
    assert [t.id for t in tasks] == ["R1F1"]
    assert tasks[0].title == THEME_FINDING["title"]
    assert not any("repos.py" in f"{t.title} {t.description} {t.expected_files}" for t in tasks)
    assert not any("Dockerfile" in f"{t.title} {t.description} {t.expected_files}" for t in tasks)
    skipped = out_of_scope_findings(findings, THEME_SCOPE, {"critical", "major"})
    assert [f["file"] for f in skipped] == ["src/swarmforge/repos.py", "Dockerfile"]


# ----------------------------------------------------------------- AC3 (unit)
def test_scoped_output_identical_to_unscoped_when_everything_in_scope():
    """With a scope that keeps every blocking finding, the produced Tasks are
    byte-for-byte what the pre-scope-lock code produced (shape, ids, dedup,
    round-robin); non-blocking severities are still dropped exactly as before."""
    findings = [
        _finding(title="a", description="A"),
        _finding(title="a", description="A"),                    # duplicate -> deduped
        _finding(file="src/swarmforge/dashboard.py", severity="critical", title="b"),
        _finding(file="assets/theme.css", severity="minor", title="c"),  # non-blocking
        _finding(title="a2", description="same file, new title"),
    ]
    roles = ["alpha", "beta", "gamma"]
    scoped = repair_tasks_from_findings(findings, roles, cycle=2, scope_files=THEME_SCOPE)
    plain = repair_tasks_from_findings(findings, roles, cycle=2)
    assert [asdict(t) for t in scoped] == [asdict(t) for t in plain]
    assert [(t.id, t.title, t.role) for t in scoped] == [
        ("R2F1", "a", "alpha"), ("R2F2", "b", "beta"), ("R2F3", "a2", "gamma")]


def test_scope_kept_findings_dedup_and_round_robin_count_over_kept_only():
    """Out-of-scope findings (including duplicates of them) must not consume a
    repair-task id nor shift the role round-robin of the kept findings."""
    findings = [
        REPOS_GIT_ENV,                                           # out of scope
        _finding(title="theme", description="A"),
        _finding(title="theme", description="A"),                # in-scope duplicate
        DOCKERFILE_RUFF,                                         # out of scope
        _finding(file="Dockerfile", title="docker dup"),         # out of scope
        _finding(file="src/swarmforge/dashboard.py", severity="critical", title="dash"),
    ]
    tasks = repair_tasks_from_findings(findings, ["one", "two"], cycle=3,
                                       scope_files=THEME_SCOPE)
    assert [(t.id, t.title, t.role) for t in tasks] == [
        ("R3F1", "theme", "one"), ("R3F2", "dash", "two")]


@pytest.mark.parametrize("scope", [None, set(), frozenset()])
def test_scope_none_and_empty_are_backward_compatible(scope):
    findings = [_finding(file="src/swarmforge/repos.py"), DOCKERFILE_RUFF]
    base = repair_tasks_from_findings(findings, ["dev"], cycle=1)
    assert [asdict(t) for t in repair_tasks_from_findings(
        findings, ["dev"], cycle=1, scope_files=scope)] == [asdict(t) for t in base]


# ----------------------------------------------------------------- AC4 (gate)
def test_gate_does_not_block_cycle_on_out_of_scope_findings(cfg, store):
    """A theme-switch run must not be pushed into another repair cycle by the
    repos.py GIT-env or Dockerfile findings: they are outside the plan's scope."""
    orch = _plan_orch(cfg, store, ["./src/swarmforge/theme.py", "src/swarmforge/dashboard.py"])
    no_file = {"severity": "critical", "title": "no file", "description": "d",
               "recommendation": "r"}
    problems = orch._gate(0, _ev(), None,
                          _review(REPOS_GIT_ENV, DOCKERFILE_RUFF, THEME_FINDING, no_file), [])
    review_problems = [p for p in problems if p["kind"] == "review"]
    # in-scope finding still blocks, and the file-less finding still blocks;
    # the two out-of-scope ones are gone -> fewer cycles burned.
    assert {json.loads(p["detail"])["title"] for p in review_problems} == \
           {"theme switch KeyError", "no file"}


def test_gate_fails_open_when_plan_has_no_expected_files(cfg, store):
    """Planner gave no expected_files -> empty scope -> every blocking finding is
    treated as in scope (fail open), identical to a run with no plan at all."""
    findings = [REPOS_GIT_ENV, DOCKERFILE_RUFF, THEME_FINDING]
    orch = _plan_orch(cfg, store, [])                 # plan exists, files empty
    assert orch._repair_scope() == set()
    with_plan = [p for p in orch._gate(0, _ev(), None, _review(*findings), [])
                 if p["kind"] == "review"]
    orch2 = _plan_orch(cfg, store, None)              # no plan at all
    assert orch2._repair_scope() == set()
    without_plan = [p for p in orch2._gate(0, _ev(), None, _review(*findings), [])
                    if p["kind"] == "review"]
    assert len(with_plan) == len(without_plan) == 3
    assert [json.loads(p["detail"]) for p in with_plan] == findings


def test_gate_blocks_on_in_scope_finding_exactly_as_before(cfg, store):
    """AC3 at the gate level: a fully in-scope review blocks like before, and the
    problem still carries the finding dict in its detail."""
    orch = _plan_orch(cfg, store, list(THEME_SCOPE))
    problems = orch._gate(0, _ev(), None, _review(THEME_FINDING), [])
    assert [p["kind"] for p in problems] == ["review"]
    assert json.loads(problems[0]["detail"]) == THEME_FINDING


# ---------------------------------------------------------- AC1+AC2 (pipeline)
async def test_pipeline_scope_event_carries_full_finding_dict(cfg, sample_repo):
    """End-to-end: exactly one kind-'scope' event per skipped finding, message
    containing 'out-of-scope', data == the whole original finding dict (AC2), and
    no repair task is ever created for it (AC1)."""
    pytest.importorskip("openai")
    from swarmforge.db import Store

    from . import fake_team
    from .conftest import ISSUE, FakeProvider
    from .test_state import MixedScopeReviewer

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

    assert store.get_run(run_id)["state"] == "COMPLETED"
    scope_events = [e for e in store.events(run_id) if e["kind"] == "scope"]
    assert len(scope_events) == 1
    assert "out-of-scope" in scope_events[0]["message"]
    data = scope_events[0]["data"]
    if isinstance(data, str):
        data = json.loads(data)
    # AC2: the event data carries the complete original finding dict, not a summary.
    assert data == MixedScopeReviewer.OUT_OF_SCOPE
    # AC1: only the in-scope finding produced a repair task, numbered R1F1.
    repair = [t for t in store.tasks(run_id) if t["task_key"].startswith("R1F")]
    assert [t["task_key"] for t in repair] == ["R1F1"]
    assert repair[0]["title"] == MixedScopeReviewer.IN_SCOPE["title"]
