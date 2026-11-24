"""Context compaction for conversations (chat, project kickoff) and agent sessions."""
import asyncio

import pytest

from swarmforge import chatbot
from swarmforge.compaction import (
    CONVERSATION_PROMPT,
    compact_agent_history,
    compact_conversation,
    stored_summary,
)
from swarmforge.db import Store

from .conftest import FakeProvider


@pytest.fixture
def store(cfg):
    s = Store(cfg.db_path)
    yield s
    s.close()


def _messages(store, cid, n):
    for i in range(n):
        store.conversation_append(cid, "user" if i % 2 == 0 else "assistant", f"m{i}")
    return store.conversation_messages(cid)


def test_conversation_summary_is_incremental_and_survives_failures(store):
    calls = []

    async def chat(messages):
        calls.append(messages[1]["content"])
        return f"summary {len(calls)}"
    msgs = _messages(store, "c", 20)
    kept = msgs[-12:]
    assert asyncio.run(compact_conversation(store, "c", msgs, kept, chat)) == "summary 1"
    assert "m0" in calls[0] and "m7" in calls[0] and "m8" not in calls[0]
    assert stored_summary(store, "c")["through_message_id"] == msgs[7]["id"]
    # Nothing new dropped out of the window: the stored summary is reused without a model call.
    assert asyncio.run(compact_conversation(store, "c", msgs, kept, chat)) == "summary 1" and len(calls) == 1
    # Two more messages push m8 and m9 out: only they are summarized, with the previous summary.
    msgs = _messages(store, "c", 2) and store.conversation_messages("c")
    assert asyncio.run(compact_conversation(store, "c", msgs, msgs[-12:], chat)) == "summary 2"
    assert "Previous summary:\nsummary 1" in calls[1] and "m8" in calls[1] and "m0" not in calls[1]

    async def broken(messages):
        raise RuntimeError("provider down")
    msgs = _messages(store, "c", 2) and store.conversation_messages("c")
    assert asyncio.run(compact_conversation(store, "c", msgs, msgs[-12:], broken)) == "summary 2"
    # A short conversation needs no summary at all.
    assert asyncio.run(compact_conversation(store, "short", _messages(store, "short", 3), store.conversation_messages("short"), chat)) is None


def _agent_transcript(turns):
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for t in range(turns):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{t}", "type": "function", "function": {"name": "read_file", "arguments": f'{{"path": "f{t}.py"}}'}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{t}", "content": "x" * 50})
    return messages


def test_agent_compaction_keeps_tool_pairs_and_recent_turns():
    messages = _agent_transcript(12)

    async def chat(msgs):
        assert "read_file" in msgs[1]["content"]
        return "Read f0..f5; nothing changed yet."
    assert asyncio.run(compact_agent_history(messages, chat, keep_turns=6))
    assert messages[0]["content"] == "sys" and messages[1]["content"] == "task"
    assert messages[2]["role"] == "user" and "Read f0..f5" in messages[2]["content"]
    assert [m["tool_calls"][0]["id"] for m in messages if m.get("tool_calls")] == [f"c{t}" for t in range(6, 12)]
    open_calls = set()
    for m in messages:  # every tool result follows the assistant call that produced it
        for call in m.get("tool_calls") or []:
            open_calls.add(call["id"])
        if m["role"] == "tool":
            assert m["tool_call_id"] in open_calls
    assert not asyncio.run(compact_agent_history(_agent_transcript(4), chat, keep_turns=6))


def test_agent_compaction_falls_back_to_a_marker_but_never_hides_budget_errors():
    from swarmforge.llm import BudgetExceeded
    messages = _agent_transcript(10)

    async def broken(msgs):
        raise RuntimeError("timeout")
    assert asyncio.run(compact_agent_history(messages, broken, keep_turns=3))
    assert "were dropped" in messages[2]["content"] and len([m for m in messages if m.get("tool_calls")]) == 3

    async def broke(msgs):
        raise BudgetExceeded("allowance exhausted")
    with pytest.raises(BudgetExceeded):
        asyncio.run(compact_agent_history(_agent_transcript(10), broke, keep_turns=3))


def test_long_chat_sends_a_summary_of_older_messages_to_the_model(cfg, store):
    cfg.chat.context_max_messages = 4
    for i in range(6):
        store.conversation_append("L", "user", f"early fact {i}")
        store.conversation_append("L", "assistant", f"noted {i}")
    fake = FakeProvider({"qwen-fake": lambda body: {"content": "SUMMARY: early facts 0-3" if body["messages"][0]["content"] == CONVERSATION_PROMPT else "answer"}})
    result = chatbot.ask(cfg, store, "what did I say first?", conversation="L", transport=fake.transport)
    assert result["answer"] == "answer"
    from swarmforge.chatbot import TITLE_PROMPT
    replies = [r for r in fake.requests if r["messages"][0]["content"] != TITLE_PROMPT]
    summary_req, answer_req = replies[0], replies[-1]
    assert summary_req["messages"][0]["content"] == CONVERSATION_PROMPT and "early fact 0" in summary_req["messages"][1]["content"]
    systems = [m["content"] for m in answer_req["messages"] if m["role"] == "system"]
    assert any("SUMMARY: early facts 0-3" in s for s in systems)
    assert "early fact 0" not in [m["content"] for m in answer_req["messages"] if m["role"] != "system"]
    assert store.cost_summary("chat:L")["total"] >= 0  # both calls are charged to this conversation
