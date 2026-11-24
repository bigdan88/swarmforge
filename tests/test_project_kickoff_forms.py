"""Product-focused kickoff forms and drafts; fake models, no repositories or workers."""
import copy
import hashlib
import json

import pytest

from swarmforge.config import ConfigError, ProjectsConfig
from swarmforge.db import Store
from swarmforge.projects import kickoff, lead
from swarmforge.projects.spec import ProjectError, validate
from swarmforge.projects.store import Projects
from swarmforge.services.dashboard import DashboardActions
from swarmforge.webapp.contracts import ProjectDetail
from swarmforge.webapp.workspace import WorkspaceReader
from .test_projects import definition


def finish(coro):
    """Fake model turns finish immediately, without an event loop or socket access."""
    try:
        coro.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coro.close()
    raise AssertionError("The fake model unexpectedly attempted asynchronous I/O")


@pytest.fixture
def state(cfg):
    cfg.projects.default_repo_owner = "bigdan88"
    store = Store(cfg.db_path)
    project = Projects(store).create(kickoff_defaults=kickoff.defaults(cfg))
    yield cfg, store, project["id"]
    store.close()


def minimal():
    return {"name": "Garden Notes", "goal": "Track garden plants", "users": "Home gardeners",
            "milestones": [{"name": n, "goal": "Implement " + n,
                            "checks": ["Users can " + n, "Tests verify " + n]} for n in ("add plants", "record care", "export notes")]}


def test_draft_defaults_fill_optional_details_and_choose_repo_without_weakening_approval(cfg):
    cfg.projects.default_repo_owner = "bigdan88"
    raw = minimal()
    draft = kickoff.normalize(raw, kickoff.defaults(cfg))
    assert draft["repo"] == "bigdan88/garden-notes" and draft["repository_kind"] == "new"
    assert draft["budget_usd"] == 30 and "public deployment" in draft["ask_before"]
    assert sum(m["estimate_usd"] for m in draft["milestones"]) <= draft["budget_usd"]
    assert draft["stack"] and draft["deadline"] and draft["latitude"]
    assert "repo" not in raw and "estimate_usd" not in raw["milestones"][0]
    with pytest.raises(ProjectError):
        validate(raw)  # Strict final spec validation is unchanged.


def test_explicit_constraints_repository_and_cap_survive_estimate_normalization(cfg):
    raw = definition()
    raw.update(repo="https://github.com/another/Existing.git", ask_before="Changing the schema", budget_usd=3)
    before = copy.deepcopy(raw)
    draft = kickoff.normalize(raw, kickoff.defaults(cfg))
    assert draft["repo"] == "another/Existing" and draft["repository_kind"] == "existing"
    assert draft["ask_before"] == raw["ask_before"] and draft["budget_usd"] == 3
    assert sum(m["estimate_usd"] for m in draft["milestones"]) <= 3 and raw == before


def test_profile_url_means_owner_and_a_new_repository_named_for_the_project(cfg):
    cfg.projects.default_repo_owner = "bigdan88"
    draft = kickoff.normalize({**minimal(), "repo": "https://github.com/bigdan88"}, kickoff.defaults(cfg))
    assert draft["repo"] == "bigdan88/garden-notes" and draft["repository_kind"] == "new"


def test_initial_form_and_substantive_followup_then_automatic_proposal(state, monkeypatch):
    cfg, store, pid = state
    projects, calls = Projects(store), []
    p = projects.get(pid)
    mid, fields = kickoff.latest_questions(store, p)
    assert fields[0]["id"] == "goal" and fields[0]["required"]
    assert not any(f["id"] in ("budget_usd", "ask_before", "repo_owner") for f in fields)
    message = kickoff.answer_message(store, p, {"expected_question": mid, "answers": {"goal": "A garden journal for home gardeners"}})

    async def model(config, s, project, prompt, context, transport=None):
        calls.append(context)
        if len(calls) == 1:
            return {"questions": [{"id": "sharing", "label": "Who can see a garden?", "kind": "select",
                                    "options": ["Only me", "Invited gardeners"], "required": False}], "definition": None}
        assert context["draft_now"] and context["defaults"]["repo_owner"] == "bigdan88"
        return {"definition": minimal()}

    monkeypatch.setattr(lead, "call", model)
    finish(lead.interview(cfg, store, pid, message))
    assert not projects.get(pid)["definition"] and len(calls) == 1
    new_mid, next_fields = kickoff.latest_questions(store, p)
    assert next_fields[0]["kind"] == "select" and next_fields[0]["options"] == ["Only me", "Invited gardeners"]
    with pytest.raises(ProjectError, match="questions changed"):
        kickoff.answer_message(store, p, {"expected_question": mid, "answers": {"goal": "Old answer"}})
    # Optional choices may be skipped; typing a literal 'None' is never necessary.
    reply = kickoff.answer_message(store, p, {"expected_question": new_mid, "answers": {"sharing": ""}})
    finish(lead.interview(cfg, store, pid, reply))
    drafted = projects.get(pid)
    assert drafted["definition"]["repo"] == "bigdan88/garden-notes"
    assert drafted["status"] == "drafting" and drafted["approved_at"] is None
    assert not projects.items(pid) and not projects.milestones(pid) and len(calls) == 2
    view = ProjectDetail.model_validate(WorkspaceReader(cfg.db_path).project(pid))
    assert view.messages[-1].actions["project_questions"] == []
    assert "proposal is ready" in view.messages[-1].content
    assert store._one("SELECT COUNT(*) AS n FROM llm_calls")["n"] == 0


def test_missing_ask_before_and_budget_are_normalized_in_one_model_turn(state, monkeypatch):
    cfg, store, pid = state
    calls = []
    async def model(*args, **kwargs):
        calls.append(True)
        return {"definition": minimal()}
    monkeypatch.setattr(lead, "call", model)
    finish(lead.interview(cfg, store, pid, "A garden journal"))
    assert len(calls) == 1
    assert Projects(store).get(pid)["definition"]["ask_before"] == kickoff.OPTIONAL_DEFAULTS["ask_before"]


def test_malformed_model_draft_is_repaired_internally_with_no_schema_error_for_user(state, monkeypatch):
    cfg, store, pid = state
    calls = []
    async def model(config, s, project, prompt, context, transport=None):
        calls.append(context)
        return {"definition": {"name": "Garden Notes"}} if len(calls) == 1 else {"definition": minimal()}
    monkeypatch.setattr(lead, "call", model)
    finish(lead.interview(cfg, store, pid, "A garden journal"))
    assert len(calls) == 2 and calls[1]["draft_now"] and "repair" in calls[1]
    assert Projects(store).get(pid)["definition"]["name"] == "Garden Notes"
    assert all("Missing" not in m["content"] for m in store.conversation_messages("project-" + pid))


def test_repeated_prose_questions_produce_bounded_starter_proposal(state, monkeypatch):
    cfg, store, pid = state
    calls = []
    async def model(*args, **kwargs):
        calls.append(True)
        return {"reply": "What is your budget? Please type None for ask_before.", "definition": None}
    monkeypatch.setattr(lead, "call", model)
    finish(lead.interview(cfg, store, pid, "Garden journal for home gardeners"))
    assert len(calls) == 2
    assert Projects(store).get(pid)["definition"]["repo"].startswith("bigdan88/")
    assert len(Projects(store).get(pid)["definition"]["milestones"]) == 3
    assert "What is your budget?" not in store.conversation_messages("project-" + pid)[-1]["content"]


def test_form_guards_required_answers_options_and_stale_specs(state, monkeypatch):
    cfg, store, pid = state
    p = Projects(store).get(pid)
    mid, _ = kickoff.latest_questions(store, p)
    with pytest.raises(ProjectError, match="Please answer"):
        kickoff.answer_message(store, p, {"expected_question": mid, "answers": {"goal": ""}})
    with pytest.raises(ProjectError, match="displayed questions"):
        kickoff.answer_message(store, p, {"expected_question": mid, "answers": {"unexpected": "x"}})
    with pytest.raises(ProjectError, match="draft changed"):
        finish(lead.interview(cfg, store, pid, "An idea", expected_spec="stale"))
    assert len(store.conversation_messages(p["conversation_id"])) == 1


def test_question_schema_is_bounded_and_filters_setup_trivia():
    assert kickoff.questions([{"id": "ask_before", "label": "What should I ask before?"}, {"id": "budget", "label": "Budget?"}]) == []
    assert kickoff.questions([{"id": "x", "label": "A", "kind": "script"}, {"id": "x", "label": "Duplicate"}]) == [{"id": "x", "label": "A", "kind": "textarea", "required": False}]


def test_old_stalled_interview_gets_a_recovery_form_without_losing_its_brief(state, monkeypatch):
    cfg, store, pid = state
    p = Projects(store).get(pid)
    store.conversation_append(p["conversation_id"], "user", "Build a garden journal")
    mid = store.conversation_append(p["conversation_id"], "assistant", "What is your budget? Missing ask_before.")
    before = store.conversation_messages(p["conversation_id"])
    view = WorkspaceReader(cfg.db_path).project(pid, kickoff_defaults=kickoff.defaults(cfg))
    assert view["messages"][-1]["actions"]["project_questions"][0]["id"] == "notes"
    assert store.conversation_messages(p["conversation_id"]) == before  # Browsing remains read-only.
    answer = kickoff.answer_message(store, p, {"expected_question": mid, "answers": {"notes": ""}}, kickoff.defaults(cfg))
    async def model(config, s, project, prompt, context, transport=None):
        assert context["draft_now"] and any("garden journal" in m["content"] for m in context["messages"])
        return {"definition": minimal()}
    monkeypatch.setattr(lead, "call", model)
    finish(lead.interview(cfg, store, pid, answer))
    assert Projects(store).get(pid)["definition"]["repo"] == "bigdan88/garden-notes"


def test_question_submission_action_returns_ready_proposal_without_launching(state, monkeypatch):
    cfg, store, pid = state
    p = Projects(store).get(pid)
    mid, _ = kickoff.latest_questions(store, p)
    async def model(*args, **kwargs):
        return {"definition": minimal()}
    monkeypatch.setattr(lead, "call", model)
    monkeypatch.setattr("swarmforge.services.dashboard.asyncio.run", finish)
    starts = []
    actions = DashboardActions(store, cfg, worker_start=lambda *_: starts.append(True), chat_background=False)
    args = {"answers": {"goal": "A garden journal"}, "expected_question": mid,
            "expected_spec": hashlib.sha256(p["spec"].encode()).hexdigest()}
    result = actions.execute("kickoff-form-submit01", "project_message", pid, args)
    assert result["ok"] and result["draft_ready"] is True
    assert Projects(store).get(pid)["status"] == "drafting" and not starts and not store.queue_items()
    assert actions.execute("kickoff-form-submit01", "project_message", pid, args) == result


def test_project_default_configuration_rejects_bad_accounts_and_caps():
    for owner in ("https://github.com/user", "owner/repo", 123):
        with pytest.raises(ConfigError):
            ProjectsConfig(default_repo_owner=owner)
    for budget in (True, -1, float("nan"), float("inf")):
        with pytest.raises(ConfigError):
            ProjectsConfig(default_budget_usd=budget)
