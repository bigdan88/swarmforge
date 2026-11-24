"""Retained chat proposals and reviewed launches, with fake worker/process adapters."""
import hashlib
import json

from swarmforge.chatbot import parse_actions
from swarmforge.db import Store
from swarmforge.projects.store import Projects
from swarmforge.services.dashboard import DashboardActions
from swarmforge.webapp.contracts import ActionCommand, ConversationDetail
from swarmforge.webapp.workspace import WorkspaceReader
from .test_projects import definition


def test_retained_suggestion_launch_uses_queue_guards_and_survives_service_restart(cfg):
    store = Store(cfg.db_path)
    starts = []
    try:
        store.conversation_create("chat", "Ideas")
        shown, suggestions = parse_actions('Review this.\n```actions\n' + json.dumps({"tasks": [{
            "title": "CSV export", "description": "Export rows. Acceptance: CSV matches the inventory.", "repo": "owner/app"}]}) + '\n```')
        mid = store.conversation_append("chat", "assistant", shown, actions=suggestions)
        actions = DashboardActions(store, cfg, worker_start=lambda *_: starts.append(True))
        view = ConversationDetail.model_validate(WorkspaceReader(cfg.db_path).conversation("chat"))
        assert view.messages[-1].actions == suggestions and view.messages[-1].content == "Review this."
        assert not store.queue_items() and not starts
        args = {"index": 0, "title": "Reviewed CSV export", "body": "Reviewed acceptance criteria", "repo": "owner/app", "budget": 2}
        assert not actions.execute("chat-guard-noauth001", "launch_chat_task", str(mid), args)["ok"]
        assert not actions.execute("chat-guard-badlimit1", "launch_chat_task", str(mid), {**args, "budget": 1000, "spending_authorized": True})["ok"]
        assert not actions.execute("chat-guard-badindex1", "launch_chat_task", str(mid), {**args, "index": True, "spending_authorized": True})["ok"]
        assert not store.queue_items() and not starts
        args["spending_authorized"] = True
        first = actions.execute("chat-launch-once001", "launch_chat_task", str(mid), args)
        assert first["ok"] and starts == [True]
        restarted = DashboardActions(store, cfg, worker_start=lambda *_: starts.append(True))
        assert restarted.execute("chat-launch-once001", "launch_chat_task", str(mid), args) == first
        assert restarted.execute("chat-launch-once002", "launch_chat_task", str(mid), args)["item_id"] == first["item_id"]
        assert restarted.execute("chat-launch-once003", "launch_chat_task", str(mid).zfill(8), args)["item_id"] == first["item_id"]
        assert len(store.queue_items()) == 1 and starts == [True]
        item = store.queue_items()[0]
        assert item["body"] == args["body"] and item["title"] == args["title"]
        assert item["budget"] == 2 and item["approval"] == "dashboard" and not item["project_id"]
        assert item["max_runtime_s"] == cfg.limits.max_runtime_s
        again = ConversationDetail.model_validate(WorkspaceReader(cfg.db_path).conversation("chat"))
        assert again.messages[-1].launched_tasks == {"0": first["item_id"]}
        assert again.messages[-1].actions == suggestions
        assert store._one("SELECT COUNT(*) AS n FROM llm_calls")["n"] == 0
    finally:
        store.close()


def test_project_approval_starts_worker_only_after_reviewed_spec(cfg):
    store = Store(cfg.db_path)
    starts = []
    try:
        actions = DashboardActions(store, cfg, worker_start=lambda *_: starts.append(True))
        assert ActionCommand(operation="start_project").operation == "start_project"
        pid = actions.execute("new-project-chat001", "start_project", "", {})["project_id"]
        projects = Projects(store)
        assert projects.get(pid)["definition"] == {} and not starts
        projects.draft(pid, definition())
        spec = projects.get(pid)["spec"]
        args = {"spending_authorized": True, "expected_spec": "stale"}
        assert not actions.execute("project-stale-spec1", "launch_project", pid, args)["ok"]
        assert projects.get(pid)["status"] == "drafting" and not starts
        args["expected_spec"] = hashlib.sha256(spec.encode()).hexdigest()
        result = actions.execute("project-launch-ok01", "launch_project", pid, args)
        assert result["ok"] and starts == [True] and projects.get(pid)["status"] == "running"
        assert actions.execute("project-launch-ok01", "launch_project", pid, args) == result and starts == [True]
        assert not projects.items(pid)  # No model, repository operation or paid build in this test.
        assert projects.get(pid)["spec"] == spec
    finally:
        store.close()
