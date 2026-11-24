"""Authenticated React journeys on disposable stores and fake model/process adapters."""
import hashlib
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from swarmforge.db import Store
from swarmforge.projects import service
from swarmforge.projects.spec import ProjectError
from swarmforge.projects.store import Projects
from swarmforge.services.chat_requests import wait_idle
from swarmforge.services.dashboard import ActionConflict, DashboardActions
from swarmforge.webapp.app import create_app
from .test_projects import definition


@pytest.fixture
def workspace(cfg):
    store = Store(cfg.db_path)
    yield cfg, store, DashboardActions(store, cfg)
    store.close()


def authorize(client):
    response = client.get("/api/v1/session")
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
    return {"origin": "http://127.0.0.1", "x-csrf-token": response.json()["csrf_token"], "idempotency-key": "request-00000000000001",
            "x-swarmforge-workspace": response.json()["workspace_id"]}


def test_actions_require_opt_in_session_origin_csrf_and_json(workspace, tmp_path):
    cfg, store, actions = workspace
    with TestClient(create_app(cfg.db_path, tmp_path), base_url="http://127.0.0.1") as readonly:
        assert readonly.get("/api/v1/session").json()["actions_enabled"] is False
        assert readonly.post("/api/v1/actions", json={}).status_code == 405
    with TestClient(create_app(cfg.db_path, tmp_path, actions=actions), base_url="http://127.0.0.1") as client:
        body = {"operation": "pause_queue"}
        assert client.post("/api/v1/actions", json=body).status_code == 403
        headers = authorize(client)
        assert client.post("/api/v1/actions", json=body, headers={**headers, "origin": "https://elsewhere.test"}).status_code == 403
        assert client.post("/api/v1/actions", json=body, headers={**headers, "x-csrf-token": "wrong"}).status_code == 403
        assert client.post("/api/v1/actions", content="body", headers=headers).status_code == 415
        assert client.post("/api/v1/actions", json=body, headers={**headers, "content-length": "999999"}).status_code == 413
        assert client.post("/api/v1/actions", json=body, headers=headers).json()["ok"]
        assert store.kv_get("queue_paused") == "1"
        assert client.get("/api/v1/session", headers={"host": "testserver"}).status_code == 400
        # Bootstrapping a second tab retains the same valid CSRF session.
        assert authorize(client)["x-csrf-token"] == headers["x-csrf-token"]


def test_duplicate_request_survives_service_restart_and_key_reuse_is_rejected(workspace):
    cfg, store, actions = workspace
    args = {"title": "Add export", "body": "Acceptance: CSV exports", "repo": "owner/app", "budget": 2, "spending_authorized": True}
    first = actions.execute("queue-request-000001", "queue_task", "", args)
    assert first["ok"] and first["item_id"]
    assert DashboardActions(store, cfg).execute("queue-request-000001", "queue_task", "", args) == first
    assert len(store.queue_items()) == 1
    assert store.queue_items()[0]["approval"] == "dashboard"
    with pytest.raises(ActionConflict, match="different"):
        actions.execute("queue-request-000001", "queue_task", "", {**args, "title": "Different"})


def test_uncertain_action_is_retained_and_never_executed_twice(workspace, monkeypatch):
    cfg, store, actions = workspace
    calls = []
    def interrupted(*args):
        calls.append(1)
        raise RuntimeError("simulated interruption after an external effect")
    monkeypatch.setattr(actions, "_apply", interrupted)
    with pytest.raises(ActionConflict, match="uncertain"):
        actions.execute("uncertain-request-0001", "start_queue", "", {})
    with pytest.raises(ActionConflict, match="uncertain"):
        DashboardActions(store, cfg).execute("uncertain-request-0001", "start_queue", "", {})
    assert len(calls) == 1


def test_project_spec_review_is_checked_atomically_and_browsing_never_launches(workspace, tmp_path):
    cfg, store, actions = workspace
    p = Projects(store)
    created = actions.execute("project-create-00001", "create_project", "", {"definition": definition()})
    pid = created["project_id"]
    original = p.get(pid)["spec"]
    with TestClient(create_app(cfg.db_path, tmp_path, actions=actions), base_url="http://127.0.0.1") as client:
        headers = authorize(client)
        page = client.get("/api/v1/projects").json()
        assert page["items"][0]["spent_usd"] == 0
        detail = client.get(f"/api/v1/projects/{pid}").json()
        assert detail["spec_sha"] == hashlib.sha256(original.encode()).hexdigest()
        assert p.get(pid)["status"] == "drafting" and not p.milestones(pid) and not p.items(pid)
        command = {"operation": "launch_project", "target": pid, "args": {"expected_spec": "stale", "spending_authorized": True}}
        assert client.post("/api/v1/actions", json=command, headers=headers).json()["ok"] is False
        assert p.get(pid)["status"] == "drafting"
        headers["idempotency-key"] = "project-launch-000002"
        command["args"]["expected_spec"] = detail["spec_sha"]
        assert client.post("/api/v1/actions", json=command, headers=headers).json()["ok"]
        assert p.get(pid)["status"] == "running"
        assert p.get(pid)["spec"] == original
        assert len(p.milestones(pid)) == 3 and not p.items(pid)
    with pytest.raises(ProjectError, match="draft"):
        p.draft(pid, definition(), expected_spec=detail["spec_sha"])


def test_stale_draft_save_cannot_overwrite_newer_spec(workspace):
    _, store, _ = workspace
    ps = Projects(store)
    pid = ps.create()["id"]
    ps.draft(pid, definition())
    digest = hashlib.sha256(ps.get(pid)["spec"].encode()).hexdigest()
    revised = {**definition(), "goal": "A newer agreed goal"}
    ps.draft(pid, revised, expected_spec=digest)
    with pytest.raises(ProjectError, match="changed"):
        ps.draft(pid, definition(), expected_spec=digest)
    assert ps.get(pid)["definition"]["goal"] == revised["goal"]


def test_project_retry_cannot_escape_accounting_and_approval_is_not_overwritten(workspace):
    _, store, actions = workspace
    ps = Projects(store);pid = ps.create()["id"];ps.draft(pid, definition());service.approve(store, pid)
    qid = store.queue_add("Project task", "", "owner/app", project_id=pid, status="failed")
    result = actions.execute("project-retry-000001", "retry_work", f"task-{qid}", {"spending_authorized": True})
    assert not result["ok"] and len(store.queue_items()) == 1
    store.create_run("active")
    approval = store.request_approval("active", "Open a draft PR?")
    assert actions.execute("approval-request-001", "decide_approval", str(approval), {"approved": False})["ok"]
    assert not actions.execute("approval-request-002", "decide_approval", str(approval), {"approved": True})["ok"]
    assert store.approval(approval)["status"] == "declined"


def test_chat_duplicate_uses_fake_adapter_once_and_keeps_project_conversations_separate(workspace, monkeypatch, tmp_path):
    cfg, store, actions = workspace
    calls = []
    async def answer(config, s, question, *, conversation):
        calls.append(question)
        s.conversation_append(conversation, "user", question)
        s.conversation_append(conversation, "assistant", "Fake recorded reply")
        return {"answer": "Fake recorded reply"}
    monkeypatch.setattr("swarmforge.chatbot.answer", answer)
    cid = actions.execute("chat-create-0000001", "create_conversation", "", {})["conversation_id"]
    body = {"message": "What is blocked?", "spending_authorized": True}
    first = actions.execute("chat-message-000001", "send_message", cid, body)
    assert first["ok"] and actions.execute("chat-message-000001", "send_message", cid, body) == first
    assert wait_idle(store, cid)["status"] == "done"
    assert actions.execute("chat-message-000001", "send_message", cid, body) == first
    assert len(calls) == 1 and len(store.conversation_messages(cid)) == 2
    pid = Projects(store).create()["id"]
    assert not actions.execute("chat-project-000001", "send_message", "project-" + pid, body)["ok"]
    with TestClient(create_app(cfg.db_path, tmp_path), base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/conversations").json()["total"] == 1
        assert len(client.get(f"/api/v1/conversations/{cid}").json()["messages"]) == 2
        assert client.get(f"/api/v1/conversations/project-{pid}").status_code == 404


def test_public_settings_exclude_secrets_and_controls_cannot_target_another_database(workspace, tmp_path):
    cfg, store, actions = workspace
    setattr(cfg.providers["fake"], "extra_headers", {"Authorization": "do-not-expose-this-secret"})
    with TestClient(create_app(cfg.db_path, tmp_path, config=cfg), base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/session")
        assert "do-not-expose" not in response.text and "extra_headers" not in response.text
        assert response.json()["settings"]["roles"]
    with pytest.raises(ValueError, match="same database"):
        create_app(tmp_path / "other.db", actions=actions)


def test_interactive_demo_journeys_never_call_models_or_start_processes(tmp_path, monkeypatch):
    import httpx
    from swarmforge.webapp.demo import create_demo
    async def no_network(*args, **kwargs):
        raise AssertionError("Demo attempted network model access")
    def no_process(*args, **kwargs):
        raise AssertionError("Demo attempted a process launch")
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    monkeypatch.setattr("subprocess.Popen", no_process)
    cfg, store, actions = create_demo(tmp_path)
    seeded_calls = store._one("SELECT COUNT(*) AS n FROM llm_calls")["n"]
    try:
        with TestClient(create_app(cfg.db_path, actions=actions, demo=True), base_url="http://127.0.0.1") as client:
            headers = authorize(client)
            assert client.get("/api/v1/session").json()["demo_mode"]
            for route in ("work", "projects", "conversations", "queue", "outcomes", "features", "knowledge"):
                assert client.get("/api/v1/" + route).status_code == 200, route
            for view in ("prs", "failures", "history"):
                assert client.get("/api/v1/work?view=" + view).status_code == 200
            chat = {"operation": "send_message", "target": "demo-chat", "args": {"message": "Show current work", "spending_authorized": True}}
            assert client.post("/api/v1/actions", json=chat, headers=headers).json()["ok"]
            assert client.get("/api/v1/conversations/demo-chat").json()["requests"][0]["status"] == "thinking"
            assert wait_idle(store, "demo-chat")["status"] == "done"
            assert "Demo reply" in client.get("/api/v1/conversations/demo-chat").text
            project = next(p for p in client.get("/api/v1/projects").json()["items"] if p["status"] == "drafting")
            pid = project["id"]
            before = client.get(f"/api/v1/projects/{pid}").json()
            headers["idempotency-key"] = "demo-project-chat-001"
            command = {"operation": "project_message", "target": pid, "args": {"message": "Refine the goals", "expected_spec": before["spec_sha"], "spending_authorized": True}}
            assert client.post("/api/v1/actions", json=command, headers=headers).json()["ok"]
            assert Projects(store).get(pid)["status"] == "drafting"
            waiting = next(p for p in client.get("/api/v1/projects").json()["items"] if p["status"] == "waiting_for_you")
            recovery = client.get(f"/api/v1/projects/{waiting['id']}").json()
            headers["idempotency-key"] = "demo-recovery-retry-001"
            retry = {"operation": "retry_project_lead", "target": waiting["id"], "args": {
                "expected_spec": recovery["spec_sha"], "expected_decision": recovery["recovery"]["latest_decision_id"], "spending_authorized": True}}
            assert client.post("/api/v1/actions", json=retry, headers=headers).json()["ok"]
            assert Projects(store).get(waiting["id"])["status"] == "running"
            headers["idempotency-key"] = "demo-worker-start-001"
            assert client.post("/api/v1/actions", json={"operation": "start_queue", "args": {"spending_authorized": True}}, headers=headers).json()["ok"]
            assert store.kv_get("queue_worker_pid") is None
            assert store._one("SELECT COUNT(*) AS n FROM llm_calls")["n"] == seeded_calls  # fabricated usage stays unchanged
    finally:
        store.close()


def test_knowledge_search_uses_opaque_ids_and_read_only_bounded_files(workspace, tmp_path):
    cfg, _, _ = workspace
    root = tmp_path / "vault"
    root.mkdir()
    (root / "Readable note.md").write_text("# Design lesson\n\nKeep queues independent.", encoding="utf-8")
    (root / "Long note.md").write_text("x" * 70000, encoding="utf-8")
    cfg.knowledge.resolved_vault = root
    with TestClient(create_app(cfg.db_path, config=cfg), base_url="http://127.0.0.1") as client:
        page = client.get("/api/v1/knowledge?search=independent").json()
        assert page["total"] == 1
        assert len(page["items"][0]["id"]) == 24
        note = client.get("/api/v1/knowledge/" + page["items"][0]["id"]).json()
        assert "independent" in note["text"] and not note["truncated"]
        assert str(root) not in json.dumps(note)
        all_notes = client.get("/api/v1/knowledge").json()["items"]
        long_note = next(n for n in all_notes if n["title"] == "Long note")
        response = client.get("/api/v1/knowledge/" + long_note["id"])
        assert response.json()["truncated"] and len(response.json()["text"]) == 64000
        assert client.get("/api/v1/knowledge/not-a-note").status_code == 404
    assert (root / "Readable note.md").read_text(encoding="utf-8").endswith("independent.")


def test_retry_control_preserves_the_original_spending_and_deadline(workspace):
    execution = pytest.importorskip("swarmforge.execution", reason="Durable retry support is an independent optional implementation")
    ensure_allowance, snapshot = execution.ensure_allowance, execution.snapshot
    cfg, store, actions = workspace
    store.create_run("retry-origin", state="FAILED", repo="owner/app", budget_usd=3)
    eid = ensure_allowance(store, "retry-origin", cfg)
    store._exec("UPDATE execution_allowances SET spent_usd=1.25 WHERE id=?", (eid,))
    qid = store.queue_add("Retry within allowance", "Acceptance", "owner/app", status="failed", run_id="retry-origin", budget=3)
    before = snapshot(store, "retry-origin")
    result = actions.execute("bounded-retry-000001", "retry_work", f"task-{qid}", {"spending_authorized": True})
    assert result["ok"], result
    queued = store.queue_item(int(result["item_id"][5:]))
    assert queued["execution_id"] == eid
    assert queued["retry_of"] == "retry-origin"
    after = snapshot(store, "retry-origin")
    assert (after["budget_usd"], after["spent_usd"], after["deadline"]) == (before["budget_usd"], before["spent_usd"], before["deadline"])


def test_demo_launcher_does_not_load_live_configuration(monkeypatch):
    from swarmforge.webapp.__main__ import main
    def forbidden(*args, **kwargs):
        raise AssertionError("Demo must not load live configuration")
    observed = []
    def fake_server(app, *, host, port):
        observed.append((host, port))
        with TestClient(app, base_url="http://127.0.0.1") as client:
            assert client.get("/api/v1/session").json()["demo_mode"]
    monkeypatch.setattr("swarmforge.config.load_config", forbidden)
    monkeypatch.setattr("uvicorn.run", fake_server)
    monkeypatch.setattr("sys.argv", ["swarmforge.webapp", "--demo", "--port", "8612"])
    main()
    assert observed == [("127.0.0.1", 8612)]


def test_actions_are_bound_to_the_workspace_the_page_displayed(workspace, tmp_path):
    from swarmforge.webapp.demo import create_demo
    cfg, store, actions = workspace
    (tmp_path / "demo").mkdir()
    demo_cfg, demo_store, demo_actions = create_demo(tmp_path / "demo")
    try:
        with TestClient(create_app(demo_cfg.db_path, actions=demo_actions, demo=True), base_url="http://127.0.0.1") as demo:
            shown = demo.get("/api/v1/session").json()  # the page the user is looking at: demo A
        # The demo is replaced by the real workspace B on the same origin. The old page can still
        # obtain fresh credentials, but its requests name workspace A and are refused.
        with TestClient(create_app(cfg.db_path, actions=actions), base_url="http://127.0.0.1") as real:
            fresh = real.get("/api/v1/session").json()
            assert fresh["workspace_id"] != shown["workspace_id"] and "tmp" not in fresh["workspace_id"]
            stale = {"origin": "http://127.0.0.1", "x-csrf-token": fresh["csrf_token"], "idempotency-key": "stale-request-000001",
                     "x-swarmforge-workspace": shown["workspace_id"]}
            refused = real.post("/api/v1/actions", json={"operation": "pause_queue"}, headers=stale)
            assert refused.status_code == 409 and refused.json()["code"] == "workspace_changed"
            assert real.post("/api/v1/chat/uploads", json={"name": "a.txt", "data": "aGk="}, headers=stale).status_code == 409
            missing = {k: v for k, v in stale.items() if k != "x-swarmforge-workspace"}
            assert real.post("/api/v1/actions", json={"operation": "pause_queue"}, headers=missing).status_code == 409
            assert store.kv_get("queue_paused") != "1"
            ok = {**stale, "x-swarmforge-workspace": fresh["workspace_id"]}
            assert real.post("/api/v1/actions", json={"operation": "pause_queue"}, headers=ok).json()["ok"]
        # Restarting the same workspace keeps its identity (drafts stay usable) but is a new instance.
        with TestClient(create_app(cfg.db_path, actions=DashboardActions(store, cfg)), base_url="http://127.0.0.1") as restarted:
            again = restarted.get("/api/v1/session").json()
            assert again["workspace_id"] == fresh["workspace_id"] and again["instance_id"] != fresh["instance_id"]
    finally:
        demo_store.close()
