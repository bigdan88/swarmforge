"""Dashboard Chat page tests: offline, `swarmforge.chatbot.ask` is monkeypatched."""
import pytest

pytest.importorskip("streamlit")
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