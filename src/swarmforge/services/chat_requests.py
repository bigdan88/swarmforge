"""Background chat replies with visible progress, failures and staged attachments.

The React chat sends a message through the idempotent action journal. The reply then runs in a
background thread so the browser can poll progress (tool steps), see a delayed or failed reply,
and cannot start a second reply in the same conversation while one is pending. Attachments are
staged first through a separate size-bounded upload endpoint and referenced by opaque IDs.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import sqlite3
import inspect
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .. import jobs
from ..db import Store

INSTANCE = jobs.ME.instance  # this process's identity as a job owner

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
STALE_AFTER_S = 60.0  # legacy rows (before jobs): an owner silent this long is gone
JOB_KIND = "chat_reply"
INTERRUPTED = "The web service stopped before this reply finished. Send the message again if you still need it."
MAX_ATTACHMENTS = 6
MAX_STEPS = 40


class ChatBusy(ValueError):
    pass


def ensure_tables(store: Store) -> None:
    store._exec("""CREATE TABLE IF NOT EXISTS chat_requests (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, question TEXT NOT NULL, status TEXT NOT NULL,
        steps TEXT NOT NULL DEFAULT '[]', error TEXT, started_at REAL NOT NULL, finished_at REAL,
        before_message_id INTEGER NOT NULL DEFAULT 0, user_message_id INTEGER, attachments TEXT NOT NULL DEFAULT '[]')""")
    store._exec("CREATE INDEX IF NOT EXISTS chat_requests_conversation ON chat_requests(conversation_id, started_at)")
    columns = {r["name"] for r in store._all("PRAGMA table_info(chat_requests)")}
    for name, kind in (("owner", "TEXT"), ("heartbeat", "REAL"), ("job_id", "TEXT")):
        if name not in columns:
            store._exec(f"ALTER TABLE chat_requests ADD COLUMN {name} {kind}")
    # One pending reply per conversation, enforced by the database across connections and processes.
    store._exec("CREATE UNIQUE INDEX IF NOT EXISTS chat_requests_one_pending ON chat_requests(conversation_id) WHERE status='thinking'")
    store._exec("""CREATE TABLE IF NOT EXISTS chat_uploads (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
        created_at REAL NOT NULL, request_id TEXT)""")
    jobs.ensure_tables(store)


_STATUS = {"succeeded": "done", "failed": "failed", "interrupted": "interrupted", "uncertain": "interrupted",
           "cancelled": "interrupted"}


def _on_transition(conn, job: dict, to_state: str, reason: str) -> None:
    """Keep the browser-visible request in step with its job, in the same transaction."""
    status = _STATUS.get(to_state)
    if status is None:
        return
    error = None
    if status == "failed":
        error = (reason or "The reply failed")[:1000]
    elif status == "interrupted":
        error = INTERRUPTED
    conn.execute("UPDATE chat_requests SET status=?, error=?, finished_at=? WHERE job_id=? AND status='thinking'",
                 (status, error, time.time(), job["id"]))


jobs.register(JOB_KIND, on_transition=_on_transition, recover_to="interrupted")


def _legacy_owner_gone(owner, heartbeat, now: float) -> bool:
    """Rows written before replies were jobs: gone if silent for a minute or its process exited."""
    if owner == INSTANCE:
        return False
    if heartbeat is None or now - heartbeat > STALE_AFTER_S:
        return True
    try:
        pid = int(str(owner).split(":", 1)[0])
    except ValueError:
        return True
    from ..launcher import pid_alive
    return not pid_alive(pid)


def release_interrupted(store: Store, conversation: str | None = None) -> int:
    """Recover replies whose owner has stopped (crashed or restarted server): they become interrupted.
    Replies still owned by a live process, including another web instance, are left alone.
    Nothing is retried automatically: the user decides whether to send again."""
    ensure_tables(store)
    released = len(jobs.recover(store, JOB_KIND, conversation))
    now = time.time()
    query = "SELECT id,owner,heartbeat FROM chat_requests WHERE status='thinking' AND job_id IS NULL" + (" AND conversation_id=?" if conversation else "")
    for row in store._all(query, (conversation,) if conversation else ()):
        if not _legacy_owner_gone(row["owner"], row["heartbeat"], now):
            continue
        cur = store._exec("UPDATE chat_requests SET status='interrupted', finished_at=?, error=? "
                          "WHERE id=? AND status='thinking' AND COALESCE(owner,'')=COALESCE(?,'') AND COALESCE(heartbeat,-1)=COALESCE(?,-1)",
                          (now, INTERRUPTED, row["id"], row["owner"], row["heartbeat"]))
        released += cur.rowcount
    return released


def upload_root(config) -> Path:
    return (Path(config.workdir) / "chat_attachments" / "web").resolve()


def stage_upload(store: Store, config, name: Any, data: Any) -> dict:
    """Validate and store one browser upload; returns public metadata (never the path)."""
    from ..attachments import RASTER_SIGNATURES, TEXT_EXTENSIONS, raster_image_matches, sanitize_name
    if config is None:
        raise ValueError("Load configuration before attaching files")
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ValueError("Give the attachment a file name")
    if not isinstance(data, str) or len(data) > (MAX_UPLOAD_BYTES * 4) // 3 + 8:
        raise ValueError("Attachments are limited to 5 MB each")
    try:
        content = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("The attachment was not encoded correctly") from None
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("Attachments must be between 1 byte and 5 MB")
    safe = sanitize_name(name)
    ext = Path(safe).suffix.lower()
    if ext in RASTER_SIGNATURES:
        if not raster_image_matches(ext, content):
            raise ValueError("The image content does not match its file type")
        kind = "image"
    elif ext in TEXT_EXTENSIONS:
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Text attachments must be UTF-8") from None
        kind = "text"
    else:
        raise ValueError("Attach PNG, JPEG, GIF or WebP images, or text and code files")
    ensure_tables(store)
    upload_id = uuid.uuid4().hex
    root = upload_root(config)
    folder = root / upload_id
    folder.mkdir(parents=True, exist_ok=False)
    path = folder / safe
    path.write_bytes(content)
    store._exec("INSERT INTO chat_uploads(id,name,kind,path,size,created_at) VALUES (?,?,?,?,?,?)",
                (upload_id, safe, kind, str(path), len(content), time.time()))
    return {"id": upload_id, "name": safe, "kind": kind, "size": len(content)}


def _claim_uploads(store: Store, config, ids: Any) -> list[dict]:
    if ids in (None, []):
        return []
    if not isinstance(ids, list) or len(ids) > MAX_ATTACHMENTS or not all(isinstance(i, str) and len(i) == 32 for i in ids):
        raise ValueError(f"Attach at most {MAX_ATTACHMENTS} staged files")
    if len(set(ids)) != len(ids):
        raise ValueError("The same attachment was included twice")
    root = upload_root(config)
    found = []
    for upload_id in ids:
        row = store._one("SELECT * FROM chat_uploads WHERE id=?", (upload_id,))
        if not row or row["request_id"]:
            raise ValueError("An attachment is no longer available. Attach it again.")
        path = Path(row["path"]).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError("An attachment is no longer available. Attach it again.")
        found.append({"id": row["id"], "name": row["name"], "kind": row["kind"], "size": row["size"], "path": path})
    return found


def _saves_question(answer_fn: Callable) -> bool:
    """Adapters that accept question_saved let us store the user's message before the reply starts."""
    return "question_saved" in inspect.signature(answer_fn).parameters


def _call_answer(answer_fn: Callable, config, store, question, conversation, attachments, on_tool, saved: bool):
    params = inspect.signature(answer_fn).parameters
    accepts_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    kwargs: dict[str, Any] = {"conversation": conversation}
    if accepts_any or "on_tool" in params:
        kwargs["on_tool"] = on_tool
    if saved:
        kwargs["question_saved"] = True
    if attachments:
        if not (accepts_any or "attachments" in params):
            raise ValueError("This chat adapter does not accept attachments")
        kwargs["attachments"] = attachments
    return asyncio.run(answer_fn(config, store, question, **kwargs))


def start_reply(store: Store, config, answer_fn: Callable, conversation: str, question: str,
                attachment_ids: Any = None, *, background: bool = True) -> dict:
    """Record a pending reply as a job owned by this process and run it; raises ChatBusy if a
    reply is already active here.

    One transaction creates the job, the browser-visible request, claims the attachments and (when
    the adapter allows) stores the user's message, so a crash can never leave half of them."""
    ensure_tables(store)
    uploads = _claim_uploads(store, config, attachment_ids)
    request_id = uuid.uuid4().hex
    public = [{k: u[k] for k in ("id", "name", "kind", "size")} for u in uploads]
    release_interrupted(store, conversation)  # a dead owner's reply must not block the conversation forever
    saved = _saves_question(answer_fn)
    busy = ChatBusy("A reply is still in progress in this conversation. Wait for it, or open another conversation.")

    def record(conn, job):
        if conn.execute("SELECT 1 FROM chat_requests WHERE conversation_id=? AND status='thinking'", (conversation,)).fetchone():
            raise busy  # a reply from before jobs existed is still running here
        before = conn.execute("SELECT COALESCE(MAX(id),0) FROM chat_messages WHERE conversation_id=?", (conversation,)).fetchone()[0]
        user_id = store.conversation_append(conversation, "user", question) if saved else None
        now = time.time()
        conn.execute("INSERT INTO chat_requests(id,conversation_id,question,status,started_at,before_message_id,attachments,owner,heartbeat,job_id,user_message_id) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (request_id, conversation, question, "thinking", now, before, json.dumps(public),
                                                        INSTANCE, now, job["id"], user_id))
        for u in uploads:
            conn.execute("UPDATE chat_uploads SET request_id=? WHERE id=?", (request_id, u["id"]))

    try:
        job = jobs.create(store, JOB_KIND, conversation, detail={"request_id": request_id}, also=record)
    except (jobs.JobBusy, sqlite3.IntegrityError):
        raise busy from None

    steps: list[dict] = []
    lock = threading.Lock()

    def on_tool(name, args, output):
        with lock:
            if len(steps) < MAX_STEPS:
                ok = not str(output).startswith("ERROR:")
                steps.append({"tool": str(name)[:80], "ok": ok, "at": time.time()})
                store._exec("UPDATE chat_requests SET steps=? WHERE id=?", (json.dumps(steps), request_id))

    def link_user_message(conn, _job):
        if saved:
            return
        # Adapters that store the question themselves: find the first new user message with this text.
        row = conn.execute("SELECT c.id FROM chat_messages c JOIN chat_requests r ON r.id=? WHERE c.conversation_id=? "
                           "AND c.id>r.before_message_id AND c.role='user' AND c.content=? ORDER BY c.id LIMIT 1",
                           (request_id, conversation, question)).fetchone()
        if row:
            conn.execute("UPDATE chat_requests SET user_message_id=? WHERE id=?", (row[0], request_id))

    def run():
        with jobs.keep_alive(store, job["id"]):
            try:
                _call_answer(answer_fn, config, store, question, conversation, [u["path"] for u in uploads], on_tool, saved)
            except Exception as exc:  # noqa: BLE001 - every failure must become visible to the user
                # If the job was recovered meanwhile, finish() changes nothing: that terminal state stands.
                jobs.finish(store, job["id"], "failed", error=(str(exc) or type(exc).__name__)[:1000], also=link_user_message)
            else:
                jobs.finish(store, job["id"], "succeeded", also=link_user_message)

    if background:
        threading.Thread(target=run, name=f"chat-reply-{request_id[:8]}", daemon=True).start()
    else:
        run()
    return {"request_id": request_id, "job_id": job["id"]}


def wait_idle(store: Store, conversation: str, timeout: float = 10.0) -> dict | None:
    """Test/demo helper: wait until no reply is pending in the conversation; returns the latest request."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = store._one("SELECT * FROM chat_requests WHERE conversation_id=? ORDER BY started_at DESC LIMIT 1", (conversation,))
        if not row or row["status"] != "thinking":
            return row
        time.sleep(0.02)
    raise TimeoutError("Chat reply did not finish")
