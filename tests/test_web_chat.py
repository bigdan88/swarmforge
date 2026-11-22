"""React chat: background replies with progress, visible failures, one pending reply, attachments."""
import asyncio
import base64
import threading

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from swarmforge.db import Store
from swarmforge.services.chat_requests import ChatBusy, release_interrupted, stage_upload, start_reply, wait_idle
from swarmforge.services.dashboard import DashboardActions
from swarmforge.webapp.app import create_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def authorize(client, key="chat-request-0000001"):
    token = client.get("/api/v1/session").json()["csrf_token"]
    return {"origin": "http://127.0.0.1", "x-csrf-token": token, "idempotency-key": key}


@pytest.fixture
def chat(cfg, tmp_path):
    store = Store(cfg.db_path)
    store.conversation_create("c1", "Test chat")
    yield cfg, store
    store.close()


def gated_answer(gate, calls):
    async def answer(config, store, question, *, conversation, attachments=None, on_tool=None):
        calls.append({"question": question, "attachments": attachments})
        store.conversation_append(conversation, "user", question)
        on_tool("status_snapshot", {}, "ok")
        while not gate.is_set():
            await asyncio.sleep(0.01)
        store.conversation_append(conversation, "assistant", "Answer")
        return {"answer": "Answer"}
    return answer


def test_reply_progress_is_visible_and_a_second_send_is_refused_until_it_finishes(chat, tmp_path):
    cfg, store = chat
    gate, calls = threading.Event(), []
    actions = DashboardActions(store, cfg, chat_answer=gated_answer(gate, calls))
    body = {"message": "What is running?", "spending_authorized": True}
    with TestClient(create_app(cfg.db_path, tmp_path, actions=actions), base_url="http://127.0.0.1") as client:
        assert client.post("/api/v1/actions", json={"operation": "send_message", "target": "c1", "args": body},
                           headers=authorize(client)).json()["ok"]
        for _ in range(200):
            req = client.get("/api/v1/conversations/c1").json()["requests"][0]
            if req["steps"]:
                break
            asyncio.run(asyncio.sleep(0.01))
        assert req["status"] == "thinking" and req["steps"][0]["tool"] == "status_snapshot"
        second = client.post("/api/v1/actions", json={"operation": "send_message", "target": "c1", "args": {**body, "message": "Again"}},
                             headers=authorize(client, "chat-request-0000002")).json()
        assert not second["ok"] and "still in progress" in second["message"]
        gate.set()
        assert wait_idle(store, "c1")["status"] == "done"
        view = client.get("/api/v1/conversations/c1").json()
        assert [m["content"] for m in view["messages"]] == ["What is running?", "Answer"]
    assert len(calls) == 1


def test_failed_reply_is_recorded_and_the_conversation_can_continue(chat):
    cfg, store = chat
    async def broken(config, store, question, *, conversation):
        store.conversation_append(conversation, "user", question)
        raise RuntimeError("provider returned HTTP 503")
    start_reply(store, cfg, broken, "c1", "Hello?", background=False)
    row = wait_idle(store, "c1")
    assert row["status"] == "failed" and "HTTP 503" in row["error"] and row["user_message_id"]
    gate = threading.Event()
    gate.set()
    start_reply(store, cfg, gated_answer(gate, []), "c1", "Retry", background=False)
    assert wait_idle(store, "c1")["status"] == "done"


def test_interrupted_reply_is_released_when_the_service_restarts(chat):
    cfg, store = chat
    store._exec("CREATE TABLE IF NOT EXISTS chat_requests (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, question TEXT NOT NULL, status TEXT NOT NULL, "
                "steps TEXT NOT NULL DEFAULT '[]', error TEXT, started_at REAL NOT NULL, finished_at REAL, before_message_id INTEGER NOT NULL DEFAULT 0, "
                "user_message_id INTEGER, attachments TEXT NOT NULL DEFAULT '[]')")
    store._exec("INSERT INTO chat_requests(id,conversation_id,question,status,started_at) VALUES ('x','c1','q','thinking',1)")
    with pytest.raises(ChatBusy):
        start_reply(store, cfg, gated_answer(threading.Event(), []), "c1", "blocked", background=False)
    assert release_interrupted(store) == 1
    assert store._one("SELECT status FROM chat_requests WHERE id='x'")["status"] == "interrupted"


def test_uploads_are_validated_confined_and_attached_once(chat, tmp_path):
    cfg, store = chat
    enc = lambda b: base64.b64encode(b).decode()
    with pytest.raises(ValueError, match="does not match"):
        stage_upload(store, cfg, "shot.png", enc(b"not a png"))
    with pytest.raises(ValueError, match="images, or text"):
        stage_upload(store, cfg, "drawing.svg", enc(b"<svg onload='x'/>"))
    with pytest.raises(ValueError, match="UTF-8"):
        stage_upload(store, cfg, "notes.txt", enc(b"\xff\xfe\x00bad"))
    with pytest.raises(ValueError, match="5 MB"):
        stage_upload(store, cfg, "big.txt", enc(b"a" * (5 * 1024 * 1024 + 1)))
    image = stage_upload(store, cfg, "../../shot.png", enc(PNG))
    notes = stage_upload(store, cfg, "notes.txt", enc(b"hello"))
    assert image["name"] == "shot.png" and "path" not in image
    gate, calls = threading.Event(), []
    gate.set()
    start_reply(store, cfg, gated_answer(gate, calls), "c1", "What do these show?", [image["id"], notes["id"]], background=False)
    paths = calls[0]["attachments"]
    assert [p.name for p in paths] == ["shot.png", "notes.txt"] and all(p.is_file() for p in paths)
    with pytest.raises(ValueError, match="no longer available"):
        start_reply(store, cfg, gated_answer(gate, []), "c1", "Again", [image["id"]], background=False)
    with TestClient(create_app(cfg.db_path, tmp_path, config=cfg), base_url="http://127.0.0.1") as client:
        view = client.get("/api/v1/conversations/c1").json()
        user = next(m for m in view["messages"] if m["role"] == "user")
        assert [a["name"] for a in user["attachments"]] == ["shot.png", "notes.txt"]
        shot = client.get(f"/api/v1/conversations/c1/attachments/{image['id']}")
        assert shot.status_code == 200 and shot.headers["content-type"] == "image/png" and shot.content == PNG
        assert client.get(f"/api/v1/conversations/c1/attachments/{notes['id']}").status_code == 404
        store.conversation_create("c2", "Other")
        assert client.get(f"/api/v1/conversations/c2/attachments/{image['id']}").status_code == 404


def test_upload_endpoint_requires_controls_session_and_bounds_size(chat, tmp_path):
    cfg, store = chat
    payload = {"name": "notes.md", "data": base64.b64encode(b"# hi").decode()}
    with TestClient(create_app(cfg.db_path, tmp_path, config=cfg), base_url="http://127.0.0.1") as readonly:
        assert readonly.post("/api/v1/chat/uploads", json=payload).status_code == 405
    actions = DashboardActions(store, cfg)
    with TestClient(create_app(cfg.db_path, tmp_path, actions=actions), base_url="http://127.0.0.1") as client:
        assert client.post("/api/v1/chat/uploads", json=payload).status_code == 403
        headers = authorize(client)
        assert client.post("/api/v1/chat/uploads", json=payload, headers={**headers, "content-length": "8000000"}).status_code == 413
        made = client.post("/api/v1/chat/uploads", json=payload, headers=headers).json()
        assert made["kind"] == "text" and len(made["id"]) == 32
        # The action endpoint keeps its small request limit.
        assert client.post("/api/v1/actions", json={"operation": "pause_queue"}, headers={**headers, "content-length": "200000"}).status_code == 413
