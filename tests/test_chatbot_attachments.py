"""Offline tests for chatbot attachment support and the _cap fix."""

import time

import pytest

from swarmforge.chatbot import (
    MAX_INLINE_ATTACHMENT_BYTES,
    ChatError,
    _cap,
    ask,
    create_conversation,
    status_snapshot,
)
from swarmforge.config import parse_config
from swarmforge.db import Store

from .conftest import FakeProvider, base_raw
from .test_chatbot import _make_run, _make_task


@pytest.fixture
def seeded_store(tmp_path) -> Store:
    store = Store(tmp_path / "db.sqlite")
    _make_run(
        store,
        "run-competed-1234",
        state="COMPLETED",
        state_reason="done",
        finished_at=time.time(),
        created_at=time.time() - 200,
    )
    _make_task(store, "run-competed-1234", "T1", state="SUCCEEDED")
    return store


def _fake():
    return FakeProvider(
        {"qwen-fake": lambda body: {"role": "assistant", "content": "ok, noted"}}
    )


def _cfg(tmp_path, monkeypatch, vision=False):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    if vision:
        raw["vision_models"] = ["qwen-fake"]
    return parse_config(raw)


# ------------------------------------------------------------------------ _cap


def test_cap_strict_limit():
    text = "x" * 5000
    out = _cap(text, 100)
    assert len(out) <= 100
    assert "truncated" in out
    short = "hello"
    assert _cap(short, 100) == short
    assert len(_cap(short, 100)) == len(short)


def test_cap_tiny_limit():
    assert _cap("abcdef", 3) == "abc"


def test_snapshot_capped_strict(tmp_path):
    store = Store(tmp_path / "big.sqlite")
    for i in range(30):
        _make_run(store, f"run-{i:04d}-aaaaaaaaaaaa", issue_title="x" * 400)
    snap = status_snapshot(store, max_runs=30)
    assert len(snap) <= 14_000
    assert "truncated" in snap


# ------------------------------------------------------------- text attachments


def test_ask_inlines_text_attachment(seeded_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    fake = _fake()
    res = ask(
        cfg,
        seeded_store,
        "What is in the file?",
        attachments=[{"name": "notes.md", "data": b"hello attachment"}],
        transport=fake.transport,
    )
    assert res["answer"] == "ok, noted"
    msgs = fake.requests[0]["messages"]
    assert "<status>" in msgs[0]["content"]
    assert any("hello attachment" in str(m.get("content")) for m in msgs)
    assert msgs[-1]["role"] == "user"
    assert "notes.md" in str(msgs[-1]["content"])


def test_ask_no_attachments_regression(seeded_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    fake = _fake()
    ask(cfg, seeded_store, "How many runs?", transport=fake.transport)
    msgs = fake.requests[0]["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system" and "<status>" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "How many runs?"}


def test_oversized_text_attachment_capped(seeded_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    fake = _fake()
    data = b"A" * 20_000
    ask(
        cfg,
        seeded_store,
        "Summarize",
        attachments=[{"name": "big.txt", "data": data}],
        transport=fake.transport,
    )
    content = fake.requests[0]["messages"][-1]["content"]
    assert isinstance(content, str)
    assert "A" * 20_000 not in content
    header = f'Attached file "big.txt" (first {MAX_INLINE_ATTACHMENT_BYTES} bytes):'
    start = content.index(header) + len(header)
    assert len(content[start:]) <= MAX_INLINE_ATTACHMENT_BYTES + 100


# ----------------------------------------------------------- image attachments


def _png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"0" * 64


def test_image_attachment_vision(seeded_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, vision=True)
    fake = _fake()
    ask(
        cfg,
        seeded_store,
        "What is in the picture?",
        attachments=[{"name": "pic.png", "data": _png_bytes()}],
        transport=fake.transport,
    )
    msgs = fake.requests[0]["messages"]
    last = msgs[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    blocks = [b for b in last["content"] if b.get("type") == "image_url"]
    assert blocks and blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert any(
        b.get("type") == "text" and "image attachments below" in b["text"]
        for b in last["content"]
    )


def test_image_attachment_non_vision(seeded_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, vision=False)
    fake = _fake()
    res = ask(
        cfg,
        seeded_store,
        "What is in the picture?",
        attachments=[{"name": "pic.png", "data": _png_bytes()}],
        transport=fake.transport,
    )
    assert res["answer"] == "ok, noted"
    msgs = fake.requests[0]["messages"]
    last = msgs[-1]
    content = last["content"]
    assert not (
        isinstance(content, list) and any(b.get("type") == "image_url" for b in content)
    )
    assert "image attachment(s) were removed" in str(content)


# --------------------------------------------------------------- conversation


def test_conversation_attachments(seeded_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    conv = create_conversation(seeded_store)
    fake = _fake()
    ask(
        cfg,
        seeded_store,
        "Read the file",
        conversation=conv["id"],
        attachments=[{"name": "a.txt", "data": b"conv attachment content"}],
        transport=fake.transport,
    )
    stored = seeded_store.conversation_messages(conv["id"])
    assert any(m["role"] == "user" and m["content"] == "Read the file" for m in stored)
    assert not any("conv attachment content" in m["content"] for m in stored)
    msgs = fake.requests[0]["messages"]
    assert any("conv attachment content" in str(m.get("content")) for m in msgs)


def test_chat_error_without_key(seeded_store, tmp_path, monkeypatch):
    monkeypatch.delenv("FAKE_KEY", raising=False)
    cfg = _cfg(tmp_path, monkeypatch)
    with pytest.raises(ChatError):
        ask(cfg, seeded_store, "hi")
    with pytest.raises(ChatError):
        ask(cfg, seeded_store, "hi", attachments=[{"name": "a.txt", "data": b"x"}])


# --------------------------------------------------------------------- storage


def test_attachments_saved_to_disk(seeded_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    fake = _fake()
    ask(
        cfg,
        seeded_store,
        "q",
        attachments=[{"name": "notes.md", "data": b"data here"}],
        transport=fake.transport,
    )
    files = list((tmp_path / "work" / "chat" / "attachments").rglob("notes.md"))
    assert files and files[0].read_bytes() == b"data here"
