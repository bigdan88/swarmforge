"""Dashboard Chat page tests: offline, `swarmforge.chatbot.ask` is monkeypatched."""
import base64
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
import streamlit as st
from streamlit.testing.v1 import AppTest

from swarmforge.chatbot import ChatError
from swarmforge.db import Store

from .test_dashboard import APP, _go, _write_cfg


def _seed(cfg) -> Store:
    """One finished run in the store the dashboard reads."""
    store = Store(cfg.db_path)
    store.create_run("20990101-000000-aaaaaa", state="COMPLETED", issue_title="Export CSV", repo="x/y",
                     budget_usd=5, state_reason="done", config_json="{}")
    return store


def _open_chat(at):
    _go(at, "Chat")
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_chat_page_renders(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(b.key == "nav_chat" for b in at.button)
    _open_chat(at)
    assert any("Chat: improve SwarmForge and fix failed tasks" in h.value for h in at.header)
    assert len(at.chat_input) == 1
    assert any(b.key == "chat_reset" for b in at.button)


def test_chat_ask_shows_answer_and_sends_history(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)
    calls = []

    def fake_ask(c, store, question, history=None, **kw):
        calls.append({"question": question, "history": list(history or [])})
        return {"answer": "**1 run** completed.", "role": "planner", "model": "qwen-fake",
                "question": question}

    from swarmforge import chatbot
    monkeypatch.setattr(chatbot, "ask", fake_ask)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open_chat(at)
    at.chat_input[0].set_value("how many runs?").run()
    assert not at.exception, [e.value for e in at.exception]
    assert calls == [{"question": "how many runs?", "history": []}]
    texts = [m.value for m in at.markdown]
    assert any("how many runs?" in t for t in texts)
    assert any("**1 run** completed." in t for t in texts)
    assert any("qwen-fake" in c.value for c in at.caption)  # the model that answered
    # a second turn carries the prior conversation
    at.chat_input[0].set_value("and why?").run()
    assert not at.exception, [e.value for e in at.exception]
    assert calls[1]["question"] == "and why?"
    assert calls[1]["history"] == [{"role": "user", "content": "how many runs?"},
                                   {"role": "assistant", "content": "**1 run** completed."}]
    assert at.session_state["chat_history"] == [
        {"role": "user", "content": "how many runs?"},
        {"role": "assistant", "content": "**1 run** completed."},
        {"role": "user", "content": "and why?"},
        {"role": "assistant", "content": "**1 run** completed."},
    ]
    # reset clears the conversation
    at.button(key="chat_reset").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["chat_history"] == []


def test_chat_passes_stable_conversation_id_and_reset_rotates_it(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)
    calls = []

    def fake_ask(c, store, question, history=None, **kw):
        calls.append({"question": question, "conversation": kw.get("conversation")})
        return {"answer": "ok", "role": "planner", "model": "qwen-fake", "question": question}

    from swarmforge import chatbot
    monkeypatch.setattr(chatbot, "ask", fake_ask)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open_chat(at)
    at.chat_input[0].set_value("first question").run()
    assert not at.exception, [e.value for e in at.exception]
    conv1 = calls[0]["conversation"]
    assert isinstance(conv1, str) and conv1
    at.chat_input[0].set_value("second question").run()
    assert not at.exception, [e.value for e in at.exception]
    assert calls[1]["conversation"] == conv1  # stable within one session
    # reset starts a new thread: new conversation id, no history leak
    at.button(key="chat_reset").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["chat_history"] == []
    at.chat_input[0].set_value("third question").run()
    assert not at.exception, [e.value for e in at.exception]
    conv2 = calls[2]["conversation"]
    assert isinstance(conv2, str) and conv2 and conv2 != conv1


def test_chat_caption_shows_conversation_and_cost(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)

    def fake_ask(c, store, question, history=None, **kw):
        return {"answer": "ok", "role": "planner", "model": "qwen-fake", "question": question,
                "conversation": kw.get("conversation"),
                "usage": {"cost_usd": 0.0123, "total_tokens": 123}}

    from swarmforge import chatbot
    monkeypatch.setattr(chatbot, "ask", fake_ask)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open_chat(at)
    at.chat_input[0].set_value("what did it cost?").run()
    assert not at.exception, [e.value for e in at.exception]
    captions = [c.value for c in at.caption]
    assert any("conversation" in c and "$0.0123" in c for c in captions)


def test_chat_caption_degrades_gracefully_without_usage(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)

    def fake_ask(c, store, question, history=None, **kw):
        return {"answer": "ok", "role": "planner", "model": "qwen-fake", "question": question}

    from swarmforge import chatbot
    monkeypatch.setattr(chatbot, "ask", fake_ask)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open_chat(at)
    at.chat_input[0].set_value("no usage here").run()
    assert not at.exception, [e.value for e in at.exception]
    captions = [c.value for c in at.caption]
    assert any("conversation" in c and "$0.0000" in c for c in captions)


def test_chat_error_is_shown_not_raised(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)

    def broken_ask(c, store, question, history=None, **kw):
        raise ChatError("model provider unavailable")

    from swarmforge import chatbot
    monkeypatch.setattr(chatbot, "ask", broken_ask)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open_chat(at)
    at.chat_input[0].set_value("which runs failed?").run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Chatbot: model provider unavailable" in e.value for e in at.error)
    # the question stays in the conversation so the user can retry after it
    assert at.session_state["chat_history"] == [{"role": "user", "content": "which runs failed?"}]

# ------------------------------------------------------- attachments uploads
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeFile:
    """Duck-types streamlit's UploadedFile (name + getvalue)."""

    def __init__(self, name: str, data: bytes):
        self.name = name
        self._data = data

    def getvalue(self) -> bytes:
        return self._data


def _fake_ask_recording(calls):
    def fake_ask(c, store, question, history=None, **kw):
        calls.append({"question": question, "history": list(history or []), "kw": dict(kw)})
        return {"answer": "**2 attachments** seen.", "role": "planner", "model": "qwen-fake",
                "question": question}
    return fake_ask


def test_chat_uploads_attachments_and_renders_them(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)
    calls = []
    from swarmforge import chatbot
    monkeypatch.setattr(chatbot, "ask", _fake_ask_recording(calls))
    monkeypatch.setattr(st, "file_uploader",
                        lambda *a, **k: [FakeFile("pic.png", PNG_1PX),
                                         FakeFile("notes.txt", b"hello notes")])
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open_chat(at)
    at.chat_input[0].set_value("what are these files?").run()
    assert not at.exception, [e.value for e in at.exception]
    # ask was called once with saved attachment paths
    assert len(calls) == 1
    atts = calls[0]["kw"].get("attachments")
    assert atts and len(atts) == 2
    saved_dir = (cfg.workdir / "chat_attachments").resolve()
    assert all(str(saved_dir) in str(Path(p)) for p in atts)
    assert all(Path(p).is_file() for p in atts)
    names = {Path(p).name for p in atts}
    assert names == {"pic.png", "notes.txt"}
    # the transcript renders an image thumbnail and a chip with the .txt name
    assert len(at.image) == 1
    texts = [m.value for m in at.markdown]
    assert any("notes.txt" in t for t in texts)
    assert any("**2 attachments** seen." in t for t in texts)
    # the user message carries attachment metadata for later reruns
    hist = at.session_state["chat_history"]
    assert hist[0].get("attachments") and len(hist[0]["attachments"]) == 2


def test_chat_without_uploads_calls_ask_without_attachments_kwarg(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)
    calls = []
    from swarmforge import chatbot
    monkeypatch.setattr(chatbot, "ask", _fake_ask_recording(calls))
    monkeypatch.setattr(st, "file_uploader", lambda *a, **k: None)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open_chat(at)
    at.chat_input[0].set_value("how many runs?").run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(calls) == 1
    assert "attachments" not in calls[0]["kw"]  # today's exact call signature
    assert calls[0]["kw"].get("conversation")
    assert len(at.image) == 0
    assert at.session_state["chat_history"][0].get("attachments") is None


def test_chat_uploader_failure_degrades_gracefully(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)
    calls = []

    def broken_uploader(*a, **k):
        raise RuntimeError("uploader unsupported")

    from swarmforge import chatbot
    monkeypatch.setattr(chatbot, "ask", _fake_ask_recording(calls))
    monkeypatch.setattr(st, "file_uploader", broken_uploader)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open_chat(at)
    at.chat_input[0].set_value("still works?").run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(calls) == 1
    assert "attachments" not in calls[0]["kw"]
    assert any("still works?" in m.value for m in at.markdown)


def test_chat_tool_status_chip(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)

    def fake_ask(c, store, question, history=None, **kw):
        kw["on_tool"]("fetch_url", {"url": "https://example.com"})
        return {"answer": "Fetched the page.", "role": "planner", "model": "qwen-fake",
                "question": question}

    from swarmforge import chatbot
    monkeypatch.setattr(chatbot, "ask", fake_ask)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open_chat(at)
    at.chat_input[0].set_value("fetch example.com").run()
    assert not at.exception, [e.value for e in at.exception]
    chips = [m.value for m in at.markdown if "🔧 calling" in m.value]
    assert chips, "expected a tool status chip to render"
    assert any("fetch_url" in v for v in chips)
    assert any("Fetched the page." in m.value for m in at.markdown)
