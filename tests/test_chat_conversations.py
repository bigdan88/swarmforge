"""Offline unit tests for conversation/message persistence (swarmforge.chatbot)."""
import pytest

from swarmforge import chatbot
from swarmforge.chatbot import (
    DEFAULT_CONVERSATION_TITLE,
    ChatError,
    add_message,
    create_conversation,
    delete_conversation,
    ensure_default_conversation,
    get_conversation,
    get_messages,
    list_conversations,
    rename_conversation,
)
from swarmforge.db import Store


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "db.sqlite")


def _tables(store: Store) -> set[str]:
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {r[0] for r in rows}


def test_tables_created_idempotently(tmp_path):
    path = tmp_path / "db.sqlite"
    s1 = Store(path)
    s1.close()
    s2 = Store(path)  # second open on the same file must not raise
    try:
        tables = _tables(s2)
        assert "conversations" in tables
        assert "chat_messages" in tables
    finally:
        s2.close()


def test_create_add_get_round_trip(store):
    conv = create_conversation(store, "Work chat")
    assert set(conv) >= {"id", "title", "created_at", "updated_at", "last_run_id"}
    assert conv["title"] == "Work chat"
    assert conv["last_run_id"] is None
    assert get_conversation(store, conv["id"])["id"] == conv["id"]

    add_message(store, conv["id"], "user", "hi")
    add_message(store, conv["id"], "assistant", "hello")

    msgs = get_messages(store, conv["id"])
    assert [m["content"] for m in msgs] == ["hi", "hello"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["id"] < msgs[1]["id"]


def test_get_messages_limit_returns_last_n_oldest_first(store):
    conv = create_conversation(store)
    for text in ("m1", "m2", "m3", "m4"):
        add_message(store, conv["id"], "user", text)

    msgs = get_messages(store, conv["id"], limit=2)
    assert [m["content"] for m in msgs] == ["m3", "m4"]
    assert msgs[0]["id"] < msgs[1]["id"]


def test_list_conversations_most_recent_first(store):
    a = create_conversation(store, "A")
    b = create_conversation(store, "B")
    assert [c["id"] for c in list_conversations(store)] == [b["id"], a["id"]]

    # touching A later must move it back to the front
    add_message(store, a["id"], "user", "ping")
    assert [c["id"] for c in list_conversations(store)] == [a["id"], b["id"]]


def test_rename_conversation_updates_title_and_bumps_updated_at(store, monkeypatch):
    conv = create_conversation(store, "Old title")
    t0 = conv["updated_at"]
    ticks = iter([t0 + 100.0, t0 + 200.0, t0 + 300.0])
    monkeypatch.setattr(chatbot.time, "time", lambda: next(ticks))

    renamed = rename_conversation(store, conv["id"], "New title")
    assert renamed["title"] == "New title"
    assert renamed["updated_at"] > conv["updated_at"]
    assert get_conversation(store, conv["id"])["title"] == "New title"

    with pytest.raises(ChatError):
        rename_conversation(store, 999999, "nope")


def test_delete_conversation_cascades_no_orphans(store):
    conv = create_conversation(store, "Doomed")
    for i in range(3):
        add_message(store, conv["id"], "user", f"m{i}")

    assert delete_conversation(store, conv["id"]) is True
    assert get_messages(store, conv["id"]) == []
    assert store._all("SELECT * FROM chat_messages") == []
    assert get_conversation(store, conv["id"]) is None
    assert delete_conversation(store, conv["id"]) is False


def test_add_message_unknown_conversation_raises(store):
    with pytest.raises(ChatError):
        add_message(store, 424242, "user", "hi")


def test_ensure_default_conversation_seeds_once(store):
    conv = ensure_default_conversation(store)
    assert conv["title"] == DEFAULT_CONVERSATION_TITLE
    assert len(list_conversations(store)) == 1

    again = ensure_default_conversation(store)
    assert again["id"] == conv["id"]
    assert len(list_conversations(store)) == 1

    other = create_conversation(store, "Other")
    seeded = ensure_default_conversation(store)
    assert seeded["id"] != other["id"]  # nothing new was seeded
    assert len(list_conversations(store)) == 2
