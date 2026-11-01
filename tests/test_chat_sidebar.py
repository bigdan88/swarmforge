"""Chat page conversation sidebar: list, new, select, rename, delete (AppTest, offline)."""
import json
import time

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from swarmforge import chatbot  # noqa: E402
from swarmforge.db import Store  # noqa: E402

from .test_dashboard import APP, _go, _write_cfg  # noqa: E402


def _fake_ask(monkeypatch, answer="**done**"):
    asked = []

    def fake_ask(c, store, question, history=None, **kw):
        asked.append({"question": question, "history": list(history or [])})
        return {"answer": answer, "role": "planner", "model": "qwen-fake",
                "question": question, "actions": {"tasks": [], "fixes": []}}

    monkeypatch.setattr(chatbot, "ask", fake_ask)
    return asked


def _open_chat(at):
    _go(at, "Chat")
    assert not at.exception, [e.value for e in at.exception]
    return at


def _new_app():
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _sel_keys(at):
    return [b.key for b in at.button if (b.key or "").startswith("chat_sel_")]


def _sel_labels(at):
    return [b.label for b in at.button if (b.key or "").startswith("chat_sel_")]


def test_sidebar_lists_conversations_most_recent_first_with_active_marked(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _fake_ask(monkeypatch)
    st = Store(cfg.db_path)
    a = chatbot.create_conversation(st, "Alpha thread")
    beta = chatbot.create_conversation(st, "Beta thread")
    chatbot.add_message(st, a["id"], "user", "alpha question")
    chatbot.add_message(st, beta["id"], "user", "beta question")

    at = _open_chat(_new_app())
    keys = _sel_keys(at)
    assert len(keys) == 2
    # most-recent first: Beta was touched last
    assert keys[0] == f"chat_sel_{beta['id']}" and keys[1] == f"chat_sel_{a['id']}"
    labels = _sel_labels(at)
    assert labels[0].startswith("● ") and "Beta thread" in labels[0]
    assert not labels[1].startswith("● ") and "Alpha thread" in labels[1]
    # rail furniture: new-chat button, per-row edit/delete, relative-time captions
    assert any(b.key == "chat_new" for b in at.button)
    assert any(b.key == f"chat_edit_{a['id']}" for b in at.button)
    assert any(b.key == f"chat_del_{beta['id']}" for b in at.button)
    assert any("just now" in c.value for c in at.caption)


def test_new_chat_creates_and_selects_empty_conversation(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _fake_ask(monkeypatch)
    st = Store(cfg.db_path)
    a = chatbot.create_conversation(st, "Alpha thread")
    chatbot.add_message(st, a["id"], "user", "hello")

    at = _open_chat(_new_app())
    at.button(key="chat_new").click().run()
    assert not at.exception, [e.value for e in at.exception]
    labels = _sel_labels(at)
    assert len(labels) == 2
    assert labels[0].startswith("● ") and "New chat" in labels[0]
    assert at.session_state["chat_history"] == []
    # the new conversation is the active one and has no messages in the DB
    active = at.session_state["chat_conv_id"]
    assert active != a["id"]
    assert chatbot.get_messages(Store(cfg.db_path), active) == []


def test_selecting_a_conversation_swaps_the_transcript(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _fake_ask(monkeypatch)
    st = Store(cfg.db_path)
    a = chatbot.create_conversation(st, "Alpha thread")
    beta = chatbot.create_conversation(st, "Beta thread")
    chatbot.add_message(st, a["id"], "user", "alpha question")
    chatbot.add_message(st, a["id"], "assistant", "alpha answer")
    chatbot.add_message(st, beta["id"], "user", "beta question")

    at = _open_chat(_new_app())
    assert at.session_state["chat_history"] == [{"role": "user", "content": "beta question"}]
    at.button(key=f"chat_sel_{a['id']}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["chat_conv_id"] == a["id"]
    assert at.session_state["chat_history"] == [
        {"role": "user", "content": "alpha question"},
        {"role": "assistant", "content": "alpha answer"},
    ]
    texts = [m.value for m in at.markdown]
    assert any("alpha answer" in t for t in texts)
    assert not any("beta question" in t for t in texts)
    # the active marker moved
    labels = _sel_labels(at)
    assert labels[1].startswith("● ") and "Alpha thread" in labels[1]


def test_rename_and_delete_update_the_sidebar_immediately(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _fake_ask(monkeypatch)
    st = Store(cfg.db_path)
    a = chatbot.create_conversation(st, "Alpha thread")
    beta = chatbot.create_conversation(st, "Beta thread")

    at = _open_chat(_new_app())
    # rename Alpha inline
    at.button(key=f"chat_edit_{a['id']}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    at.text_input(key=f"chat_rename_{a['id']}").set_value("Renamed title").run()
    at.button(key=f"chat_rename_save_{a['id']}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    labels = _sel_labels(at)
    assert any("Renamed title" in l for l in labels)
    assert not any("Alpha thread" in l for l in labels)
    conv = chatbot.get_conversation(Store(cfg.db_path), a["id"])
    assert conv["title"] == "Renamed title"
    # renaming bumped updated_at, so Alpha moved to the front; the active marker
    # stays on the selected conversation (Beta)
    assert labels[0] == "Renamed title"
    assert labels[1].startswith("● ") and "Beta thread" in labels[1]

    # delete the non-active conversation: its row disappears at once
    at.button(key=f"chat_del_{beta['id']}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert _sel_keys(at) == [f"chat_sel_{a['id']}"]
    assert chatbot.get_conversation(Store(cfg.db_path), beta["id"]) is None

    # delete the active one: the remaining most-recent conversation takes over
    c = chatbot.create_conversation(Store(cfg.db_path), "Gamma thread")
    at.button(key=f"chat_del_{a['id']}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert _sel_keys(at) == [f"chat_sel_{c['id']}"]
    assert at.session_state["chat_conv_id"] == c["id"]
    assert chatbot.get_conversation(Store(cfg.db_path), a["id"]) is None


def test_messages_persist_to_the_active_conversation_across_reload(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    asked = _fake_ask(monkeypatch, answer="**42 runs**")
    st = Store(cfg.db_path)
    a = chatbot.create_conversation(st, "Alpha thread")

    at = _open_chat(_new_app())
    at.button(key=f"chat_sel_{a['id']}").click().run()
    at.chat_input[0].set_value("how many runs?").run()
    assert not at.exception, [e.value for e in at.exception]
    assert asked[0]["question"] == "how many runs?"
    msgs = chatbot.get_messages(Store(cfg.db_path), a["id"])
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "how many runs?"), ("assistant", "**42 runs**")]

    # a fresh app on the same DB restores the thread from the store
    at2 = _open_chat(_new_app())
    assert at2.session_state["chat_conv_id"] == a["id"]
    assert at2.session_state["chat_history"] == [
        {"role": "user", "content": "how many runs?"},
        {"role": "assistant", "content": "**42 runs**"},
    ]
    assert any("**42 runs**" in m.value for m in at2.markdown)

    # reset empties the active conversation in the DB too
    at2.button(key="chat_reset").click().run()
    assert not at2.exception, [e.value for e in at2.exception]
    assert at2.session_state["chat_history"] == []
    assert chatbot.get_messages(Store(cfg.db_path), a["id"]) == []


def test_legacy_flat_history_migrates_into_general(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _fake_ask(monkeypatch)
    st = Store(cfg.db_path)
    st.kv_set("chat_history_v1", json.dumps({
        "history": [{"role": "user", "content": "old question"},
                    {"role": "assistant", "content": "old answer"}],
        "actions": {"1": {"tasks": [], "fixes": []}}}))

    at = _open_chat(_new_app())
    assert not at.exception, [e.value for e in at.exception]
    conv = chatbot.ensure_default_conversation(Store(cfg.db_path))
    assert conv["title"] == "General"
    assert at.session_state["chat_history"] == [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    assert st.kv_get("chat_history_v1") is None
    assert json.loads(st.kv_get(f"chat_actions_v1:{conv['id']}") or "{}") == {"1": {"tasks": [], "fixes": []}}
    # migration happens once: a second app run does not duplicate the messages
    at2 = _open_chat(_new_app())
    assert len(at2.session_state["chat_history"]) == 2
    assert len(chatbot.get_messages(Store(cfg.db_path), conv["id"])) == 2


def test_relative_time_formats():
    from swarmforge.dashboard import _chat_rel_time
    now = time.time()
    assert _chat_rel_time(now - 10) == "just now"
    assert _chat_rel_time(now - 120) == "2 min ago"
    assert _chat_rel_time(now - 2 * 3600) == "2 h ago"
    assert _chat_rel_time(now - 2 * 86400) == "2 d ago"
    assert _chat_rel_time(now - 30 * 86400) == time.strftime("%Y-%m-%d", time.localtime(now - 30 * 86400))
