"""Offline tests for the chatbot's GitHub repo-analysis tool wiring."""

import asyncio

import pytest

from swarmforge.chatbot import SYSTEM_PROMPT, ask, build_chat_registry
from swarmforge.config import parse_config
from swarmforge.db import Store
from swarmforge.tools.github_repo import GitHubRepoError

from .conftest import FakeProvider, base_raw, tool_call

REPO_TOOLS = ["repo_tree", "repo_file", "repo_search", "repo_issues"]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    return parse_config(base_raw(tmp_path / "work"))


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "db.sqlite")


def _stub(monkeypatch, name, text):
    calls = []

    async def fake(*args, **kw):
        calls.append({"args": args, "kwargs": kw})
        return text

    monkeypatch.setattr(f"swarmforge.tools.github_repo.{name}", fake)
    return calls


def _failing(monkeypatch, name, message="boom"):
    async def fake(*args, **kw):
        raise GitHubRepoError(message)

    monkeypatch.setattr(f"swarmforge.tools.github_repo.{name}", fake)


def test_system_prompt_mentions_repo_tools():
    assert "repo_tree" in SYSTEM_PROMPT
    assert "repo_issues" in SYSTEM_PROMPT


def test_build_chat_registry_exposes_repo_tools(cfg):
    registry = build_chat_registry(cfg)
    names = registry.names()
    assert names[0] == "fetch_url"
    for name in REPO_TOOLS:
        assert name in names
    schemas = registry.schemas()
    assert schemas[0]["function"]["name"] == "fetch_url"
    by_name = {s["function"]["name"]: s["function"]["parameters"] for s in schemas}
    assert by_name["repo_tree"]["required"] == ["owner", "repo"]
    assert by_name["repo_file"]["required"] == ["owner", "repo", "path"]
    assert by_name["repo_search"]["required"] == ["owner", "repo", "query"]
    assert by_name["repo_issues"]["required"] == ["owner", "repo"]
    assert by_name["repo_issues"]["properties"]["state"]["enum"] == [
        "open",
        "closed",
        "all",
    ]
    for name in REPO_TOOLS:
        params = by_name[name]
        assert params["properties"]["owner"]["type"] == "string"
        assert params["properties"]["repo"]["type"] == "string"


@pytest.mark.parametrize("name", REPO_TOOLS)
def test_execute_repo_tool_returns_stub(cfg, monkeypatch, name):
    _stub(monkeypatch, name, "stub output")
    registry = build_chat_registry(cfg)
    args = {
        "repo_tree": {"owner": "o", "repo": "r", "path": "src"},
        "repo_file": {"owner": "o", "repo": "r", "path": "src/a.py"},
        "repo_search": {"owner": "o", "repo": "r", "query": "def main"},
        "repo_issues": {"owner": "o", "repo": "r", "state": "closed"},
    }[name]
    result = asyncio.run(registry.execute(name, args))
    assert result == "stub output"


@pytest.mark.parametrize("name", REPO_TOOLS)
def test_execute_repo_tool_error_is_error_string(cfg, monkeypatch, name):
    _failing(monkeypatch, name, "boom")
    registry = build_chat_registry(cfg)
    result = asyncio.run(
        registry.execute(name, {"owner": "o", "repo": "r", "path": "p", "query": "q"})
    )
    assert isinstance(result, str)
    assert result.startswith("ERROR:")


def test_answer_runs_repo_tool_loop(cfg, store, monkeypatch):
    calls = _stub(monkeypatch, "repo_issues", "- #7: something is broken")
    responses = iter(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call("repo_issues", {"owner": "o", "repo": "r"})],
            },
            {"role": "assistant", "content": "The repo has 1 open issue."},
        ]
    )
    fake = FakeProvider({"qwen-fake": lambda body: next(responses)})
    res = ask(
        cfg, store, "What issues does github.com/o/r have?", transport=fake.transport
    )
    assert res["answer"] == "The repo has 1 open issue."
    assert calls and calls[0]["args"][:2] == ("o", "r")
    # Round 2 request contains the tool result message.
    msgs = fake.requests[1]["messages"]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "something is broken" in tool_msgs[0]["content"]
    # Round 1 request offered the repo tool schemas.
    offered = {t["function"]["name"] for t in fake.requests[0]["tools"]}
    assert set(REPO_TOOLS) <= offered
