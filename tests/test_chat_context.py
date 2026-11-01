"""Per-conversation context windows: isolation, truncation and cost attribution.

Fully offline through the conftest FakeProvider.
"""
import httpx
import pytest

from swarmforge import chatbot
from swarmforge.db import Store

from .conftest import FakeProvider


@pytest.fixture
def st_(cfg):
    return Store(cfg.db_path)


def _fake(reply: str = "ok"):
    return FakeProvider({"qwen-fake": lambda b: {"content": reply}})


def _thread_messages(request_body):
    """The non-system messages of a chat request."""
    return [m for m in request_body["messages"] if m["role"] != "system"]


# ------------------------------------------------------------- isolation (AC1)
def test_two_conversations_have_disjoint_contexts(cfg, st_):
    fake = _fake()
    chatbot.ask(cfg, st_, "what about alpha?", conversation="A", transport=fake.transport)
    chatbot.ask(cfg, st_, "what about bravo?", conversation="B", transport=fake.transport)
    chatbot.ask(cfg, st_, "and alpha again?", conversation="A", transport=fake.transport)

    a = _thread_messages(fake.requests[2])
    b = _thread_messages(fake.requests[1])
    assert [m["content"] for m in a] == ["what about alpha?", "ok", "and alpha again?"]
    assert [m["content"] for m in b] == ["what about bravo?"]
    # no leak across threads
    assert all("bravo" not in m["content"] for m in a)
    assert all("alpha" not in m["content"] for m in b)
    # the snapshot system prompt is always present
    assert fake.requests[2]["messages"][0]["role"] == "system"
    assert "<status>" in fake.requests[2]["messages"][0]["content"]


def test_history_argument_is_ignored_when_conversation_given(cfg, st_):
    fake = _fake()
    chatbot.ask(cfg, st_, "question one", history=[{"role": "user", "content": "stale history"}],
                conversation="C", transport=fake.transport)
    thread = _thread_messages(fake.requests[0])
    assert [m["content"] for m in thread] == ["question one"]


# ---------------------------------------------------------- truncation (AC2/AC4)
def test_truncate_context_message_limit():
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(6)]
    kept = chatbot.truncate_context(msgs, 4)
    assert [m["content"] for m in kept] == ["m2", "m3", "m4", "m5"]
    assert len(msgs) == 6  # input not mutated


def test_truncate_context_token_budget():
    msgs = [{"role": "user", "content": "x" * 400},   # 100 tokens
            {"role": "assistant", "content": "y" * 400},
            {"role": "user", "content": "z" * 40}]    # 10 tokens
    kept = chatbot.truncate_context(msgs, 10, max_tokens=150)
    # 100 + 100 + 10 = 210 > 150 -> drop oldest; 100 + 10 = 110 <= 150
    assert [m["content"][0] for m in kept] == ["y", "z"]


def test_truncate_context_reserved_tokens():
    msgs = [{"role": "user", "content": "a" * 200},   # 50 tokens
            {"role": "assistant", "content": "b" * 200},
            {"role": "user", "content": "c" * 40}]    # 10 tokens
    kept = chatbot.truncate_context(msgs, 10, max_tokens=200, reserved_tokens=150)
    # 150 + 50 + 50 + 10 = 260 > 200 -> drop oldest; 150 + 50 + 10 = 210 > 200
    # -> drop again; 150 + 10 = 160 <= 200
    assert [m["content"][0] for m in kept] == ["c"]
    assert 150 + chatbot.estimate_tokens(kept) <= 200


def test_truncate_context_reserved_default_zero():
    msgs = [{"role": "user", "content": "x" * 400},
            {"role": "assistant", "content": "y" * 400},
            {"role": "user", "content": "z" * 40}]
    kept = chatbot.truncate_context(msgs, 10, max_tokens=150)
    assert [m["content"][0] for m in kept] == ["y", "z"]


def test_truncate_context_newest_always_kept():
    msgs = [{"role": "user", "content": "tiny"},
            {"role": "user", "content": "big " * 5000}]
    kept = chatbot.truncate_context(msgs, 5, max_tokens=10)
    assert len(kept) == 1 and kept[0]["content"].startswith("big")


def test_estimate_tokens():
    assert chatbot.estimate_tokens([{"content": ""}]) == 1
    assert chatbot.estimate_tokens([{"content": "x" * 8}, {"content": "y" * 4}]) == 3


def test_long_thread_truncated_in_request(cfg, st_):
    cfg.chat.context_max_messages = 4
    for i in range(3):
        st_.conversation_append("L", "user", f"seed question {i}")
        st_.conversation_append("L", "assistant", f"seed answer {i}")
    fake = _fake()
    chatbot.ask(cfg, st_, "the newest question", conversation="L", transport=fake.transport)
    msgs = fake.requests[0]["messages"]
    thread = _thread_messages(fake.requests[0])
    # 6 seeds + the new question = 7 stored messages -> exactly the newest 4 are sent
    assert [m["content"] for m in thread] == [
        "seed answer 1", "seed question 2", "seed answer 2", "the newest question"]
    assert len(msgs) == len(thread) + 1  # + system snapshot


# ------------------------------------------------------ cost attribution (AC3)
def test_cost_attributed_per_conversation(cfg, st_):
    fake = _fake()
    res = chatbot.ask(cfg, st_, "how much did we spend?", conversation="A", transport=fake.transport)
    assert res["conversation"] == "A"
    summary = st_.cost_summary("chat:A")
    assert summary["total"] > 0
    assert res["usage"] == summary
    rows = st_.llm_calls("chat:A")
    assert rows and rows[0]["run_id"] == "chat:A" and rows[0]["task_key"] == "chatbot"
    # an untouched conversation reports zero cost
    assert st_.cost_summary("chat:B")["total"] == 0


def test_failed_call_keeps_user_message_without_assistant(cfg, st_):
    def handler(body):
        raise httpx.ConnectError("connection refused")
    fake = FakeProvider({"qwen-fake": handler})
    with pytest.raises(chatbot.ChatError):
        chatbot.ask(cfg, st_, "will fail", conversation="F", transport=fake.transport)
    msgs = st_.conversation_messages("F")
    assert [m["role"] for m in msgs] == ["user"] and msgs[0]["content"] == "will fail"


# ------------------------------------------------- backward compatibility (AC6)
def test_ask_without_conversation_keeps_old_contract(cfg, st_):
    fake = _fake()
    res = chatbot.ask(cfg, st_, "plain question", transport=fake.transport)
    assert set(res) == {"answer", "role", "model", "question", "actions"}
    assert res["answer"] == "ok"
    # no persistence happened
    assert st_.conversations_list() == []
    assert st_.cost_summary("chat:cli")["total"] == 0
