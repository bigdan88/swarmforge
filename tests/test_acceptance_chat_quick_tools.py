"""Acceptance tests: Chat page quick-actions + collapsible tool transcript.

Issue: "Chat page: paste-a-URL quick-action + collapsible tool transcript".

Covers AC1 (URL -> fetch_url hint), AC2 (/repo, /code, /issues expansion),
AC3 (collapsible 'Tools used' expander), AC4 (`chatbot.tools` config gate)
and AC5 (chat_with_tools `trace` + answer()['tools_used'] in both branches).
AC6 (the pre-existing suite still passes) is verified by running the full
suite; the tests here are purely additive.
"""

import asyncio

import pytest
import yaml

from swarmforge import chat_quick
from swarmforge.chat_tools import chat_with_tools
from swarmforge.chatbot import ask, build_chat_registry, create_conversation
from swarmforge.config import parse_config
from swarmforge.db import Store
from swarmforge.tools.web import WebToolError

from .conftest import ROOT, FakeProvider, base_raw, tool_call

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from .test_dashboard import APP, _go, _write_cfg


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    return parse_config(base_raw(tmp_path / "work"))


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "db.sqlite")


def _stub_fetch(monkeypatch, text="# Repo\n\nHello from the README"):
    async def fake_fetch_url(url, max_chars=8000, extract="markdown", **kw):
        return text

    monkeypatch.setattr("swarmforge.tools.web.fetch_url", fake_fetch_url)


def _failing_fetch(monkeypatch):
    async def fake_fetch_url(url, max_chars=8000, extract="markdown", **kw):
        raise WebToolError("robots.txt disallows fetching this URL")

    monkeypatch.setattr("swarmforge.tools.web.fetch_url", fake_fetch_url)


class _ScriptedLLM:
    """Minimal duck-typed LLM client for chat_with_tools unit tests."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.calls: list[dict] = []

    async def chat(self, role, messages, tools=None, task_key=None):
        self.calls.append(
            {"role": role, "messages": messages, "tools": tools, "task_key": task_key}
        )
        assert self._messages, "scripted LLM responses exhausted"
        return type("Resp", (), {"message": self._messages.pop(0)})()


# ============================================================== AC1: URL hint


def test_ac1_url_in_input_expands_with_fetch_url_hint():
    typed = "what does https://example.com/doc say?"
    out = chat_quick.expand_prompt(typed)
    assert out.startswith(typed)  # the user's question is preserved
    assert "fetch_url" in out  # tool hint added
    assert "https://example.com/doc" in out  # the detected URL is cited
    # idempotent: expanding the expansion adds no second hint
    assert chat_quick.expand_prompt(out) == out


def test_ac1_hint_for_url_input_mentions_fetch_url():
    hint = chat_quick.hint_for("see https://example.com/a")
    assert hint is not None
    assert "fetch_url" in hint


def test_ac1_plain_input_is_untouched():
    assert chat_quick.expand_prompt("plain question") == "plain question"
    assert chat_quick.hint_for("plain question") is None


def test_ac1_apptest_url_paste_sends_expanded_question_and_hint_caption(
    tmp_path, monkeypatch
):
    calls = []

    def fake_ask(c, s, question, history=None, **kw):
        calls.append(question)
        return {
            "answer": "ok",
            "role": "planner",
            "model": "qwen-fake",
            "question": question,
        }

    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)
    from swarmforge import chatbot

    monkeypatch.setattr(chatbot, "ask", fake_ask)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _go(at, "Chat")
    assert not at.exception, [e.value for e in at.exception]
    typed = "Summarize https://github.com/o/r please"
    at.chat_input[0].set_value(typed).run()
    assert not at.exception, [e.value for e in at.exception]
    # the question recorded by ask() is exactly the expansion with the fetch_url hint
    assert calls == [chat_quick.expand_prompt(typed)]
    assert "fetch_url" in calls[0] and "https://github.com/o/r" in calls[0]
    # a hint caption is rendered and the bubble shows what the user typed
    assert any("fetch_url hint" in c.value for c in at.caption)
    assert any(typed in m.value for m in at.markdown)


# ==================================================== AC2: slash-commands


def test_ac2_repo_expands_to_structured_prompt():
    out = chat_quick.expand_prompt("/repo foo/bar")
    assert out == chat_quick.expand_prompt("/repo foo/bar")  # deterministic template
    assert "https://github.com/foo/bar" in out
    assert "fetch_url" in out
    assert "foo/bar" in out


def test_ac2_code_expands_to_structured_prompt():
    out = chat_quick.expand_prompt("/code the retry loop")
    assert out == chat_quick.expand_prompt("/code the retry loop")
    assert "the retry loop" in out
    assert "Explain" in out
    assert "fetch_url" in out


def test_ac2_issues_expands_to_structured_prompt():
    out = chat_quick.expand_prompt("/issues foo/bar")
    assert out == chat_quick.expand_prompt("/issues foo/bar")
    assert "https://github.com/foo/bar/issues" in out
    assert "fetch_url" in out


def test_ac2_apptest_slash_commands_expand_via_recorded_ask(tmp_path, monkeypatch):
    calls = []

    def fake_ask(c, s, question, history=None, **kw):
        calls.append(question)
        return {
            "answer": "ok",
            "role": "planner",
            "model": "qwen-fake",
            "question": question,
        }

    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)
    from swarmforge import chatbot

    monkeypatch.setattr(chatbot, "ask", fake_ask)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _go(at, "Chat")
    assert not at.exception, [e.value for e in at.exception]
    at.chat_input[0].set_value("/repo foo/bar").run()
    assert not at.exception, [e.value for e in at.exception]
    at.chat_input[0].set_value("/code the retry loop").run()
    assert not at.exception, [e.value for e in at.exception]
    at.chat_input[0].set_value("/issues foo/bar").run()
    assert not at.exception, [e.value for e in at.exception]
    # each recorded ask() call received the deterministic expansion
    assert calls[0] == chat_quick.expand_prompt("/repo foo/bar")
    assert "https://github.com/foo/bar" in calls[0] and "fetch_url" in calls[0]
    assert calls[1] == chat_quick.expand_prompt("/code the retry loop")
    assert "the retry loop" in calls[1]
    assert calls[2] == chat_quick.expand_prompt("/issues foo/bar")
    assert "https://github.com/foo/bar/issues" in calls[2]


# ================================================ AC3: Tools used expander


def _chat_app_with_answer(tmp_path, monkeypatch, result):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)
    from swarmforge import chatbot

    def fake_ask(c, s, question, history=None, **kw):
        return {"role": "planner", "model": "qwen-fake", "question": question, **result}

    monkeypatch.setattr(chatbot, "ask", fake_ask)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _go(at, "Chat")
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_ac3_apptest_tools_used_expander_lists_calls(tmp_path, monkeypatch):
    at = _chat_app_with_answer(
        tmp_path,
        monkeypatch,
        {
            "answer": "done",
            "tools_used": [
                {
                    "name": "fetch_url",
                    "args": {"url": "https://x.example/a"},
                    "ok": True,
                    "preview": "# Doc",
                },
                {
                    "name": "fetch_url",
                    "args": {"url": "https://y.example/b"},
                    "ok": False,
                    "preview": "ERROR: boom",
                },
            ],
        },
    )
    at.chat_input[0].set_value("plain question").run()
    assert not at.exception, [e.value for e in at.exception]
    expanders = [e for e in at.expander if "Tools used" in e.label]
    assert len(expanders) == 1
    assert "(2)" in expanders[0].label
    md = [m.value for m in expanders[0].markdown]
    assert any("fetch_url" in m for m in md)
    assert any("ok" in m for m in md)  # success marker for the first call
    assert any("error" in m for m in md)  # failure marker for the second call
    codes = [c.value for c in expanders[0].code]
    assert any("https://x.example/a" in c for c in codes)
    assert any("https://y.example/b" in c for c in codes)
    caps = [c.value for c in expanders[0].caption]
    assert any("# Doc" in c for c in caps)
    assert any("ERROR: boom" in c for c in caps)


def test_ac3_apptest_no_expander_when_result_lacks_tools(tmp_path, monkeypatch):
    at = _chat_app_with_answer(tmp_path, monkeypatch, {"answer": "done"})
    at.chat_input[0].set_value("plain question").run()
    assert not at.exception, [e.value for e in at.exception]
    assert not [e for e in at.expander if "Tools used" in e.label]


def test_ac3_apptest_no_expander_when_tools_used_empty(tmp_path, monkeypatch):
    at = _chat_app_with_answer(
        tmp_path, monkeypatch, {"answer": "done", "tools_used": []}
    )
    at.chat_input[0].set_value("another plain question").run()
    assert not at.exception, [e.value for e in at.exception]
    assert not [e for e in at.expander if "Tools used" in e.label]


# ============================================= AC4: chatbot.tools config gate


def test_ac4_default_config_registers_fetch_url(cfg):
    assert cfg.chatbot.tools is None
    assert set(build_chat_registry(cfg).names()) == {"fetch_url", "list_files", "repo_tree", "repo_file", "repo_search", "repo_issues"}


def test_ac4_empty_tools_list_yields_empty_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"tools": []}
    cfg = parse_config(raw)
    assert build_chat_registry(cfg).names() == []


def test_ac4_only_listed_tools_are_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"tools": ["nope", "fetch_url"]}
    cfg = parse_config(raw)
    # unknown names are ignored; only the listed, known tool is registered
    assert build_chat_registry(cfg).names() == ["fetch_url"]


def test_ac4_example_yaml_has_chatbot_tools_section():
    raw = yaml.safe_load((ROOT / "swarmforge.example.yaml").read_text(encoding="utf-8"))
    assert raw["chatbot"]["tools"] is None
    cfg = parse_config(raw)
    assert cfg.chatbot.tools is None
    assert set(build_chat_registry(cfg).names()) == {"fetch_url", "list_files", "repo_tree", "repo_file", "repo_search", "repo_issues"}


# ================================================= AC5: trace + tools_used


def test_ac5_chat_with_tools_appends_trace_per_executed_call(cfg, monkeypatch):
    _stub_fetch(monkeypatch, "R" * 300)
    llm = _ScriptedLLM(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call("fetch_url", {"url": "https://a.example/1"}, "c1"),
                    tool_call("fetch_url", {"url": "https://b.example/2"}, "c2"),
                ],
            },
            {"role": "assistant", "content": "done"},
        ]
    )
    trace: list = []
    out = asyncio.run(
        chat_with_tools(
            llm,
            "planner",
            [{"role": "user", "content": "q"}],
            build_chat_registry(cfg),
            trace=trace,
        )
    )
    assert out == "done"
    assert len(trace) == 2  # one entry per executed call
    for entry, url in zip(trace, ["https://a.example/1", "https://b.example/2"]):
        assert set(entry) == {"name", "args", "ok", "preview"}
        assert entry["name"] == "fetch_url"
        assert entry["args"] == {"url": url}
        assert entry["ok"] is True
        assert entry["preview"] == "R" * 160  # preview capped at 160 chars


def test_ac5_chat_with_tools_trace_flags_error_calls(cfg, monkeypatch):
    _failing_fetch(monkeypatch)
    llm = _ScriptedLLM(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call("fetch_url", {"url": "https://x.example"}, "c1")
                ],
            },
            {"role": "assistant", "content": "sorry"},
        ]
    )
    trace: list = []
    out = asyncio.run(
        chat_with_tools(
            llm,
            "planner",
            [{"role": "user", "content": "q"}],
            build_chat_registry(cfg),
            trace=trace,
        )
    )
    assert out == "sorry"
    assert len(trace) == 1
    assert trace[0]["ok"] is False
    assert trace[0]["preview"].startswith("ERROR:")


def test_ac5_chat_with_tools_without_trace_is_unchanged(cfg, monkeypatch):
    _stub_fetch(monkeypatch)
    llm = _ScriptedLLM(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call("fetch_url", {"url": "https://a.example"}, "c1")
                ],
            },
            {"role": "assistant", "content": "done"},
        ]
    )
    out = asyncio.run(
        chat_with_tools(
            llm, "planner", [{"role": "user", "content": "q"}], build_chat_registry(cfg)
        )
    )
    assert out == "done"


def test_ac5_answer_history_branch_returns_tools_used(cfg, store, monkeypatch):
    _stub_fetch(monkeypatch, "# Doc\n\nDocs content")
    responses = iter(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call("fetch_url", {"url": "https://docs.example.com/a"})
                ],
            },
            {"role": "assistant", "content": "Docs content"},
        ]
    )
    fake = FakeProvider({"qwen-fake": lambda body: next(responses)})
    res = ask(
        cfg,
        store,
        "Summarize https://docs.example.com/a",
        history=[{"role": "user", "content": "earlier"}],
        transport=fake.transport,
    )
    assert "conversation" not in res  # history branch
    assert res["answer"] == "Docs content"
    assert len(res["tools_used"]) == 1
    entry = res["tools_used"][0]
    assert entry["name"] == "fetch_url"
    assert entry["args"] == {"url": "https://docs.example.com/a"}
    assert entry["ok"] is True


def test_ac5_answer_conversation_branch_returns_tools_used(cfg, store, monkeypatch):
    _stub_fetch(monkeypatch, "# Doc\n\nDocs content")
    conv = create_conversation(store)
    responses = iter(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call("fetch_url", {"url": "https://docs.example.com/a"})
                ],
            },
            {"role": "assistant", "content": "Docs content"},
        ]
    )
    fake = FakeProvider({"qwen-fake": lambda body: next(responses)})
    res = ask(
        cfg,
        store,
        "Summarize https://docs.example.com/a",
        conversation=conv["id"],
        transport=fake.transport,
    )
    assert res["conversation"] == conv["id"]  # conversation branch
    assert res["answer"] == "Docs content"
    assert res["tools_used"][0]["name"] == "fetch_url"
    assert res["tools_used"][0]["ok"] is True


def test_ac5_answer_tools_used_empty_when_no_tools_called(cfg, store):
    fake = FakeProvider(
        {"qwen-fake": lambda body: {"role": "assistant", "content": "2 runs finished"}}
    )
    res = ask(cfg, store, "How many runs?", transport=fake.transport)
    assert res["tools_used"] == []


# ============================== F2: load-time caption + persisted expander


def _write_cfg_raw(tmp_path, monkeypatch, raw):
    """Like test_dashboard._write_cfg but with a custom raw config dict."""
    import streamlit as st

    st.cache_resource.clear()
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    monkeypatch.setenv("SWARMFORGE_CONFIG", str(p))
    monkeypatch.setenv("SWARMFORGE_DASH_AUTOREFRESH", "0")
    monkeypatch.setenv("SWARMFORGE_DASH_UPDATE_CHECK", "0")
    monkeypatch.delenv("SWARMFORGE_DB", raising=False)
    return parse_config(raw)


def test_f2_quick_actions_caption_on_load(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _go(at, "Chat")
    assert not at.exception, [e.value for e in at.exception]
    # the affordance is visible immediately, before any interaction
    caps = [c.value for c in at.caption]
    assert any(
        "fetch_url" in c and "/repo" in c and "/code" in c and "/issues" in c
        for c in caps
    )


def test_f2_caption_omits_fetch_url_when_tools_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"tools": []}
    cfg = _write_cfg_raw(tmp_path, monkeypatch, raw)
    _seed(cfg)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _go(at, "Chat")
    assert not at.exception, [e.value for e in at.exception]
    caps = [c.value for c in at.caption]
    quick = [c for c in caps if "Quick actions" in c]
    assert quick, caps
    assert "fetch_url" not in quick[0]  # disabled tool is not advertised
    assert "/repo" in quick[0]  # slash-command affordance still appears


def test_f2_tools_used_expander_persists_across_reruns(tmp_path, monkeypatch):
    trace = [
        {
            "name": "fetch_url",
            "args": {"url": "https://x.example/a"},
            "ok": True,
            "preview": "# Doc",
        }
    ]
    results = iter(
        [
            {"answer": "traced answer", "tools_used": trace},
            {"answer": "plain answer"},  # second send: no tools_used
        ]
    )

    def fake_ask(c, s, question, history=None, **kw):
        return {
            "role": "planner",
            "model": "qwen-fake",
            "question": question,
            **next(results),
        }

    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)
    from swarmforge import chatbot

    monkeypatch.setattr(chatbot, "ask", fake_ask)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _go(at, "Chat")
    assert not at.exception, [e.value for e in at.exception]
    at.chat_input[0].set_value("first question").run()
    assert not at.exception, [e.value for e in at.exception]
    expanders = [e for e in at.expander if "Tools used" in e.label]
    assert len(expanders) == 1
    assert "(1)" in expanders[0].label

    # a second, untraced exchange must keep the earlier expander (exactly once)
    at.chat_input[0].set_value("second question").run()
    assert not at.exception, [e.value for e in at.exception]
    expanders = [e for e in at.expander if "Tools used" in e.label]
    assert len(expanders) == 1
    assert "(1)" in expanders[0].label
    assert any("traced answer" in m.value for m in at.markdown)
    assert any("plain answer" in m.value for m in at.markdown)

    # a plain rerun (page-reload style) must not duplicate the expander
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    expanders = [e for e in at.expander if "Tools used" in e.label]
    assert len(expanders) == 1
    assert "(1)" in expanders[0].label


def test_f2_clear_removes_persisted_tools_expander(tmp_path, monkeypatch):
    trace = [
        {
            "name": "fetch_url",
            "args": {"url": "https://x.example/a"},
            "ok": True,
            "preview": "# Doc",
        }
    ]

    def fake_ask(c, s, question, history=None, **kw):
        return {
            "answer": "traced answer",
            "role": "planner",
            "model": "qwen-fake",
            "question": question,
            "tools_used": trace,
        }

    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)
    from swarmforge import chatbot

    monkeypatch.setattr(chatbot, "ask", fake_ask)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _go(at, "Chat")
    assert not at.exception, [e.value for e in at.exception]
    at.chat_input[0].set_value("question with tools").run()
    assert not at.exception, [e.value for e in at.exception]
    assert len([e for e in at.expander if "Tools used" in e.label]) == 1

    reset = next((b for b in at.button if b.key == "chat_reset"), None)
    assert reset is not None
    reset.click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert not [e for e in at.expander if "Tools used" in e.label]
    assert not any("traced answer" in m.value for m in at.markdown)


# --------------------------------------------------------- AppTest plumbing


def _seed(cfg) -> Store:
    """One finished run in the store the dashboard reads."""
    store = Store(cfg.db_path)
    store.create_run(
        "20990101-000000-aaaaaa",
        state="COMPLETED",
        issue_title="Export CSV",
        repo="x/y",
        budget_usd=5,
        state_reason="done",
        config_json="{}",
    )
    return store
