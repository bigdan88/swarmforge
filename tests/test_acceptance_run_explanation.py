"""Acceptance tests for the run explanation card (issue: "Show what happened and what to do next").

Focused regression evidence for AC2: no explain_run handler may crash when
budget_usd is NULL / missing, and every documented case must still return a
specific headline plus at least one action (AC1).
"""

import pytest

from swarmforge.explain import SEVERITIES, Explanation, explain_run


def _run(state, reason=None, **kw):
    d = {"state": state, "state_reason": reason}
    d.update(kw)
    return d


def _assert_valid(exp: Explanation):
    assert isinstance(exp, Explanation)
    assert exp.headline and exp.headline.strip()
    assert exp.detail and exp.detail.strip()
    assert exp.severity in SEVERITIES
    assert len(exp.actions) >= 1
    assert all(isinstance(a, str) and a.strip() for a in exp.actions)


# ------------------------------------------------------------------ AC2: NULL budget

NULL_BUDGET_CASES = [
    # (state, reason, extra run fields)
    ("BUDGET_EXCEEDED", "budget of $5.00 exceeded", {}),
    ("BUDGET_EXCEEDED", "budget exceeded", {"budget_usd": None}),
    ("BUDGET_EXCEEDED", "budget exceeded", {"budget_usd": 0}),
    ("COMPLETED", "approved; PR creation disabled in config", {"budget_usd": None}),
    ("COMPLETED", "approved; PR created", {"budget_usd": None,
                                           "pr_url": "https://github.com/o/r/pull/1"}),
    ("COMPLETED", "approved; delivered to branch sf/x in /repo", {"budget_usd": None}),
    ("COMPLETED", "approved; PR not created (no push access)", {"budget_usd": None}),
]


@pytest.mark.parametrize("state,reason,extra", NULL_BUDGET_CASES)
def test_null_budget_does_not_crash(state, reason, extra):
    """budget_usd=None (or the key missing) must never raise TypeError."""
    exp = explain_run(_run(state, reason, **extra), [])
    _assert_valid(exp)


def test_budget_exceeded_null_budget_shows_zero_amount():
    exp = explain_run(_run("BUDGET_EXCEEDED", "budget exceeded", budget_usd=None), [])
    assert "$0.00" in exp.detail
    assert exp.severity == "error"
    assert "budget" in exp.headline.lower()


def test_completed_null_budget_shows_zero_amount():
    exp = explain_run(_run("COMPLETED", "approved; PR creation disabled in config",
                           budget_usd=None), [])
    assert "$0.00" in "\n".join(exp.actions)


def test_budget_key_absent_does_not_crash():
    """Even a run dict without the budget_usd key at all must be handled."""
    exp = explain_run(_run("BUDGET_EXCEEDED", "budget exceeded"), [])
    _assert_valid(exp)
    assert "$0.00" in exp.detail


# ------------------------------------------------------------------ AC1: every case

FULL_COVERAGE_CASES = [
    ("TASK_EXECUTION", None, {"phase": "TASK_EXECUTION"}),
    ("COMPLETED", "approved; PR created", {"pr_url": "https://github.com/o/r/pull/9"}),
    ("COMPLETED", "approved; delivered to branch sf/b in /repo",
     {"integration_branch": "sf/b", "repo": "/repo", "base_branch": "main"}),
    ("COMPLETED", "approved; PR not created (no push access). Add a GitHub token.", {}),
    ("BLOCKED", "Docker is not installed (or not on PATH).", {}),
    ("BLOCKED", "Docker is not responding (error during connect).", {}),
    ("BLOCKED", ("The API key for provider 'openai' is missing: add OPENAI_API_KEY=... "
                 "to the .env file next to swarmforge.yaml and restart the dashboard."), {}),
    ("BLOCKED", "The repository https://github.com/o/r has no commits yet.", {}),
    ("BLOCKED", "cannot access repository o/r: repository not found.", {}),
    ("BLOCKED", "'/workspace/repo' is not a git repository. In that folder run: git init -b main.", {}),
    ("BLOCKED", ("The sandbox image 'swarmforge:latest' is not built yet. "
                 "Run `swarmforge sandbox build` once."), {}),
    ("BUDGET_EXCEEDED", "budget of $5.00 exceeded", {"budget_usd": 5.0}),
    ("FAILED", ("repair limit reached (2 cycle(s)); 1 unresolved problem(s): "
               "[major] something broke"), {}),
    ("CANCELLED", "interrupted by user", {}),
    ("FAILED", "unexpected error: RuntimeError: boom", {}),
]


@pytest.mark.parametrize("state,reason,extra", FULL_COVERAGE_CASES)
def test_every_case_has_specific_headline_and_action(state, reason, extra):
    exp = explain_run(_run(state, reason, **extra), [])
    _assert_valid(exp)


def test_headlines_are_distinct_per_case():
    """Each case gets a *specific* headline, not one generic string."""
    headlines = set()
    for state, reason, extra in FULL_COVERAGE_CASES:
        exp = explain_run(_run(state, reason, **extra), [])
        headlines.add(exp.headline)
    # running/cancelled/generic may share, but the distinct outcomes must differ
    assert len(headlines) >= 12
