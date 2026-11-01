"""Per-conversation chat storage (db.py) and the `chat:` config section."""
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
