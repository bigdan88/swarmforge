"""Criteria a test can prove stay gates; checks only a person can do are listed, never fail a run."""
import pytest

from swarmforge.config import ProjectsConfig, ReviewConfig
from swarmforge.criteria import mark_person_checks, needs_person, split
from swarmforge.postmortem import classify, parse_diagnosis, rules_diagnosis
from swarmforge.services.assessment import assess_gate
from swarmforge.testing import TestEvidence

# Real criteria from the Horse Tinder project (Nov 23) plus everyday ones.
PERSON = [
    "The task report explicitly leaves the live URL / console checks unverified",
    "README + spec + a statement that browser checks are unverified",
    "Visually confirm the swipe animation looks right in the browser",
    "The live site at https://bigdan88.github.io/horse_tinder/ loads without console errors",
    "Open the app on a phone and check the layout",
    "Manually verify the match overlay appears about a third of the time",
    "The styling looks cohesive across pages",
    "No errors in the browser console",
]
TESTABLE = [
    "README.md contains a '## Testing' section documenting python -m pytest -q",
    "The modal has a close button that dismisses it",
    "Clicking Reset & Shuffle restores all 14 horses in a new random order",
    "No source, package or workflow files are modified (diff touches only README.md)",
    "A unit test checks the 33% match probability with a seeded random",
    "Manual test steps are documented in docs/testing.md",
    "The browser tab title is 'Horse Tinder'",
    "index.html sets the viewport meta tag for mobile browsers",
]


@pytest.mark.parametrize("text", PERSON)
def test_person_only_checks_are_recognized(text):
    assert needs_person(text)


@pytest.mark.parametrize("text", TESTABLE)
def test_testable_criteria_stay_gates(text):
    assert not needs_person(text)


def test_split_keeps_order_within_each_pile():
    gated, person = split([TESTABLE[0], PERSON[0], TESTABLE[1], PERSON[3]])
    assert gated == [TESTABLE[0], TESTABLE[1]] and person == [PERSON[0], PERSON[3]]


def _result(cid, text, status):
    return {"criterion_id": cid, "text": text, "status": status, "evidence": "e"}


def test_only_unproved_person_checks_become_needs_you():
    out = mark_person_checks([
        _result("AC1", PERSON[2], "unverified"),   # nobody could prove it -> for the person
        _result("AC2", PERSON[2], "failed"),       # a test actually failed: stays failed
        _result("AC3", TESTABLE[0], "unverified"), # a test could prove it: still unverified
        _result("AC4", PERSON[2], "satisfied"),
    ])
    assert [r["status"] for r in out] == ["needs_you", "failed", "unverified", "satisfied"]
    assert "does not block" in out[0]["evidence"]


def _gate(criteria):
    ev = TestEvidence(label="cycle0", command="pytest", exit_code=0, timed_out=False, duration_s=1,
                      passed=10, junit_found=True)
    return assess_gate(ev, criteria=criteria, tester_report=None, review=None, failed_tasks=[], task_rows=[],
                       scope=set(), visual_findings=[], policy=ReviewConfig(require_verified_criteria=True))


def test_gate_never_blocks_on_person_checks_but_still_on_unproved_or_failed_ones():
    assert _gate([_result("AC1", PERSON[0], "needs_you")]) == []
    unproved = _gate([_result("AC2", TESTABLE[0], "unverified")])
    assert [p["kind"] for p in unproved] == ["acceptance_unverified"] and "not proved by any test" in unproved[0]["summary"]
    assert [p["kind"] for p in _gate([_result("AC3", TESTABLE[0], "failed")])] == ["acceptance"]


def test_unprovable_criteria_are_re_planned_not_retried():
    run = {"state": "FAILED", "state_reason": "criteria not provable: no test proves AC6 (tests pass and review "
                                             "found no blocking problems). Re-plan: ...", "stop_kind": None}
    category = classify(run, [], [], [])
    assert category == "unprovable_criteria"
    rules = rules_diagnosis(category, run)
    assert rules.retry is False and "Re-plan" in rules.fix
    # even if the analysis model says "retry", an unchanged retry is never allowed for this category
    model = parse_diagnosis('{"category": "tests_failing", "root_cause": "x", "fix": "y", "retry": true}', rules)
    assert model.retry is False


def test_build_tasks_get_a_two_dollar_floor_by_default():
    assert ProjectsConfig().min_build_budget_usd == 2.0


def _approving_provider():
    from . import fake_team
    from .conftest import FakeProvider
    reviewer = fake_team.Reviewer()
    reviewer.sessions = 1  # approves on its first review
    return FakeProvider({"qwen-fake": fake_team.planner, "deepseek-fake": fake_team.developer,
                         "kimi-fake": fake_team.developer, "minimax-fake": fake_team.tester, "glm-fake": reviewer})


async def _run_with_unproved_criteria(cfg, sample_repo, monkeypatch, text):
    from swarmforge import orchestrator as orch_mod
    from swarmforge.db import Store
    from swarmforge.orchestrator import Orchestrator

    from .test_e2e import ISSUE
    cfg.review.require_verified_criteria = True
    monkeypatch.setattr(orch_mod, "evaluate_criteria", lambda mapping, criteria, ev: [
        {"criterion_id": cid, "text": text, "status": "unverified", "evidence": "no test mapped"} for cid, _ in criteria])
    store = Store(cfg.db_path)
    run_id = await Orchestrator(cfg, store, echo=None, llm_transport=_approving_provider().transport).execute(
        issue_file=str(ISSUE), repo=str(sample_repo))
    return store, store.get_run(run_id)


async def test_unproved_testable_criteria_stop_without_wasting_repair_cycles(cfg, sample_repo, monkeypatch):
    cfg.limits.max_repair_cycles = 2
    store, run = await _run_with_unproved_criteria(cfg, sample_repo, monkeypatch, "CSV export has a header row")
    assert run["state"] == "FAILED" and run["state_reason"].startswith("criteria not provable"), run["state_reason"]
    assert run["repair_cycle"] == 0  # a repair cycle re-runs the same tests and cannot prove them
    assert classify(run, [], [], []) == "unprovable_criteria"


async def test_person_only_criteria_are_listed_and_the_run_completes(cfg, sample_repo, monkeypatch):
    store, run = await _run_with_unproved_criteria(cfg, sample_repo, monkeypatch,
                                                   "Visually confirm the export looks right in the browser")
    assert run["state"] == "COMPLETED", run["state_reason"]
    assert {a["status"] for a in store.acceptance(run["id"])} == {"needs_you"}
    from swarmforge.report import build_report
    assert "For you to check" in build_report(store, run["id"])
