"""Offline tests for chatbot attachments (multimodal user messages)."""
import base64

import pytest

from swarmforge.chatbot import (
    answer,
    ask,
    build_user_content,
    create_conversation,
    get_messages,
)
from swarmforge.config import parse_config
from swarmforge.db import Store

from .conftest import FakeProvider, base_raw


def _fake():
    return FakeProvider({"qwen-fake": lambda body: {"role": "assistant",
                                                     "content": "ok, saw the files"}})


def _cfg(tmp_path, monkeypatch, vision: bool = False):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    if vision:
        raw["vision_models"] = ["qwen-fake"]
    return parse_config(raw)


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "db.sqlite")


def _png(path, payload: bytes = b"\x89PNG-fake-data"):
    path.write_bytes(payload)
    return path


def test_build_user_content_no_attachments():
    assert build_user_content("hi?", None) == "hi?"
    assert build_user_content("hi?", []) == "hi?"


def test_build_user_content_blocks(tmp_path):
    png = _png(tmp_path / "shot.png")
    txt = tmp_path / "notes.txt"
    txt.write_text("hello notes", encoding="utf-8")
    other = tmp_path / "blob.bin"
    other.write_bytes(b"\x00\x01")

    blocks = build_user_content("What is this?", [png, txt, other])
    assert isinstance(blocks, list)
    assert blocks[0]["type"] == "text"
    assert "What is this?" in blocks[0]["text"]
    assert "hello notes" in blocks[0]["text"]
    assert "- notes.txt" not in blocks[0]["text"]  # inlined, not just listed
    assert "- shot.png (image)" in blocks[0]["text"]
    assert "- blob.bin (other)" in blocks[0]["text"]
    assert len([b for b in blocks if b["type"] == "image_url"]) == 1
    url = blocks[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"\x89PNG-fake-data"


def test_build_user_content_skips_missing(tmp_path):
    missing = tmp_path / "nope.png"
    blocks = build_user_content("q", [missing])
    assert isinstance(blocks, list)
    assert len(blocks) == 1  # text block only, unreadable image skipped


def test_ask_with_attachments_sends_multimodal(store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, vision=True)
    png = _png(tmp_path / "img.png")
    txt = tmp_path / "data.txt"
    txt.write_text("the answer is 42", encoding="utf-8")
    fake = _fake()
    res = ask(cfg, store, "What do the files say?", attachments=[png, txt],
              transport=fake.transport)
    assert res["answer"] == "ok, saw the files"
    msgs = fake.requests[0]["messages"]
    content = msgs[-1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "What do the files say?" in content[0]["text"]
    assert "the answer is 42" in content[0]["text"]
    img = next(b for b in content if b["type"] == "image_url")
    assert img["image_url"]["url"].startswith("data:image/png;base64,")


def test_ask_without_attachments_plain_string(store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    fake = _fake()
    res = ask(cfg, store, "How many runs?", transport=fake.transport)
    assert res["answer"] == "ok, saw the files"
    msgs = fake.requests[0]["messages"]
    assert msgs[-1] == {"role": "user", "content": "How many runs?"}


def test_conversation_mode_stores_plain_question(store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, vision=True)
    conv = create_conversation(store)["id"]
    png = _png(tmp_path / "pic.png")
    fake = _fake()
    res = ask(cfg, store, "describe this", conversation=conv, attachments=[png],
              transport=fake.transport)
    assert res["conversation"] == conv
    # DB keeps the plain question
    stored = get_messages(store, conv)
    assert stored[-2]["role"] == "user" and stored[-2]["content"] == "describe this"
    # sent payload is the block list
    msgs = fake.requests[0]["messages"]
    content = msgs[-1]["content"]
    assert isinstance(content, list)
    assert any(b["type"] == "image_url" for b in content)


def test_answer_async_attachments(store, tmp_path, monkeypatch):
    import asyncio
    cfg = _cfg(tmp_path, monkeypatch)
    png = _png(tmp_path / "a.png")
    fake = _fake()
    res = asyncio.run(answer(cfg, store, "q?", attachments=[png], transport=fake.transport))
    assert res["answer"] == "ok, saw the files"
