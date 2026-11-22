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
import inspect
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..db import Store

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
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
    store._exec("""CREATE TABLE IF NOT EXISTS chat_uploads (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
        created_at REAL NOT NULL, request_id TEXT)""")


def release_interrupted(store: Store) -> int:
    """Replies owned by a previous server process can never finish; make that visible."""
    ensure_tables(store)
    with store._lock, store.conn:
        cur = store.conn.execute("UPDATE chat_requests SET status='interrupted', finished_at=?, "
                                 "error='The web service stopped before this reply finished. Send the message again if you still need it.' "
                                 "WHERE status='thinking'", (time.time(),))
        return cur.rowcount


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


def _call_answer(answer_fn: Callable, config, store, question, conversation, attachments, on_tool):
    params = inspect.signature(answer_fn).parameters
    accepts_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    kwargs: dict[str, Any] = {"conversation": conversation}
    if accepts_any or "on_tool" in params:
        kwargs["on_tool"] = on_tool
    if attachments:
        if not (accepts_any or "attachments" in params):
            raise ValueError("This chat adapter does not accept attachments")
        kwargs["attachments"] = attachments
    return asyncio.run(answer_fn(config, store, question, **kwargs))


def start_reply(store: Store, config, answer_fn: Callable, conversation: str, question: str,
                attachment_ids: Any = None, *, background: bool = True) -> dict:
    """Record a pending reply and run it; raises ChatBusy if one is already pending here."""
    ensure_tables(store)
    uploads = _claim_uploads(store, config, attachment_ids)
    request_id = uuid.uuid4().hex
    public = [{k: u[k] for k in ("id", "name", "kind", "size")} for u in uploads]
    with store._lock, store.conn:
        store.conn.execute("BEGIN IMMEDIATE")
        if store.conn.execute("SELECT 1 FROM chat_requests WHERE conversation_id=? AND status='thinking'", (conversation,)).fetchone():
            raise ChatBusy("A reply is still in progress in this conversation. Wait for it, or open another conversation.")
        before = store.conn.execute("SELECT COALESCE(MAX(id),0) FROM chat_messages WHERE conversation_id=?", (conversation,)).fetchone()[0]
        store.conn.execute("INSERT INTO chat_requests(id,conversation_id,question,status,started_at,before_message_id,attachments) "
                           "VALUES (?,?,?,?,?,?,?)", (request_id, conversation, question, "thinking", time.time(), before, json.dumps(public)))
        for u in uploads:
            store.conn.execute("UPDATE chat_uploads SET request_id=? WHERE id=?", (request_id, u["id"]))

    steps: list[dict] = []
    lock = threading.Lock()

    def on_tool(name, args, output):
        with lock:
            if len(steps) < MAX_STEPS:
                ok = not str(output).startswith("ERROR:")
                steps.append({"tool": str(name)[:80], "ok": ok, "at": time.time()})
                store._exec("UPDATE chat_requests SET steps=? WHERE id=?", (json.dumps(steps), request_id))

    def finish(status: str, error: str | None):
        user = store._one("SELECT id FROM chat_messages WHERE conversation_id=? AND id>? AND role='user' AND content=? ORDER BY id LIMIT 1",
                          (conversation, before, question))
        store._exec("UPDATE chat_requests SET status=?,error=?,finished_at=?,user_message_id=? WHERE id=?",
                    (status, error, time.time(), user["id"] if user else None, request_id))

    def run():
        try:
            _call_answer(answer_fn, config, store, question, conversation, [u["path"] for u in uploads], on_tool)
        except Exception as exc:  # noqa: BLE001 - every failure must become visible to the user
            finish("failed", (str(exc) or type(exc).__name__)[:1000])
        else:
            finish("done", None)

    if background:
        threading.Thread(target=run, name=f"chat-reply-{request_id[:8]}", daemon=True).start()
    else:
        run()
    return {"request_id": request_id}


def wait_idle(store: Store, conversation: str, timeout: float = 10.0) -> dict | None:
    """Test/demo helper: wait until no reply is pending in the conversation; returns the latest request."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = store._one("SELECT * FROM chat_requests WHERE conversation_id=? ORDER BY started_at DESC LIMIT 1", (conversation,))
        if not row or row["status"] != "thinking":
            return row
        time.sleep(0.02)
    raise TimeoutError("Chat reply did not finish")
