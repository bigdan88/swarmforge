"""Acceptance tests: per-conversation context assembly with a token/window cap.

Written independently of the developer tests; every acceptance criterion (AC1-AC6)
of the issue is checked here. Fully offline through the conftest FakeProvider.
"""
import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
import yaml

from swarmforge import chatbot
from swarmforge.chatbot import estimate_tokens, truncate_context
from swarmforge.config import ConfigError, load_config, parse_config
from swarmforge.db import Store

from .conftest import FakeProvider, base_raw

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(cfg):
    s = Store(cfg.db_path)
    yield s
    s.close()


def _fake(reply: str = "ok"):
    return FakeProvider({"qwen-fake": lambda b: {"content": reply}})


def _thread(request_body):
    """Non-system messages sent to the model, in order."""
    return [m for m in request_body["messages"] if m["role"] != "system"]


# ============================================================== AC1: isolation
def test_ac1_two_conversations_produce_disjoint_contexts(cfg, store):
    fake = _fake()
    chatbot.ask(cfg, store, "alpha question", conversation="A", transport=fake.transport)
    chatbot.ask(cfg, store, "bravo question", conversation="B", transport=fake.transport)
    # second turn on A: must see A's history only, never B's
    chatbot.ask(cfg, store, "alpha follow-up", conversation="A", transport=fake.transport)
    # second turn on B: must see B's history only, never A's
    chatbot.ask(cfg, store, "bravo follow-up", conversation="B", transport=fake.transport)

    assert [m["content"] for m in _thread(fake.requests[2])] == [
        "alpha question", "ok", "alpha follow-up"]
    assert [m["content"] for m in _thread(fake.requests[3])] == [
        "bravo question", "ok", "bravo follow-up"]
    for body in (fake.requests[2], fake.requests[3]):
        contents = " ".join(m["content"] for m in _thread(body))
        if "alpha" in contents:
            assert "bravo" not in contents
        else:
            assert "alpha" not in contents
    # each conversation persisted only its own rows
    assert {m["conversation_id"] for m in store.conversation_messages("A")} == {"A"}
    assert {m["conversation_id"] for m in store.conversation_messages("B")} == {"B"}
    assert len(store.conversation_messages("A")) == 4
    assert len(store.conversation_messages("B")) == 4


def test_ac1_switching_threads_does_not_leak_history(cfg, store):
    fake = _fake()
    for i in range(3):
        chatbot.ask(cfg, store, f"secret thread one {i}", conversation="one", transport=fake.transport)
    chatbot.ask(cfg, store, "first question in thread two", conversation="two", transport=fake.transport)
    last = _thread(fake.requests[-1])
    assert [m["content"] for m in last] == ["first question in thread two"]
    assert all("thread one" not in m["content"] for m in fake.requests[-1]["messages"])
    # thread "one" itself is intact and still isolated
    assert len(store.conversation_messages("one")) == 6
    assert store.conversation_messages("two")[0]["content"] == "first question in thread two"


def test_ac1_answer_async_is_also_scoped_by_conversation(cfg, store):
    fake = _fake()
    asyncio.run(chatbot.answer(cfg, store, "q for X", conversation="X", transport=fake.transport))
    asyncio.run(chatbot.answer(cfg, store, "q for Y", conversation="Y", transport=fake.transport))
    assert [m["content"] for m in _thread(fake.requests[1])] == ["q for Y"]
    assert fake.requests[0]["messages"][0]["role"] == "system"
    assert "<status>" in fake.requests[0]["messages"][0]["content"]


def test_ac1_conversation_wins_over_history_argument(cfg, store):
    """With a conversation, the caller-supplied history is not mixed into the thread."""
    fake = _fake()
    chatbot.ask(cfg, store, "real question", conversation="Z",
                history=[{"role": "user", "content": "foreign thread"},
                         {"role": "assistant", "content": "foreign answer"}],
                transport=fake.transport)
    assert [m["content"] for m in _thread(fake.requests[0])] == ["real question"]


# ========================================================= AC2: truncation rule
def test_ac2_estimate_tokens_formula():
    assert estimate_tokens([{"role": "user", "content": ""}]) == 1          # floor of 1
    assert estimate_tokens([{"role": "user", "content": "x" * 7}]) == 1     # 7 // 4 == 1
    assert estimate_tokens([{"role": "user", "content": "x" * 40},
                            {"role": "assistant", "content": "y" * 9}]) == 10 + 2
    assert estimate_tokens([]) == 0


def test_ac2_message_window_exact_boundary():
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(5)]
    # at the limit nothing is dropped
    assert [m["content"] for m in truncate_context(msgs, 5)] == ["m0", "m1", "m2", "m3", "m4"]
    # one over the limit drops exactly the oldest
    assert [m["content"] for m in truncate_context(msgs, 4)] == ["m1", "m2", "m3", "m4"]
    assert [m["content"] for m in truncate_context(msgs, 1)] == ["m4"]
    assert truncate_context(msgs, 0) == []
    assert len(msgs) == 5  # input never mutated


def test_ac2_token_budget_drops_oldest_then_keeps_newest_always():
    a = {"role": "user", "content": "a" * 400}   # 100 tokens
    b = {"role": "assistant", "content": "b" * 400}
    c = {"role": "user", "content": "c" * 40}    # 10 tokens
    kept = truncate_context([a, b, c], 10, max_tokens=150)
    assert [m["content"][0] for m in kept] == ["b", "c"]     # 110 <= 150
    assert estimate_tokens(kept) == 110
    # exact fit keeps everything
    assert len(truncate_context([a, b, c], 10, max_tokens=210)) == 3
    # one message over budget still keeps the newest
    only = truncate_context([a, c], 10, max_tokens=50)
    assert only == [c]
    assert truncate_context([a], 10, max_tokens=1) == [a]
    assert truncate_context([], 10, max_tokens=1) == []


def test_ac2_window_is_the_configured_value(cfg, store):
    cfg.chat.context_max_messages = 4
    for i in range(4):
        store.conversation_append("W", "user", f"u{i}")
        store.conversation_append("W", "assistant", f"a{i}")
    fake = _fake()
    chatbot.ask(cfg, store, "newest", conversation="W", transport=fake.transport)
    # 8 stored + this question = 9 rows, newest 4 go to the model
    assert [m["content"] for m in _thread(fake.requests[0])] == ["a2", "u3", "a3", "newest"]


def test_ac2_token_budget_applied_end_to_end(cfg, store):
    cfg.chat.context_max_messages = 12
    cfg.chat.context_max_tokens = 256
    big = "z" * 1000  # 250 tokens
    store.conversation_append("T", "user", big)
    store.conversation_append("T", "assistant", "short")
    store.conversation_append("T", "user", "q1")
    store.conversation_append("T", "assistant", "a1")
    fake = _fake()
    chatbot.ask(cfg, store, "q2", conversation="T", transport=fake.transport)
    thread = _thread(fake.requests[0])
    contents = [m["content"] for m in thread]
    assert big not in contents                      # oldest over-budget message dropped
    assert contents == ["a1", "q2"] or contents[-1] == "q2"
    assert estimate_tokens(thread) <= 256


def test_ac2_truncation_is_deterministic(cfg, store):
    msgs = [{"role": "user", "content": f"m{i}" * (i + 1)} for i in range(9)]
    first = [m["content"] for m in truncate_context(msgs, 5, max_tokens=20)]
    second = [m["content"] for m in truncate_context(msgs, 5, max_tokens=20)]
    assert first == second


# ==================================================== AC3: cost attribution
def test_ac3_usage_recorded_under_chat_conversation_run_id(cfg, store):
    fake = _fake()
    res = chatbot.ask(cfg, store, "how much?", conversation="paid", transport=fake.transport)
    rows = store.llm_calls("chat:paid")
    assert len(rows) == 1
    assert rows[0]["run_id"] == "chat:paid"
    assert rows[0]["task_key"] == "chatbot"
    assert rows[0]["cost_usd"] and rows[0]["cost_usd"] > 0
    summary = store.cost_summary("chat:paid")
    assert summary["total"] == pytest.approx(rows[0]["cost_usd"])
    assert res["conversation"] == "paid"
    assert res["usage"] == summary


def test_ac3_cost_is_per_conversation_and_accumulates(cfg, store):
    fake = _fake()
    chatbot.ask(cfg, store, "a1", conversation="A", transport=fake.transport)
    chatbot.ask(cfg, store, "a2", conversation="A", transport=fake.transport)
    chatbot.ask(cfg, store, "b1", conversation="B", transport=fake.transport)

    a = store.cost_summary("chat:A")
    b = store.cost_summary("chat:B")
    assert a["calls"] == 2 and b["calls"] == 1
    assert a["total"] == pytest.approx(2 * b["total"])
    # untouched conversations report zero
    assert store.cost_summary("chat:never")["total"] == 0
    assert store.cost_summary("chat:never")["calls"] == 0
    assert store.llm_calls("chat:never") == []
    # and nothing is attributed to a bare run id
    assert store.llm_calls("A") == []


def test_ac3_failed_call_is_still_attributed(cfg, store):
    def boom(body):
        raise httpx.ConnectError("connection refused")
    fake = FakeProvider({"qwen-fake": boom})
    with pytest.raises(chatbot.ChatError):
        chatbot.ask(cfg, store, "will fail", conversation="F", transport=fake.transport)
    msgs = store.conversation_messages("F")
    assert [m["role"] for m in msgs] == ["user"]
    assert store.cost_summary("chat:F")["calls"] == 0


# ============================== AC4: tests/test_chat_context.py assertions
def test_ac4_chat_context_test_file_exists_and_covers_isolation_and_boundary():
    p = ROOT / "tests" / "test_chat_context.py"
    assert p.is_file(), "tests/test_chat_context.py must exist"
    src = p.read_text(encoding="utf-8")
    names = re.findall(r"^def (test_\w+)", src, re.M)
    assert names, "the file must contain tests"
    assert re.search(r"conversation=\"A\"", src) and re.search(r"conversation=\"B\"", src), \
        "isolation between two conversations must be asserted"
    assert "disjoint" in src.lower() or "isolation" in src.lower()
    assert "truncate_context" in src and "context_max_messages" in src, \
        "the truncation boundary must be asserted"
    assert "[m[\"content\"] for m in kept] ==" in src or "kept] ==" in src, \
        "exact kept/dropped sets at the limit must be asserted"


# ========================================================== AC5: config
def _write_yaml(tmp_path, raw):
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


def test_ac5_defaults_and_validated_values(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    cfg = parse_config(base_raw(tmp_path / "work"))
    assert cfg.chat.context_max_messages == 12
    assert cfg.chat.context_max_tokens == 4000

    raw = base_raw(tmp_path / "work")
    raw["chat"] = {"context_max_messages": 3, "context_max_tokens": 512}
    cfg = parse_config(raw)
    assert cfg.chat.context_max_messages == 3 and cfg.chat.context_max_tokens == 512


@pytest.mark.parametrize("bad,err", [
    ({"context_max_messages": 0}, "context_max_messages"),
    ({"context_max_messages": 201}, "context_max_messages"),
    ({"context_max_messages": 200}, None),
    ({"context_max_tokens": 255}, "context_max_tokens"),
    ({"context_max_tokens": 200001}, "context_max_tokens"),
    ({"context_max_tokens": 256}, None),
    ({"context_max_messages": 12, "typo_key": 1}, "unknown"),
])
def test_ac5_validation(tmp_path, monkeypatch, bad, err):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    raw = base_raw(tmp_path / "work")
    raw["chat"] = bad
    if err is None:
        cfg = parse_config(raw)
        assert cfg.chat.context_max_messages == bad.get("context_max_messages", 12)
    else:
        with pytest.raises(ConfigError, match=re.escape(err)):
            parse_config(raw)


def test_ac5_config_without_chat_section_still_loads(tmp_path, monkeypatch):
    """Backward compatibility: an existing swarmforge.yaml with no chat: section works."""
    monkeypatch.setenv("FAKE_KEY", "test-key")
    p = _write_yaml(tmp_path, base_raw(tmp_path / "work"))
    assert "chat" not in yaml.safe_load(p.read_text(encoding="utf-8"))
    cfg = load_config(str(p))
    assert cfg.chat.context_max_messages == 12
    assert cfg.chat.context_max_tokens == 4000
    fake = _fake()
    st_ = Store(cfg.db_path)
    res = chatbot.ask(cfg, st_, "hi", conversation="compat", transport=fake.transport)
    assert res["answer"] == "ok"
    # the default window really is 12 messages
    for i in range(20):
        st_.conversation_append("wide", "user", f"m{i}")
    chatbot.ask(cfg, st_, "last", conversation="wide", transport=fake.transport)
    assert len(_thread(fake.requests[-1])) == 12
    assert _thread(fake.requests[-1])[-1]["content"] == "last"


def test_ac5_example_yaml_documents_the_chat_section():
    raw = yaml.safe_load((ROOT / "swarmforge.example.yaml").read_text(encoding="utf-8"))
    assert "chat" in raw
    assert raw["chat"]["context_max_messages"] == 12
    assert raw["chat"]["context_max_tokens"] == 4000


# ================================================ AC6: backward compatibility
def test_ac6_ask_without_conversation_is_unchanged(cfg, store):
    fake = _fake()
    res = chatbot.ask(cfg, store, "plain", transport=fake.transport)
    assert set(res) == {"answer", "role", "model", "question", "actions"}
    assert res["answer"] == "ok" and res["question"] == "plain"
    assert res["role"] == "planner" and res["model"] == "qwen-fake"
    # history param honored, question appended last
    chatbot.ask(cfg, store, "next", history=[{"role": "user", "content": "h1"},
                                             {"role": "assistant", "content": "h2"}],
                transport=fake.transport)
    assert [m["content"] for m in _thread(fake.requests[1])] == ["h1", "h2", "next"]
    # nothing persisted, no cost attributed
    assert store.conversations_list() == []
    assert store.cost_summary("chat:cli")["total"] == 0
    assert store.llm_calls("chat:cli") == []


def test_ac6_legacy_result_json_roundtrips(cfg, store):
    """Old callers read answer/role/model/question out of the result dict."""
    fake = _fake()
    res = chatbot.ask(cfg, store, "plain", transport=fake.transport)
    assert json.dumps({k: res[k] for k in ("question", "answer", "role", "model")})


def test_ac6_cli_ask_has_conversation_defaulting_to_cli(tmp_path, monkeypatch, capsys):
    from swarmforge import cli
    monkeypatch.setenv("FAKE_KEY", "test-key")
    p = _write_yaml(tmp_path, base_raw(tmp_path / "work"))
    seen = {}

    def fake_ask(cfg, store, question, history=None, *, conversation=None, transport=None, max_runs=12, on_tool=None):
        seen["conversation"] = conversation
        seen["question"] = question
        return {"answer": "fine", "role": "planner", "model": "qwen-fake", "question": question}

    monkeypatch.setattr(chatbot, "ask", fake_ask)
    assert cli.main(["--config", str(p), "ask", "how are runs?"]) == 0
    assert seen == {"conversation": "cli", "question": "how are runs?"}
    assert capsys.readouterr().out.strip() == "fine"

    assert cli.main(["--config", str(p), "ask", "q", "--conversation", "thread-7"]) == 0
    assert seen["conversation"] == "thread-7"

    with pytest.raises(SystemExit) as exc:
        cli.main(["ask", "--help"])
    assert exc.value.code == 0
    assert "--conversation" in capsys.readouterr().out


def test_ac6_dashboard_chat_session_conversation_and_reset(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from .test_dashboard import APP, _go, _write_cfg

    cfg_dash = _write_cfg(tmp_path, monkeypatch)
    st_ = Store(cfg_dash.db_path)
    st_.create_run("20990101-000000-aaaaaa", state="COMPLETED", issue_title="Export CSV", repo="x/y",
                   budget_usd=5, state_reason="done", config_json="{}")
    st_.close()

    calls = []

    def fake_ask(cfg, store, question, history=None, *, conversation=None, transport=None, max_runs=12, on_tool=None):
        calls.append({"question": question, "conversation": conversation})
        return {"answer": "ok", "role": "planner", "model": "qwen-fake", "question": question,
                "conversation": conversation, "usage": {"cost_usd": 0.02, "total_tokens": 10}}

    monkeypatch.setattr(chatbot, "ask", fake_ask)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    _go(at, "Chat")
    assert not at.exception, [e.value for e in at.exception]

    at.chat_input[0].set_value("first").run()
    assert not at.exception, [e.value for e in at.exception]
    conv = calls[0]["conversation"]
    assert isinstance(conv, str) and conv
    at.chat_input[0].set_value("second").run()
    assert calls[1]["conversation"] == conv          # stable within the session
    # the page shows the conversation and its cost
    assert any("conversation" in c.value and "$0.0200" in c.value for c in at.caption)
    # reset starts a brand-new conversation id with no history
    at.button(key="chat_reset").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["chat_history"] == []
    at.chat_input[0].set_value("third").run()
    assert calls[2]["conversation"] != conv
    assert calls[2]["question"] == "third"


# =============================================== persistence-layer acceptance (main)
import sqlite3  # noqa: E402
from swarmforge.chatbot import (  # noqa: E402
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
from swarmforge.db import Store  # noqa: E402


"""Acceptance tests for the conversation/message persistence layer (issue: db.py + chatbot.py).

Complements tests/test_chat_conversations.py with the gaps an acceptance pass cares about:
the legacy-DB upgrade path (AC1), durability across Store reopens (AC2), delete isolation
(AC3), rename affecting list order (AC4) and default-seed idempotency across Store
instances (AC5).
"""

from swarmforge import chatbot


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