"""Acceptance tests for the Chat page tools on/off toggle and the demo seed.

- The Chat page renders an st.toggle labelled 'Tools', defaulting to
  cfg.chat.tools_enabled.
- Flipping the toggle off makes chatbot.ask receive a cfg whose
  chat.tools_enabled is False (and True when flipped back on), without
  mutating the cached global config.
- The demo seed lands in the default conversation, so a '🔧 list_files'
  caption is visible on the Chat page immediately after load.
"""

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from swarmforge import chatbot
from swarmforge.db import Store

from .test_dashboard import APP, _go, _write_cfg


def _app(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _chat(at):
    _go(at, "Chat")
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_chat_page_has_tools_toggle(tmp_path, monkeypatch):
    """The Chat topbar has a toggle labelled 'Tools', defaulting to True."""
    at = _app(tmp_path, monkeypatch)
    _chat(at)
    assert len(at.toggle) >= 1
    tools = [t for t in at.toggle if "Tools" in t.label]
    assert tools, [t.label for t in at.toggle]
    assert tools[0].value is True


def test_toggle_off_disables_tools_for_next_question(tmp_path, monkeypatch):
    """chatbot.ask receives a cfg whose chat.tools_enabled follows the toggle."""
    recorded = []

    def fake(cfg, store, question, history=None, **kw):
        recorded.append(cfg)
        return {
            "answer": "ok",
            "role": "planner",
            "model": "qwen-fake",
            "question": question,
            "actions": {"tasks": [], "fixes": []},
        }

    monkeypatch.setattr(chatbot, "ask", fake)
    at = _app(tmp_path, monkeypatch)
    _chat(at)
    tools = [t for t in at.toggle if "Tools" in t.label]
    assert tools

    tools[0].set_value(False).run()
    assert not at.exception, [e.value for e in at.exception]
    at.chat_input[0].set_value("hello without tools").run()
    assert not at.exception, [e.value for e in at.exception]
    assert recorded and recorded[-1].chat.tools_enabled is False

    tools = [t for t in at.toggle if "Tools" in t.label]
    tools[0].set_value(True).run()
    assert not at.exception, [e.value for e in at.exception]
    at.chat_input[0].set_value("hello with tools").run()
    assert not at.exception, [e.value for e in at.exception]
    assert recorded[-1].chat.tools_enabled is True


def test_demo_chat_page_shows_tool_caption_on_load(tmp_path, monkeypatch):
    """A seeded default conversation shows a '🔧 list_files' caption on load."""
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    conv = chatbot.ensure_default_conversation(store)
    chatbot.add_message(store, conv["id"], "user", "What files are in the repo?")
    chatbot.add_message(
        store,
        conv["id"],
        "assistant",
        "The repository contains the README and the `src/` source tree.",
        tool_calls=[
            {"name": "list_files", "args": {"path": "."}, "output": "README.md\nsrc/"}
        ],
    )
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _chat(at)
    captions = [c.value for c in at.caption]
    assert any("🔧 list_files" in c for c in captions), captions
