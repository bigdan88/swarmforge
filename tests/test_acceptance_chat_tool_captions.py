"""Acceptance tests for live tool-call captions on the Chat page.

AC7: while an answer is being prepared, each tool call renders as a caption
like '🔧 fetch_url · https://…' inside the assistant bubble.

Offline AppTest coverage: chatbot.ask is monkeypatched with a fake that invokes
the on_tool callback (or not), and we assert on the rendered captions.
"""

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from swarmforge import chatbot

from .test_dashboard import APP, _go, _write_cfg


def _app(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return cfg, at


def _chat(at):
    _go(at, "Chat")
    assert not at.exception, [e.value for e in at.exception]
    return at


def _fake_ask(monkeypatch, *, with_tool: bool):
    calls = []

    def fake(c, store, question, history=None, **kw):
        calls.append(kw)
        on_tool = kw.get("on_tool")
        if with_tool and on_tool:
            on_tool("fetch_url", {"url": "https://example.com/page"}, "ok")
        return {
            "answer": "**fake answer**",
            "role": "planner",
            "model": "qwen-fake",
            "question": question,
            "actions": {"tasks": [], "fixes": []},
        }

    monkeypatch.setattr(chatbot, "ask", fake)
    return calls


def test_tool_call_renders_as_caption_in_assistant_bubble(tmp_path, monkeypatch):
    """A fake ask() that invokes on_tool produces a '🔧 fetch_url' caption with the URL."""
    _fake_ask(monkeypatch, with_tool=True)
    _app(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _chat(at)
    at.chat_input[0].set_value("what is on that page?").run()
    assert not at.exception, [e.value for e in at.exception]
    captions = [c.value for c in at.caption]
    assert any(
        "🔧 fetch_url" in c and "https://example.com/page" in c for c in captions
    ), captions
    # the answer itself still renders
    assert any("**fake answer**" in m.value for m in at.markdown)


def test_no_caption_when_no_tool_is_called(tmp_path, monkeypatch):
    """A normal (no-tool) answer renders no 🔧 caption."""
    _fake_ask(monkeypatch, with_tool=False)
    _app(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _chat(at)
    at.chat_input[0].set_value("just answer").run()
    assert not at.exception, [e.value for e in at.exception]
    captions = [c.value for c in at.caption]
    assert not any("🔧" in c for c in captions), captions
    assert any("**fake answer**" in m.value for m in at.markdown)


def test_persisted_tool_calls_render_as_captions(tmp_path, monkeypatch):
    """Tool calls persisted with an assistant message render as captions after a fresh load."""
    from swarmforge.db import Store

    cfg = _write_cfg(tmp_path, monkeypatch)
    s = Store(cfg.db_path)
    try:
        conv = chatbot.ensure_default_conversation(s)
        chatbot.add_message(s, conv["id"], "user", "list the files")
        chatbot.add_message(
            s, conv["id"], "assistant", "here is the listing",
            tool_calls=[{"name": "list_files", "args": {"path": "."}, "output": "ok"}],
        )
    finally:
        s.close()
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    _chat(at)  # fresh page load, no run performed
    captions = [c.value for c in at.caption]
    assert any("🔧 list_files" in c and "· ." in c for c in captions), captions
    assert any("here is the listing" in m.value for m in at.markdown)
