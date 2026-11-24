"""React chat: background replies with progress, visible failures, one pending reply, attachments."""
import asyncio
import base64
import json
import threading

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from swarmforge.db import Store
from swarmforge.services.chat_requests import ChatBusy, release_interrupted, stage_upload, start_reply, wait_idle
from swarmforge.services.dashboard import DashboardActions
from swarmforge.webapp.app import create_app


def test_generated_tasks_survive_reload_and_launch_once_through_normal_queue(chat, tmp_path, monkeypatch):
    from swarmforge import chatbot
    cfg, store = chat
    calls, starts = [], []

    async def fake_model(*args, **kwargs):
        calls.append(True)
        return 'Here is a task.\n```actions\n' + json.dumps({"tasks": [{
            "title": "Export inventory", "description": "Add CSV export.\nAcceptance: exported rows match the list.",
            "repo": "owner/app", "why": "Requested in this chat"}]}) + '\n```'

    monkeypatch.setattr(chatbot, "_run_chat", fake_model)
    actions = DashboardActions(store, cfg, chat_background=False, worker_start=lambda *_: starts.append(True))
    with TestClient(create_app(cfg.db_path, tmp_path, actions=actions), base_url="http://127.0.0.1") as client:
        sent = client.post("/api/v1/actions", json={"operation": "send_message", "target": "c1", "args": {"message": "Generate a task"}},
                           headers=authorize(client)).json()
        assert sent["ok"] and len(calls) == 1
        message = client.get("/api/v1/conversations/c1").json()["messages"][-1]
        assert message["content"] == "Here is a task."
        assert message["actions"]["tasks"][0]["title"] == "Export inventory"
        assert message["launched_tasks"] == {} and not store.queue_items() and not starts
        args = {"index": 0, "title": "Export inventory", "body": "Reviewed CSV acceptance criteria", "repo": "owner/app", "budget": 2}

        def launch(key, **changes):
            return client.post("/api/v1/actions", json={"operation": "launch_chat_task", "target": str(message["id"]), "args": {**args, **changes}},
                               headers=authorize(client, key)).json()

        assert not launch("chat-launch-noauth01")["ok"]
        assert not launch("chat-launch-badindex", index=5, spending_authorized=True)["ok"]
        assert not launch("chat-launch-badlimit", budget=1000, spending_authorized=True)["ok"]
        assert not store.queue_items() and not starts
        result = launch("chat-launch-reviewed", spending_authorized=True)
        assert result["ok"] and result["item_id"]
        assert launch("chat-launch-reviewed", spending_authorized=True) == result
        assert launch("chat-launch-new-key1", spending_authorized=True)["item_id"] == result["item_id"]
        assert len(store.queue_items()) == 1
        item = store.queue_items()[0]
        assert item["approval"] == "dashboard" and item["budget"] == 2
        assert item["body"] == args["body"] and item["max_runtime_s"] == cfg.limits.max_runtime_s
        assert starts
        reloaded = client.get("/api/v1/conversations/c1").json()["messages"][-1]
        assert reloaded["launched_tasks"] == {"0": result["item_id"]}
        assert reloaded["actions"] == message["actions"]
        assert store._one("SELECT COUNT(*) AS n FROM llm_calls")["n"] == 0


def test_worker_start_failure_keeps_reviewed_task_and_prevents_duplicate_launch(chat):
    cfg, store = chat
    mid = store.conversation_append("c1", "assistant", "Proposal", actions={"tasks": [{"title": "One task"}]})
    def broken(*_):
        raise OSError("Cannot start process")
    actions = DashboardActions(store, cfg, worker_start=broken)
    args = {"index": 0, "title": "One task", "body": "Acceptance", "repo": "owner/app", "budget": 2, "spending_authorized": True}
    result = actions.execute("chat-worker-failed01", "launch_chat_task", str(mid), args)
    assert result["ok"] and "worker could not start" in result["message"]
    assert actions.execute("chat-worker-retry001", "launch_chat_task", str(mid), args)["item_id"] == result["item_id"]
    assert len(store.queue_items()) == 1

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def authorize(client, key="chat-request-0000001"):
    session = client.get("/api/v1/session").json()
    return {"origin": "http://127.0.0.1", "x-csrf-token": session["csrf_token"], "idempotency-key": key,
            "x-swarmforge-workspace": session["workspace_id"]}


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


def wait_for(predicate, timeout=5):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_another_live_instance_never_interrupts_a_running_reply(chat):
    cfg, store = chat
    gate, calls = threading.Event(), []
    first = DashboardActions(store, cfg, chat_answer=gated_answer(gate, calls))
    assert first.execute("owner-send-00000001", "send_message", "c1", {"message": "Long question"})["ok"]
    assert wait_for(lambda: calls)
    other_store = Store(cfg.db_path)  # a second web instance: its own connection and service object
    try:
        DashboardActions(other_store, cfg, chat_answer=gated_answer(gate, []))
        assert store._one("SELECT status FROM chat_requests")["status"] == "thinking"
        with pytest.raises(ChatBusy):
            start_reply(other_store, cfg, gated_answer(gate, []), "c1", "Interleaved?", background=False)
    finally:
        other_store.close()
    gate.set()
    assert wait_idle(store, "c1")["status"] == "done"
    assert [m["content"] for m in store.conversation_messages("c1")] == ["Long question", "Answer"]


def _pending(store, owner, heartbeat):
    from swarmforge.services.chat_requests import ensure_tables
    ensure_tables(store)
    store._exec("INSERT INTO chat_requests(id,conversation_id,question,status,started_at,owner,heartbeat) VALUES ('x','c1','q','thinking',1,?,?)",
                (owner, heartbeat))


@pytest.mark.parametrize("owner,age,released", [
    ("2147480000:gone", 0, True),          # owner process no longer exists
    (None, None, True),                     # replies recorded before ownership existed
    ("{pid}:other", 0, False),              # another live instance with a fresh heartbeat
    ("{pid}:other", 3600, True),            # live process, but it stopped heartbeating long ago
])
def test_recovery_releases_only_replies_whose_owner_is_gone(chat, owner, age, released):
    import os
    import time
    cfg, store = chat
    _pending(store, owner and owner.format(pid=os.getpid()), None if age is None else time.time() - age)
    assert release_interrupted(store) == int(released)
    assert store._one("SELECT status FROM chat_requests WHERE id='x'")["status"] == ("interrupted" if released else "thinking")


def test_a_dead_owners_reply_does_not_block_the_conversation_or_get_resent(chat):
    cfg, store = chat
    _pending(store, "2147480000:gone", __import__("time").time())
    calls = []
    gate = threading.Event()
    gate.set()
    start_reply(store, cfg, gated_answer(gate, calls), "c1", "Fresh question", background=False)
    assert [c["question"] for c in calls] == ["Fresh question"]  # the interrupted question is not replayed
    assert store._one("SELECT status FROM chat_requests WHERE id='x'")["status"] == "interrupted"


def test_a_late_completion_cannot_overwrite_a_recovered_reply(chat):
    cfg, store = chat
    gate, calls = threading.Event(), []
    start_reply(store, cfg, gated_answer(gate, calls), "c1", "Slow", background=True)
    assert wait_for(lambda: calls)
    store._exec("UPDATE chat_requests SET status='interrupted', finished_at=1")  # recovered meanwhile
    gate.set()
    assert wait_for(lambda: len(store.conversation_messages("c1")) == 2)
    import time
    time.sleep(0.1)
    assert store._one("SELECT status FROM chat_requests")["status"] == "interrupted"


def test_one_pending_reply_is_enforced_by_the_database(chat):
    import sqlite3
    cfg, store = chat
    _pending(store, "{}:x".format(__import__("os").getpid()), __import__("time").time())
    with pytest.raises(sqlite3.IntegrityError):
        store._exec("INSERT INTO chat_requests(id,conversation_id,question,status,started_at) VALUES ('y','c1','q','thinking',1)")


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


def test_sending_is_the_consent_for_one_reply_but_bigger_spending_still_needs_approval(chat):
    cfg, store = chat
    calls = []
    gate = threading.Event()
    gate.set()
    actions = DashboardActions(store, cfg, chat_answer=gated_answer(gate, calls), chat_background=False)
    assert actions.execute("plain-send-00000001", "send_message", "c1", {"message": "Just send it"})["ok"]
    assert calls[0]["question"] == "Just send it"
    # Queueing paid work, starting the worker and retries keep their explicit confirmation.
    task = {"title": "T", "body": "B", "repo": "owner/app", "budget": 2}
    assert not actions.execute("plain-queue-0000001", "queue_task", "", task)["ok"]
    assert not actions.execute("plain-start-0000001", "start_queue", "", {})["ok"]


KILLED_REPLY = """
import asyncio, sys
from pathlib import Path
from swarmforge.db import Store
from swarmforge.services.chat_requests import start_reply
store = Store(Path(sys.argv[1]))
store.conversation_create("k1", "Kill test")
async def answer(config, store, question, *, conversation, question_saved=False):
    print("started", flush=True)
    await asyncio.sleep(60)
start_reply(store, None, answer, "k1", "Keep my message", background=False)
"""


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="SIGKILL")
def test_a_killed_web_process_loses_no_message_and_frees_the_conversation(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    script = tmp_path / "reply.py"
    script.write_text(KILLED_REPLY)
    db = tmp_path / "kill.db"
    proc = subprocess.Popen([sys.executable, str(script), str(db)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "started"
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()
    store = Store(db)
    try:
        # The question was stored with the job, before the reply began: nothing to retype.
        assert [m["content"] for m in store.conversation_messages("k1")] == ["Keep my message"]
        request = store._one("SELECT * FROM chat_requests")
        assert request["status"] == "thinking" and request["user_message_id"]
        assert release_interrupted(store, "k1") == 1  # recovered at once: its process is gone
        request = store._one("SELECT * FROM chat_requests")
        assert request["status"] == "interrupted" and "Send the message again" in request["error"]
        from swarmforge import jobs
        assert jobs.get(store, request["job_id"])["state"] == "interrupted"
        gate = threading.Event()
        gate.set()
        start_reply(store, None, gated_answer(gate, []), "k1", "Next", background=False)
        assert wait_idle(store, "k1")["status"] == "done"
    finally:
        store.close()
