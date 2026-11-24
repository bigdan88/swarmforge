"""Conversation-led kickoff and queue recovery, using disposable state only."""
import hashlib

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from swarmforge.db import Store
from swarmforge.projects import lead
from swarmforge.projects.store import Projects
from swarmforge.services.dashboard import DashboardActions
from swarmforge.webapp.app import create_app
from .test_projects import definition
from .test_web_controls import authorize


def test_kickoff_conversation_refines_then_requires_reviewed_launch(cfg, tmp_path, monkeypatch):
    cfg.projects.default_repo_owner = "bigdan88"
    store = Store(cfg.db_path)
    calls, starts = [], []

    async def fake_call(config, s, project, prompt, context, transport=None):
        calls.append(context)
        return {"reply": "Who will use it?", "questions": [{"id": "users", "label": "Who will use it?", "kind": "text", "required": True}], "definition": None} if len(calls) == 1 else {
            "reply": "Review the proposed milestones and allowance.", "definition": definition()}

    monkeypatch.setattr(lead, "call", fake_call)
    actions = DashboardActions(store, cfg, worker_start=lambda *_: starts.append(True), chat_background=False)
    try:
        with TestClient(create_app(cfg.db_path, tmp_path, actions=actions), base_url="http://127.0.0.1") as client:
            def send(key, operation, target="", **args):
                return client.post("/api/v1/actions", json={"operation": operation, "target": target, "args": args},
                                   headers={**authorize(client), "idempotency-key": key}).json()

            created = send("kickoff-start-000001", "start_project")
            assert created["ok"]
            pid = created["project_id"]
            assert send("kickoff-start-000001", "start_project")["project_id"] == pid
            ps = Projects(store)
            assert len(ps.list()) == 1 and not calls and not starts
            empty = client.get(f"/api/v1/projects/{pid}").json()
            assert empty["definition"] == {} and empty["spec"] == ""
            assert empty["messages"][0]["role"] == "assistant"
            assert empty["messages"][0]["actions"]["kickoff_defaults"]["repo_owner"] == "bigdan88"
            assert not send("kickoff-launch-early", "launch_project", pid,
                            expected_spec=empty["spec_sha"], spending_authorized=True)["ok"]

            first = send("kickoff-message-0001", "project_message", pid,
                         answers={"goal": "I want an inventory app"}, expected_question=empty["messages"][-1]["id"], expected_spec=empty["spec_sha"])
            assert first["ok"] and first["draft_ready"] is False
            assert ps.get(pid)["definition"] == {} and not ps.items(pid)
            followup = client.get(f"/api/v1/projects/{pid}").json()
            second = send("kickoff-message-0002", "project_message", pid,
                          answers={"users": "Small shop owners; use owner/app and a $30 cap"}, expected_question=followup["messages"][-1]["id"], expected_spec=empty["spec_sha"])
            assert second["ok"] and second["draft_ready"] is True
            ready = client.get(f"/api/v1/projects/{pid}").json()
            assert len(ready["definition"]["milestones"]) == 3
            assert [m["role"] for m in ready["messages"]] == ["assistant", "user", "assistant", "user", "assistant"]
            assert len(calls) == 2 and ps.get(pid)["status"] == "drafting"
            assert not ps.milestones(pid) and not ps.items(pid) and not starts
            assert not send("kickoff-launch-stale", "launch_project", pid,
                            expected_spec=empty["spec_sha"], spending_authorized=True)["ok"]
            assert not send("kickoff-launch-noauth", "launch_project", pid, expected_spec=ready["spec_sha"])["ok"]
            assert send("kickoff-launch-valid", "launch_project", pid,
                        expected_spec=ready["spec_sha"], spending_authorized=True)["ok"]
            assert ps.get(pid)["status"] == "running" and len(ps.milestones(pid)) == 3
            assert hashlib.sha256(ps.get(pid)["spec"].encode()).hexdigest() == ready["spec_sha"]
            assert not ps.items(pid) and starts == [True]
    finally:
        store.close()


def test_empty_queue_explains_project_hold_without_launching_worker(cfg, tmp_path):
    cfg.source_path = tmp_path / "config.yaml"
    store = Store(cfg.db_path)
    starts = []
    actions = DashboardActions(store, cfg, worker_start=lambda *_: starts.append(True))
    try:
        pid = Projects(store).create("Blocked project")["id"]
        store._exec("UPDATE projects SET status='waiting_for_you',last_error='Three attempts failed' WHERE id=?", (pid,))
        store.queue_add("Later milestone", "", "owner/app", project_id=pid, status="tentative")
        result = actions.execute("queue-empty-00000001", "start_queue", "", {"spending_authorized": True})
        assert not result["ok"] and "Blocked project" in result["message"] and not starts
        with TestClient(create_app(cfg.db_path, tmp_path, actions=actions), base_url="http://127.0.0.1") as client:
            state = client.get("/api/v1/queue").json()
            assert state["queued_count"] == 0
            assert state["blocked_projects"] == [{"id": pid, "name": "Blocked project", "status": "waiting_for_you", "reason": "Three attempts failed"}]
        store.queue_add("Ready task", "", "owner/other")
        assert actions.execute("queue-ready-00000001", "start_queue", "", {"spending_authorized": True})["ok"]
        assert starts == [True]
    finally:
        store.close()
