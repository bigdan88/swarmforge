"""Offline unit tests for conversation/message persistence (swarmforge.chatbot)."""

import time

import pytest

from swarmforge.config import ConfigError, parse_config
from swarmforge.db import Store
from tests.conftest import base_raw


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def test_conversation_crud_roundtrip(store):
    conv = store.conversation_create("c1", title="First")
    assert conv["id"] == "c1"
    assert conv["title"] == "First"
    assert conv["created_at"] > 0

    # creating again is idempotent
    again = store.conversation_create("c1", title="ignored")
    assert again["title"] == "First"

    assert store.conversation_get("c1")["id"] == "c1"
    assert store.conversation_get("missing") is None

    store.conversation_create("c2")
    ids = [c["id"] for c in store.conversations_list()]
    assert set(ids) == {"c1", "c2"}


def test_conversations_list_orders_by_recent_activity(store):
    store.conversation_create("old")
    store.conversation_create("new")
    time.sleep(0.01)
    store.conversation_append("old", "user", "bump")
    assert store.conversations_list()[0]["id"] == "old"


def test_append_auto_creates_and_returns_id(store):
    mid = store.conversation_append("fresh", "user", "hello")
    assert isinstance(mid, int) and mid > 0
    conv = store.conversation_get("fresh")
    assert conv is not None
    assert conv["updated_at"] >= conv["created_at"]
    msgs = store.conversation_messages("fresh")
    assert len(msgs) == 1
    assert msgs[0] == {"id": mid, "conversation_id": "fresh", "role": "user",
                       "content": "hello", "created_at": msgs[0]["created_at"]}
    assert msgs[0]["created_at"] > 0


def test_message_isolation_and_limit(store):
    for i in range(4):
        store.conversation_append("a", "user", f"a{i}")
    for i in range(2):
        store.conversation_append("b", "user", f"b{i}")

    a = store.conversation_messages("a")
    assert [m["content"] for m in a] == ["a0", "a1", "a2", "a3"]
    assert all(m["conversation_id"] == "a" for m in a)
    b = store.conversation_messages("b")
    assert [m["content"] for m in b] == ["b0", "b1"]

    # limit keeps the NEWEST N rows, still chronological
    tail = store.conversation_messages("a", limit=2)
    assert [m["content"] for m in tail] == ["a2", "a3"]
    assert tail[0]["id"] < tail[1]["id"]
    assert store.conversation_messages("a", limit=10) == a
    assert store.conversation_messages("unknown") == []
    assert store.conversation_messages("unknown", limit=3) == []


def test_conversation_delete_removes_messages(store):
    store.conversation_append("a", "user", "x")
    store.conversation_append("b", "user", "y")
    store.conversation_delete("a")
    assert store.conversation_get("a") is None
    assert store.conversation_messages("a") == []
    assert len(store.conversation_messages("b")) == 1
    store.conversation_delete("missing")  # no error


# ------------------------------------------------------------------- config
def test_chat_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    cfg = parse_config(base_raw(tmp_path / "work"))
    assert cfg.chat.context_max_messages == 12
    assert cfg.chat.context_max_tokens == 4000


def test_chat_config_parsing(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    raw = base_raw(tmp_path / "work")
    raw["chat"] = {"context_max_messages": 5, "context_max_tokens": 1024}
    cfg = parse_config(raw)
    assert cfg.chat.context_max_messages == 5
    assert cfg.chat.context_max_tokens == 1024


@pytest.mark.parametrize("raw_chat", [
    {"context_max_messages": 0},
    {"context_max_messages": 201},
    {"context_max_tokens": 10},
    {"context_max_tokens": 200001},
])
def test_chat_config_validation(tmp_path, monkeypatch, raw_chat):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    raw = base_raw(tmp_path / "work")
    raw["chat"] = raw_chat
    with pytest.raises(ConfigError):
        parse_config(raw)


# ============================================ chatbot persistence helpers (main)
from swarmforge import chatbot  # noqa: E402
from swarmforge.chatbot import (  # noqa: E402
    DEFAULT_CONVERSATION_TITLE,
    ChatError,
    add_message,
    clear_messages,
    create_conversation,
    delete_conversation,
    ensure_default_conversation,
    get_conversation,
    get_messages,
    list_conversations,
    rename_conversation,
)


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


def test_clear_messages_empties_but_keeps_conversation(store):
    conv = create_conversation(store, "Keep me")
    add_message(store, conv["id"], "user", "hi")
    add_message(store, conv["id"], "assistant", "hello")

    deleted = clear_messages(store, conv["id"])

    assert deleted == 2
    assert get_messages(store, conv["id"]) == []
    assert get_conversation(store, conv["id"]) is not None
    assert get_conversation(store, conv["id"])["id"] == conv["id"]


def test_clear_messages_returns_zero_when_empty(store):
    conv = create_conversation(store, "Empty")
    assert clear_messages(store, conv["id"]) == 0
    assert clear_messages(store, conv["id"]) == 0


def test_clear_messages_unknown_conversation_raises(store):
    with pytest.raises(ChatError) as exc:
        clear_messages(store, 987654)
    assert "987654" in str(exc.value)


def test_clear_messages_leaves_other_conversations_alone(store):
    a = create_conversation(store, "A")
    b = create_conversation(store, "B")
    add_message(store, a["id"], "user", "a1")
    add_message(store, b["id"], "user", "b1")
    add_message(store, b["id"], "assistant", "b2")

    assert clear_messages(store, a["id"]) == 1
    assert [m["content"] for m in get_messages(store, b["id"])] == ["b1", "b2"]


def test_clear_messages_does_not_bump_updated_at(store):
    first = create_conversation(store, "First")
    second = create_conversation(store, "Second")
    add_message(store, first["id"], "user", "hi")
    add_message(store, second["id"], "user", "yo")

    before = [c["id"] for c in list_conversations(store)]
    second_now = get_conversation(store, second["id"])

    clear_messages(store, second["id"])

    after = [c["id"] for c in list_conversations(store)]
    assert after == before
    assert (
        get_conversation(store, second["id"])["updated_at"] == second_now["updated_at"]
    )
