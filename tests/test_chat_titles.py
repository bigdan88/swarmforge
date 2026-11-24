"""Conversations can be renamed, and unnamed ones get a title after their first reply."""
import asyncio

import pytest

from swarmforge import chatbot
from swarmforge.chatbot import TITLE_PROMPT, auto_title, heuristic_title
from swarmforge.db import Store
from swarmforge.services.dashboard import DashboardActions

from .conftest import FakeProvider


@pytest.fixture
def store(cfg):
    s = Store(cfg.db_path)
    yield s
    s.close()


def test_heuristic_title_is_short_and_readable():
    assert heuristic_title("why is the **horse tinder** project stuck on milestone 3?") == "Why is the horse tinder project stuck on"
    assert heuristic_title("ok") == "Ok" and heuristic_title("   ") == "New chat"
    assert len(heuristic_title("x" * 300)) <= 48


def test_first_reply_names_the_conversation_with_the_chat_model(cfg, store):
    store.conversation_create("t1", "New chat")
    fake = FakeProvider({"qwen-fake": lambda b: {"content": '"Horse Tinder blocker."'} if b["messages"][0]["content"] == TITLE_PROMPT else {"content": "Docker is down."}})
    chatbot.ask(cfg, store, "why is horse tinder stuck?", conversation="t1", transport=fake.transport)
    assert store.conversation_get("t1")["title"] == "Horse Tinder blocker"
    title_req = fake.requests[-1]
    assert "why is horse tinder stuck?" in title_req["messages"][1]["content"] and "Docker is down." in title_req["messages"][1]["content"]
    # Named conversations are never renamed again, so later replies make no title call.
    before = len(fake.requests)
    chatbot.ask(cfg, store, "and now?", conversation="t1", transport=fake.transport)
    assert len(fake.requests) == before + 1 and store.conversation_get("t1")["title"] == "Horse Tinder blocker"


def test_title_falls_back_to_first_words_and_never_overrides_a_chosen_name(store):
    store.conversation_create("t2", "New chat")
    store.conversation_append("t2", "user", "Plan the kitchen remodel budget")
    store.conversation_append("t2", "assistant", "Sure")

    async def broken(messages):
        raise RuntimeError("provider down")
    assert asyncio.run(auto_title(store, "t2", broken)) == "Plan the kitchen remodel budget"
    store._exec("UPDATE conversations SET title='Mine' WHERE id='t2'")

    async def fine(messages):
        return "Other"
    assert asyncio.run(auto_title(store, "t2", fine)) is None and store.conversation_get("t2")["title"] == "Mine"


def test_rename_action_trims_and_validates(cfg, store):
    store.conversation_create("t3", "New chat")
    actions = DashboardActions(store, cfg)
    assert actions.execute("rename-conv-00000001", "rename_conversation", "t3", {"title": "  Release   notes  "})["ok"]
    assert store.conversation_get("t3")["title"] == "Release notes"
    assert not actions.execute("rename-conv-00000002", "rename_conversation", "t3", {"title": "   "})["ok"]
    assert not actions.execute("rename-conv-00000003", "rename_conversation", "missing", {"title": "x"})["ok"]
    assert not actions.execute("rename-conv-00000004", "rename_conversation", "t3", {"title": "x" * 121})["ok"]
