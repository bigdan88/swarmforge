"""Acceptance tests for the conversation/message persistence layer (issue: db.py + chatbot.py).

Complements tests/test_chat_conversations.py with the gaps an acceptance pass cares about:
the legacy-DB upgrade path (AC1), durability across Store reopens (AC2), delete isolation
(AC3), rename affecting list order (AC4) and default-seed idempotency across Store
instances (AC5).
"""

import sqlite3

from swarmforge import chatbot
from swarmforge.chatbot import (
    DEFAULT_CONVERSATION_TITLE,
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


def _tables(store: Store) -> set[str]:
    return {
        r[0]
        for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


# ------------------------------------------------------------------ AC1
def test_reopen_on_existing_store_no_error_and_tables_present(tmp_path):
    """Startup is idempotent: opening the same file repeatedly never raises and both
    new tables exist."""
    path = tmp_path / "db.sqlite"
    for _ in range(3):
        s = Store(path)
        assert {"conversations", "chat_messages"} <= _tables(s)
        s.close()


def test_legacy_db_without_chat_tables_gets_them_on_reopen(tmp_path):
    """A database from an older release (no conversations/chat_messages) is upgraded
    in place on the next Store open, without errors."""
    path = tmp_path / "db.sqlite"
    s = Store(path)
    s.create_run("r" * 32, state="COMPLETED")  # some legacy data
    s.conn.executescript("DROP TABLE chat_messages; DROP TABLE conversations;")
    s.conn.commit()
    assert "conversations" not in _tables(s)
    s.close()

    reopened = Store(path)  # must not raise
    try:
        assert {"conversations", "chat_messages"} <= _tables(reopened)
        assert reopened.get_run("r" * 32)["state"] == "COMPLETED"  # legacy data intact
    finally:
        reopened.close()


# ------------------------------------------------------------------ AC2
def test_round_trip_survives_store_reopen(tmp_path):
    path = tmp_path / "db.sqlite"
    s1 = Store(path)
    conv = create_conversation(s1, "Persisted", last_run_id="run-42")
    add_message(s1, conv["id"], "user", "first")
    add_message(s1, conv["id"], "assistant", "second")
    s1.close()

    s2 = Store(path)
    try:
        again = get_conversation(s2, conv["id"])
        assert again is not None and again["title"] == "Persisted"
        assert again["last_run_id"] == "run-42"
        msgs = get_messages(s2, conv["id"])
        assert [(m["role"], m["content"]) for m in msgs] == [
            ("user", "first"),
            ("assistant", "second"),
        ]
        assert msgs[0]["id"] < msgs[1]["id"]  # oldest first
    finally:
        s2.close()


def test_get_messages_limit_edge_values(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    conv = create_conversation(store)
    for i in range(5):
        add_message(store, conv["id"], "user", f"m{i}")
    assert [m["content"] for m in get_messages(store, conv["id"], limit=0)] == []
    assert [m["content"] for m in get_messages(store, conv["id"], limit=1)] == ["m4"]
    assert [m["content"] for m in get_messages(store, conv["id"], limit=99)] == [
        "m0",
        "m1",
        "m2",
        "m3",
        "m4",
    ]  # limit larger than history, still oldest-first
    store.close()


# ------------------------------------------------------------------ AC3
def test_delete_only_touches_the_named_conversation(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    a = create_conversation(store, "A")
    b = create_conversation(store, "B")
    add_message(store, a["id"], "user", "keep? no")
    add_message(store, b["id"], "user", "keep me")

    assert delete_conversation(store, a["id"]) is True
    assert get_messages(store, a["id"]) == []
    assert [m["content"] for m in get_messages(store, b["id"])] == ["keep me"]
    assert store._one("SELECT COUNT(*) AS n FROM chat_messages")["n"] == 1
    # deleting the same id again reports nothing was removed
    assert delete_conversation(store, a["id"]) is False
    assert delete_conversation(store, "does-not-exist") is False
    store.close()


def test_delete_uses_no_orphan_rows_even_with_foreign_keys_enforced(tmp_path):
    """chat_messages has ON DELETE CASCADE; prove no orphans are left either way."""
    path = tmp_path / "db.sqlite"
    store = Store(path)
    conv = create_conversation(store, "Cascade")
    for i in range(3):
        add_message(store, conv["id"], "user", f"m{i}")
    delete_conversation(store, conv["id"])
    store.close()

    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 0
    finally:
        conn.close()


# ------------------------------------------------------------------ AC4
def test_rename_moves_conversation_to_front_of_list(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite")
    a = create_conversation(store, "A")
    b = create_conversation(store, "B")
    assert [c["id"] for c in list_conversations(store)] == [b["id"], a["id"]]

    tick = iter(
        [
            store._one("SELECT updated_at FROM conversations WHERE id = ?", (b["id"],))[
                "updated_at"
            ]
            + 10_000
        ]
    )
    monkeypatch.setattr(chatbot.time, "time", lambda: next(tick))
    renamed = rename_conversation(store, a["id"], "A renamed")
    assert renamed["title"] == "A renamed"
    assert renamed["updated_at"] > a["updated_at"]
    assert [c["id"] for c in list_conversations(store)] == [a["id"], b["id"]]
    store.close()


# ------------------------------------------------------------------ AC5
def test_ensure_default_conversation_shared_across_store_instances(tmp_path):
    path = tmp_path / "db.sqlite"
    s1 = Store(path)
    first = ensure_default_conversation(s1)
    assert first["title"] == DEFAULT_CONVERSATION_TITLE
    s1.close()

    s2 = Store(path)
    again = ensure_default_conversation(s2)
    assert again["id"] == first["id"]
    assert len(list_conversations(s2)) == 1  # seeded exactly once, ever
    s2.close()
