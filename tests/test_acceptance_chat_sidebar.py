"""Acceptance tests for the Chat-page conversation sidebar (issue: "dashboard.py Chat
page: Claude-style conversation sidebar (list, new, select, rename, delete)").

Offline AppTest coverage, one test per acceptance criterion:
  AC1 rail lists all conversations most-recent-first with title + relative time, active marked
  AC2 '+ New chat' starts an empty conversation and selects it
  AC3 selecting swaps the transcript (loaded from the DB); new messages persist to the active one
  AC4 inline rename / delete go through the chatbot CRUD helpers and update the rail at once
  AC5 (tests/test_chat_sidebar.py exercises new/select/rename/delete and the existing dashboard
      chat tests keep passing) is verified by running those files.
Per the AppTest lesson every assertion here uses Button.key, never Button.proto.key.
"""
import time

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from swarmforge import chatbot
from swarmforge.db import Store

from .test_dashboard import APP, _go, _write_cfg


def _app(tmp_path, monkeypatch):
    """A booted dashboard app on a fresh config/db, still on the Work page."""
    cfg = _write_cfg(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return cfg, at


def _chat(at):
    _go(at, "Chat")
    assert not at.exception, [e.value for e in at.exception]
    return at


def _pin(store: Store, conv_id: str, ts: float) -> None:
    """Force a conversation's timestamps (add_message would bump updated_at)."""
    store._exec("UPDATE conversations SET updated_at = ?, created_at = ? WHERE id = ?", (ts, ts, conv_id))


def _seed_thread(store: Store, title: str, ts: float, messages=()) -> dict:
    conv = chatbot.create_conversation(store, title)
    for role, content in messages:
        chatbot.add_message(store, conv["id"], role, content)
    _pin(store, conv["id"], ts)
    return conv


def _fake_ask(monkeypatch, answer="**fake answer**"):
    asked = []

    def fake(c, store, question, history=None, **kw):
        asked.append({"question": question, "history": list(history or [])})
        return {"answer": answer, "role": "planner", "model": "qwen-fake",
                "question": question, "actions": {"tasks": [], "fixes": []}}

    monkeypatch.setattr(chatbot, "ask", fake)
    return asked



def _active(at):
    """Key of the highlighted rail row (the active conversation renders as a 'secondary' button)."""
    on = [b.key for b in at.button if (b.key or "").startswith("chat_sel_") and b.proto.type == "secondary"]
    assert len(on) == 1, on
    return on[0]

def _sel_buttons(at):
    return [b for b in at.button if (b.key or "").startswith("chat_sel_")]


def _db_msgs(cfg, conv_id):
    return [(m["role"], m["content"]) for m in Store(cfg.db_path)._all(
        "SELECT role, content FROM chat_messages WHERE conversation_id = ? ORDER BY id", (conv_id,))]


# ------------------------------------------------------------------ AC1
def test_ac1_rail_lists_all_conversations_most_recent_first_with_time_and_active_mark(tmp_path, monkeypatch):
    """The rail renders every conversation, newest first, title + relative time, active one highlighted."""
    _fake_ask(monkeypatch)
    cfg, at = _app(tmp_path, monkeypatch)
    now = time.time()
    store = Store(cfg.db_path)
    old = _seed_thread(store, "Oldest thread", now - 90000, [("user", "old question")])
    middle = _seed_thread(store, "Middle thread", now - 3700, [("user", "middle question")])
    newest = _seed_thread(store, "Newest thread", now - 300, [("user", "newest question")])
    _chat(at)

    sels = _sel_buttons(at)
    keys = [b.key for b in sels]
    # every conversation is listed
    assert len(keys) == len(chatbot.list_conversations(store)) == 3
    # most-recent first by updated_at
    order = [k.removeprefix("chat_sel_") for k in keys]
    assert order == [newest["id"], middle["id"], old["id"]]
    labels = {b.key: b.label for b in sels}
    assert "Newest thread" in labels[f"chat_sel_{newest['id']}"]
    assert "Middle thread" in labels[f"chat_sel_{middle['id']}"]
    assert "Oldest thread" in labels[f"chat_sel_{old['id']}"]
    # the active conversation is highlighted: a fresh session activates the most recent thread
    assert _active(at) == f"chat_sel_{newest['id']}"
    assert _active(at) != f"chat_sel_{middle['id']}"
    assert _active(at) != f"chat_sel_{old['id']}"
    # relative time shown per row
    captions = [c.value for c in at.caption]
    assert "5 min ago" in captions and "1 h ago" in captions and "1 d ago" in captions


def test_ac1_new_chat_button_sits_on_top_of_the_rail(tmp_path, monkeypatch):
    _fake_ask(monkeypatch)
    cfg, at = _app(tmp_path, monkeypatch)
    _seed_thread(Store(cfg.db_path), "Some thread", time.time())
    _chat(at)
    new = [i for i, b in enumerate(at.button) if b.key == "chat_new"]
    sels = [i for i, b in enumerate(at.button) if (b.key or "").startswith("chat_sel_")]
    assert new and sels, "no '+ New chat' button or no conversation rows"
    assert new[0] < sels[0]  # rendered above the conversation list
    assert any(b.key == "chat_new" and "New chat" in b.label for b in at.button)


# ------------------------------------------------------------------ AC2
def test_ac2_new_chat_starts_empty_conversation_and_selects_it(tmp_path, monkeypatch):
    _fake_ask(monkeypatch)
    cfg, at = _app(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    first = _seed_thread(store, "First chat", time.time(),
                         [("user", "remember me question"), ("assistant", "remember me answer")])
    _chat(at)
    assert any("remember me answer" in m.value for m in at.markdown)  # transcript visible first

    at.button(key="chat_new").click().run()
    assert not at.exception, [e.value for e in at.exception]

    # a fresh conversation exists in the DB, is empty, and became the active one
    convs = chatbot.list_conversations(Store(cfg.db_path))
    fresh = [c for c in convs if c["title"] == "New chat"]
    assert len(fresh) == 1 and fresh[0]["id"] != first["id"]
    assert chatbot.get_messages(Store(cfg.db_path), fresh[0]["id"]) == []
    assert at.session_state["chat_conv_id"] == fresh[0]["id"]
    assert at.session_state["chat_history"] == []
    # the rail marks it active and the old transcript is gone from the page
    sels = {b.key: b.label for b in _sel_buttons(at)}
    assert _active(at) == f"chat_sel_{fresh[0]['id']}" and sels[f"chat_sel_{fresh[0]['id']}"] == "New chat"
    texts = [m.value for m in at.markdown]
    assert not any("remember me" in t for t in texts)


# ------------------------------------------------------------------ AC3
def test_ac3_selecting_swaps_transcript_and_round_trips(tmp_path, monkeypatch):
    _fake_ask(monkeypatch)
    cfg, at = _app(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    now = time.time()
    a = _seed_thread(store, "Alpha chat", now - 600,
                     [("user", "alpha q"), ("assistant", "alpha a")])
    b = _seed_thread(store, "Beta chat", now - 60, [("user", "beta q"), ("assistant", "beta a")])
    _chat(at)
    # fresh session: the most recent thread (Beta) is active and shown
    assert at.session_state["chat_conv_id"] == b["id"]
    assert at.session_state["chat_history"] == [{"role": "user", "content": "beta q"},
                                                {"role": "assistant", "content": "beta a"}]

    at.button(key=f"chat_sel_{a['id']}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["chat_conv_id"] == a["id"]
    assert at.session_state["chat_history"] == [{"role": "user", "content": "alpha q"},
                                                {"role": "assistant", "content": "alpha a"}]
    texts = [m.value for m in at.markdown]
    assert any("alpha a" in t for t in texts) and not any("beta a" in t for t in texts)

    # switch back: Beta's own messages come back (the transcript is per thread, from the DB)
    at.button(key=f"chat_sel_{b['id']}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    texts = [m.value for m in at.markdown]
    assert any("beta a" in t for t in texts) and not any("alpha a" in t for t in texts)


def test_ac3_messages_persist_to_active_conversation_and_reload_from_db(tmp_path, monkeypatch):
    asked = _fake_ask(monkeypatch, answer="**persisted answer**")
    cfg, at = _app(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    now = time.time()
    a = _seed_thread(store, "Alpha chat", now - 600, [("user", "alpha old")])
    b = _seed_thread(store, "Beta chat", now - 60, [("user", "beta old")])
    _chat(at)

    at.button(key=f"chat_sel_{a['id']}").click().run()
    at.chat_input[0].set_value("new question").run()
    assert not at.exception, [e.value for e in at.exception]
    assert asked[0]["question"] == "new question"

    # the exchange landed in Alpha only — Beta untouched
    assert _db_msgs(cfg, a["id"]) == [("user", "alpha old"), ("user", "new question"),
                                      ("assistant", "**persisted answer**")]
    assert _db_msgs(cfg, b["id"]) == [("user", "beta old")]

    # a brand-new app session restores the thread from the DB (Alpha is now most recent)
    _, at2 = _app(tmp_path, monkeypatch)
    _chat(at2)
    assert at2.session_state["chat_conv_id"] == a["id"]
    assert at2.session_state["chat_history"] == [{"role": "user", "content": "alpha old"},
                                                 {"role": "user", "content": "new question"},
                                                 {"role": "assistant", "content": "**persisted answer**"}]
    assert any("**persisted answer**" in m.value for m in at2.markdown)


# ------------------------------------------------------------------ AC4
def test_ac4_inline_rename_goes_through_helper_and_updates_rail_at_once(tmp_path, monkeypatch):
    _fake_ask(monkeypatch)
    cfg, at = _app(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    now = time.time()
    a = _seed_thread(store, "Alpha chat", now - 600)
    _seed_thread(store, "Beta chat", now - 60)
    _chat(at)
    before = chatbot.get_conversation(store, a["id"])["updated_at"]

    at.button(key=f"chat_edit_{a['id']}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    at.text_input(key=f"chat_rename_{a['id']}").set_value("Renamed in rail").run()
    at.button(key=f"chat_rename_save_{a['id']}").click().run()
    assert not at.exception, [e.value for e in at.exception]

    # rail updated immediately, and the DB went through rename_conversation
    # (title changed AND updated_at bumped, so the row jumped to the front)
    labels = [b.label for b in _sel_buttons(at)]
    assert labels[0] == "Renamed in rail"
    conv = chatbot.get_conversation(Store(cfg.db_path), a["id"])
    assert conv["title"] == "Renamed in rail"
    assert conv["updated_at"] > before


def test_ac4_rename_cancel_keeps_title(tmp_path, monkeypatch):
    _fake_ask(monkeypatch)
    cfg, at = _app(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    now = time.time()
    a = _seed_thread(store, "Alpha chat", now - 600)
    _seed_thread(store, "Beta chat", now - 60)
    _chat(at)

    at.button(key=f"chat_edit_{a['id']}").click().run()
    at.text_input(key=f"chat_rename_{a['id']}").set_value("Should not stick").run()
    at.button(key=f"chat_rename_cancel_{a['id']}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert chatbot.get_conversation(Store(cfg.db_path), a["id"])["title"] == "Alpha chat"
    assert any(b.key == f"chat_sel_{a['id']}" and "Alpha chat" in b.label for b in _sel_buttons(at))


def test_ac4_delete_removes_thread_row_and_its_messages_from_db(tmp_path, monkeypatch):
    _fake_ask(monkeypatch)
    cfg, at = _app(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    now = time.time()
    a = _seed_thread(store, "Alpha chat", now - 60, [("user", "keep me")])  # active (most recent)
    b = _seed_thread(store, "Beta chat", now - 600, [("user", "bye"), ("assistant", "bye answer")])
    _chat(at)

    at.button(key=f"chat_del_{b['id']}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    # rail updated immediately
    assert f"chat_sel_{b['id']}" not in [x.key for x in _sel_buttons(at)]
    # CRUD helper path: conversation row and its messages are gone from the DB
    assert chatbot.get_conversation(Store(cfg.db_path), b["id"]) is None
    assert Store(cfg.db_path)._all(
        "SELECT * FROM chat_messages WHERE conversation_id = ?", (b["id"],)) == []
    # the active thread is untouched
    assert at.session_state["chat_conv_id"] == a["id"]
    assert any("keep me" in m.value for m in at.markdown)


def test_ac4_deleting_the_active_thread_selects_the_next_most_recent(tmp_path, monkeypatch):
    _fake_ask(monkeypatch)
    cfg, at = _app(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    now = time.time()
    a = _seed_thread(store, "Alpha chat", now - 60, [("user", "active q")])  # active
    b = _seed_thread(store, "Beta chat", now - 600, [("user", "next q"), ("assistant", "next a")])
    _chat(at)

    at.button(key=f"chat_del_{a['id']}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["chat_conv_id"] == b["id"]
    assert at.session_state["chat_history"] == [{"role": "user", "content": "next q"},
                                                {"role": "assistant", "content": "next a"}]
    sels = {x.key: x.label for x in _sel_buttons(at)}
    assert _active(at) == f"chat_sel_{b['id']}"
    assert any("next a" in m.value for m in at.markdown)
