"""Parametrized tests for swarmforge.explain.explain_run."""

import pytest

from swarmforge.explain import SEVERITIES, Explanation, explain_run

# ---------------------------------------------------------------- helpers


def ev(kind="task", message="", task_key=None, level="info"):
    return {"kind": kind, "message": message, "task_key": task_key, "level": level}


def run(state, reason=None, **kw):
    d = {"state": state, "state_reason": reason}
    d.update(kw)
    return d


def check(exp: Explanation):
    assert isinstance(exp, Explanation)
    assert exp.headline and exp.headline.strip()
    assert exp.detail and exp.detail.strip()
    assert exp.severity in SEVERITIES
    assert isinstance(exp.actions, list) and len(exp.actions) >= 1
    assert all(isinstance(a, str) and a.strip() for a in exp.actions)


# ---------------------------------------------------------------- running phases

RUNNING_CASES = [
    ("ISSUE_RECEIVED", "The task has been received"),
    ("REPOSITORY_INSPECTION", "Inspecting the repository"),
    ("PLANNING", "breaking the task into small tasks"),
    ("TASK_VALIDATION", "valid"),
    ("INTEGRATION", "Merging the developers' work"),
    ("TESTING", "Running the test suite"),
    ("CODE_REVIEW", "reviewer is checking the diff"),
    ("FIX_REQUIRED", "corrective tasks"),
    ("APPROVED", "approval to push"),
    ("PULL_REQUEST_CREATION", "draft pull request"),
]


@pytest.mark.parametrize("phase,expect", RUNNING_CASES)
def test_running_phases(phase, expect):
    exp = explain_run(run(phase, phase=phase), [])
    check(exp)
    assert exp.severity == "info"
    assert expect.lower() in exp.detail.lower()
    assert any("Cancel" in a for a in exp.actions)


def test_running_task_execution_counts_running_tasks():
    events = [
        ev(message="RUNNING", task_key="t1"),
        ev(message="RUNNING", task_key="t2"),
        ev(message="RUNNING: waiting on dependency", task_key="t3"),
    ]
    exp = explain_run(run("TASK_EXECUTION", phase="TASK_EXECUTION"), events)
    check(exp)
    assert "Developers are coding 3 tasks" in exp.detail


def test_running_task_last_state_wins_no_overcount():
    """A task that went RUNNING then SUCCEEDED is no longer running."""
    events = [
        ev(message="RUNNING", task_key="t1"),
        ev(message="SUCCEEDED", task_key="t1"),
        ev(message="RUNNING", task_key="t2"),
        ev(message="RUNNING", task_key="t2"),
        ev(message="SUCCEEDED", task_key="t2"),
        ev(message="FAILED: tests broke", task_key="t3"),
        ev(message="RUNNING", task_key="t4"),
        ev(message="BLOCKED", task_key="t4"),
        ev(message="CANCELLED", task_key="t5"),
    ]
    exp = explain_run(run("TASK_EXECUTION", phase="TASK_EXECUTION"), events)
    assert "Developers are coding 0 tasks" in exp.detail


def test_running_single_task_grammar():
    exp = explain_run(run("TASK_EXECUTION", phase="TASK_EXECUTION"),
                      [ev(message="RUNNING", task_key="t1")])
    assert "coding 1 task." in exp.detail


def test_running_ignores_non_task_events():
    events = [ev(kind="run", message="RUNNING"), ev(kind="plan", message="RUNNING")]
    exp = explain_run(run("TASK_EXECUTION", phase="TASK_EXECUTION"), events)
    assert "coding 0 tasks" in exp.detail


# ---------------------------------------------------------------- COMPLETED


def test_completed_with_pr():
    exp = explain_run(run("COMPLETED", "approved; PR created",
                          pr_url="https://github.com/o/r/pull/12"), [])
    check(exp)
    assert exp.severity == "success"
    assert "pull request" in exp.headline.lower()
    assert "https://github.com/o/r/pull/12" in exp.detail
    assert any("https://github.com/o/r/pull/12" in a for a in exp.actions)
    assert any("merged automatically" in a for a in exp.actions)


def test_completed_delivered_to_branch():
    reason = "approved; delivered to branch sf/20260101-120000-abcdef in /workspace/repo"
    exp = explain_run(run("COMPLETED", reason,
                          integration_branch="sf/20260101-120000-abcdef",
                          repo="/workspace/repo", base_branch="main"), [])
    check(exp)
    assert exp.severity == "success"
    assert "local branch" in exp.headline.lower()
    joined = "\n".join(exp.actions)
    assert "git diff main...sf/20260101-120000-abcdef" in joined
    assert "git merge sf/20260101-120000-abcdef" in joined
    assert "git show sf/20260101-120000-abcdef" in joined


def test_completed_delivered_to_branch_parses_reason():
    """Branch/ src fall back to parsing the reason string."""
    reason = "approved; delivered to branch sf/x in /repo"
    exp = explain_run(run("COMPLETED", reason), [])
    joined = "\n".join(exp.actions)
    assert "git diff main...sf/x" in joined
    assert "git merge sf/x" in joined


def test_completed_pr_not_created():
    reason = "approved; PR not created (no push access). Add a GitHub token."
    exp = explain_run(run("COMPLETED", reason), [])
    check(exp)
    assert exp.severity == "warning"
    assert "no pull request" in exp.headline.lower()
    assert any("GITHUB_TOKEN" in a or "token" in a.lower() for a in exp.actions)
    assert any("Push branch" in a for a in exp.actions)


def test_completed_generic_fallback():
    exp = explain_run(run("COMPLETED", "approved; PR creation disabled in config"), [])
    check(exp)
    assert exp.severity == "success"


def test_completed_null_budget():
    """COMPLETED with budget_usd=None must not raise (regression)."""
    exp = explain_run(run("COMPLETED", "approved; PR creation disabled in config",
                          budget_usd=None), [])
    check(exp)
    assert "$0.00" in "\n".join(exp.actions)


# ---------------------------------------------------------------- BUDGET_EXCEEDED


@pytest.mark.parametrize("budget", [None, 5, 12.5])
@pytest.mark.parametrize("present", [True, False])
def test_budget_exceeded(budget, present):
    kw = {"budget_usd": budget} if present else {}
    exp = explain_run(run("BUDGET_EXCEEDED", "budget of $5.00 exceeded", **kw), [])
    check(exp)
    assert exp.severity == "error"
    assert "budget" in exp.headline.lower()
    assert "$" in exp.detail
    assert any("budget" in a.lower() for a in exp.actions)


def test_budget_exceeded_null_budget_quotes_zero():
    exp = explain_run(run("BUDGET_EXCEEDED", "budget exceeded", budget_usd=None), [])
    assert "$0.00" in exp.detail


# ---------------------------------------------------------------- BLOCKED


BLOCKED_CASES = [
    (("Docker is not installed (or not on PATH). Install Docker Desktop, or set sandbox.mode: "
     "local in swarmforge.yaml (unsandboxed)."),
     "Docker is not installed", ["Docker Desktop", "sandbox.mode: local"]),
    (("Docker is not responding (error during connect). Start Docker Desktop and wait for "
     "'Engine running'."),
     "Docker is not running", ["Engine running"]),
    (("The API key for provider 'openai' is missing: add OPENAI_API_KEY=... to the .env file "
     "next to swarmforge.yaml and restart the dashboard."),
     "API key is missing", ["OPENAI_API_KEY", ".env"]),
    (("The sandbox image 'swarmforge-dev:latest' is not built yet. Run `swarmforge sandbox "
     "build --dev` once."),
     "sandbox image", ["swarmforge sandbox build --dev"]),
    ("The sandbox image 'swarmforge:latest' is not built yet. Run `swarmforge sandbox build` once.",
     "sandbox image", ["swarmforge sandbox build"]),
    (("the repository https://github.com/o/r has no commits yet. SwarmForge needs a base commit "
     "to branch from."),
     "repository is empty", ["git init -b main", "git commit"]),
    ("cannot access repository o/r: repository not found. Check the name and credentials.",
     "could not be reached", ["spelling", "GITHUB_TOKEN"]),
    ("'/workspace/repo' is not a git repository. In that folder run: git init -b main.",
     "not a git repository", ["git init -b main"]),
    ("could not clone https://github.com/o/r: fatal: repository not found",
     "could not be cloned", ["URL", "network"]),
]



@pytest.mark.parametrize("reason,headline,action_bits", BLOCKED_CASES)
def test_blocked_cases(reason, headline, action_bits):
    exp = explain_run(run("BLOCKED", reason), [])
    check(exp)
    assert exp.severity == "warning"
    assert headline.lower() in exp.headline.lower()
    joined = "\n".join(exp.actions)
    for bit in action_bits:
        assert bit.lower() in joined.lower()
    # the raw technical reason is preserved
    assert reason[:40] in exp.detail


def test_blocked_waiting_for_repository_lock():
    reason = "waiting for the repository: run(s) 20260101-000000-aaaaaa — shared clone"
    exp = explain_run(run("BLOCKED", reason), [])
    check(exp)
    assert exp.severity == "info"
    assert "another run" in exp.headline.lower()


def test_blocked_generic_fallback():
    exp = explain_run(run("BLOCKED", "swarmforge.yaml is not valid YAML: bad mapping"), [])
    check(exp)
    assert exp.severity == "warning"
    assert "could not start" in exp.headline.lower()
    assert "swarmforge.yaml is not valid YAML" in exp.detail


# ---------------------------------------------------------------- FAILED


def test_failed_repair_limit_summarizes_problems():
    reason = ("repair limit reached (2 cycle(s)); 2 unresolved problem(s): "
              "[major] test_foo.py fails on import; [minor] README not updated")
    exp = explain_run(run("FAILED", reason), [])
    check(exp)
    assert exp.severity == "error"
    assert "unresolved problems" in exp.headline.lower()
    assert "[major] test_foo.py fails on import" in exp.detail
    assert "[minor] README not updated" in exp.detail
    assert any("Review" in a for a in exp.actions)


def test_failed_no_code_changes():
    exp = explain_run(run("FAILED", "no code changes were produced by any task"), [])
    check(exp)
    assert exp.severity == "error"
    assert "no code changes" in exp.headline.lower()


def test_failed_maximum_runtime():
    exp = explain_run(run("FAILED", "maximum runtime of 3600s exceeded"), [])
    check(exp)
    assert exp.severity == "error"
    assert "time limit" in exp.headline.lower()
    assert any("max_runtime_s" in a for a in exp.actions)


def test_failed_unexpected_error():
    exp = explain_run(run("FAILED", "unexpected error: TypeError: bad argument"), [])
    check(exp)
    assert exp.severity == "error"
    assert "unexpectedly" in exp.headline.lower()
    assert "TypeError: bad argument" in exp.detail
    assert any("Console" in a for a in exp.actions)


# ---------------------------------------------------------------- CANCELLED


def test_cancelled():
    exp = explain_run(run("CANCELLED", "interrupted by user"), [])
    check(exp)
    assert exp.severity == "info"
    assert "cancelled" in exp.headline.lower()
    assert any("Run again" in a or "new task" in a for a in exp.actions)


# ---------------------------------------------------------------- purity


def test_module_is_pure():
    import sys

    import swarmforge.explain as m

    assert "streamlit" not in sys.modules or not any(
        "streamlit" in str(getattr(m, k)) for k in dir(m))
    with open(m.__file__) as fh:
        assert "streamlit" not in fh.read()
