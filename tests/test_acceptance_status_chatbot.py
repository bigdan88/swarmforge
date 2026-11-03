"""Acceptance tests for the runs/swarm-status chatbot (issue: "Add a chatbot that can
answer questions about runs and swarms statuses").

Fully offline per repo conventions: httpx.MockTransport (FakeProvider) or a
monkeypatched LLMClient.chat / chatbot.ask — no network, no real API keys.
"""
import io
import sys
import time

import pytest
import yaml

from swarmforge import chatbot, llm
from swarmforge.chatbot import ChatError, ask, status_snapshot
from swarmforge.cli import main
from swarmforge.config import parse_config
from swarmforge.db import Store

from .conftest import FakeProvider, base_raw


def _rich_run(store: Store, run_id: str = "run-rich-00000000") -> None:
    now = time.time()
    store.create_run(
        run_id, created_at=now - 300, updated_at=now, finished_at=now, mode="swarm",
        issue_url="https://github.com/o/r/issues/1", issue_title="Add export", repo="o/r",
        base_branch="main", integration_branch="swarmforge/issue-1-abc", run_dir="/tmp/runs/r1",
        state="FAILED", state_reason="tests failed", repair_cycle=2, budget_usd=20.0,
        pr_url="https://github.com/o/r/pull/7", cancel_requested=0, plan_json=None,
        config_json=None, pid=None,
    )
    store.record_llm_call(run_id, role="planner", model="qwen-fake", cost_usd=1.234,
                          cost_source="provider_reported", prompt_tokens=100,
                          completion_tokens=20, total_tokens=120)
    store.upsert_task(run_id, "T1", cycle=1, kind="implement", title="Implement export",
                      description="d", role="developer_backend", model="deepseek-fake",
                      depends_on=[], acceptance_criteria=["x"], expected_files=["a.py"],
                      state="INTEGRATED", branch="b", worktree="/tmp/wt/T1",
                      started_at=now - 100, ended_at=now - 50, attempts=1,
                      summary="exporter added", error=None)


@pytest.fixture
def rich_store(tmp_path) -> Store:
    store = Store(tmp_path / "rich.sqlite")
    _rich_run(store)
    store.failure_save("run-rich-00000000", title="Add export", repo="o/r", state="FAILED",
                       category="test_failure", root_cause="flaky csv test",
                       outcome="fixed", retry=0)
    store.queue_add("Fix login", "body", "o/r", status="queued")
    return store


# ------------------------------------------------- AC1: snapshot content + chat role

def test_snapshot_covers_run_details_and_task_roles(rich_store):
    snap = status_snapshot(rich_store)
    # run identity: id prefix, state and reason
    assert "Run run-rich" in snap and "FAILED - tests failed" in snap
    # mode, issue and repo
    assert "mode: swarm" in snap and "repo: o/r" in snap and "issue: Add export" in snap
    # cost (from recorded llm calls) against budget, repair cycle and PR
    assert "cost: $1.234 of $20.00" in snap
    assert "repair cycle: 2" in snap
    assert "pr: https://github.com/o/r/pull/7" in snap
    # per-task swarm status: state, task key, role, model, cycle
    assert "[INTEGRATED] T1 (developer_backend, deepseek-fake, cycle 1)" in snap
    assert "exporter added" in snap
    # recent failures (post-mortems)
    assert "test_failure" in snap and "root cause: flaky csv test" in snap
    # queue items
    assert "[queued] Fix login (repo: o/r)" in snap
    assert "Pending approvals: 0" in snap


def test_ask_answers_through_configured_chat_role(rich_store, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    raw["roles"]["chat"] = {"provider": "fake", "model": "chat-fake"}
    cfg = parse_config(raw)
    fake = FakeProvider({"chat-fake": lambda body: {"role": "assistant",
                                                    "content": "1 run failed: tests failed"}})
    res = ask(cfg, rich_store, "what happened?", transport=fake.transport)
    assert res == {"answer": "1 run failed: tests failed", "role": "chat",
                   "model": "chat-fake", "question": "what happened?", "actions": {"tasks": [], "fixes": []},
                   "tools_used": []}
    # the request really went to the chat role's model with the snapshot attached
    assert fake.requests[0]["model"] == "chat-fake"
    sys_msg = fake.requests[0]["messages"][0]
    assert sys_msg["role"] == "system" and "<status>" in sys_msg["content"]
    assert "FAILED - tests failed" in sys_msg["content"]


# ------------------------------------------------------- AC2/AC4: CLI missing key path

def _capture_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return out.getvalue(), err.getvalue(), code


def test_cli_ask_without_api_key_exits_2_friendly(tmp_path, monkeypatch):
    """Real end-to-end CLI path (no monkeypatched chatbot): missing provider key
    surfaces as a friendly message on stderr and exit code 2, not a traceback."""
    monkeypatch.delenv("FAKE_KEY", raising=False)
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(base_raw(tmp_path / "work")))
    out, err, code = _capture_cli(["--config", str(p), "ask", "how are the runs?"])
    assert code == 2
    assert out == ""
    assert "chatbot:" in err and "FAKE_KEY" in err
    assert "Traceback" not in err


# ------------------------------------------------- AC4: LLMError/BudgetExceeded -> ChatError

def _patch_chat(monkeypatch, exc):
    async def boom(self, role, messages, **kw):
        raise exc
    monkeypatch.setattr(llm.LLMClient, "chat", boom)


def test_llm_error_surfaces_as_chat_error(rich_store, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    cfg = parse_config(base_raw(tmp_path / "work"))
    _patch_chat(monkeypatch, llm.LLMError("provider exploded"))
    with pytest.raises(ChatError, match="provider exploded"):
        ask(cfg, rich_store, "status?")


def test_budget_exceeded_surfaces_as_chat_error(rich_store, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    cfg = parse_config(base_raw(tmp_path / "work"))
    _patch_chat(monkeypatch, llm.BudgetExceeded("budget $2.00 exhausted (spent $2.0001)"))
    with pytest.raises(ChatError, match=r"budget \$2\.00 exhausted"):
        ask(cfg, rich_store, "status?")


# ------------------------------------- AC3/AC4: dashboard renders chat messages, recovers

def test_dashboard_chat_messages_rendered_and_page_recovers(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from .test_dashboard import APP, _go, _write_cfg

    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    store.create_run("20990101-000001-bbbbbbbb", state="COMPLETED", issue_title="Export CSV",
                     repo="x/y", budget_usd=5, state_reason="done", config_json="{}")
    calls = {"n": 0}

    def flaky_ask(c, s, question, history=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ChatError("model provider unavailable")
        return {"answer": "recovered answer", "role": "planner", "model": "qwen-fake",
                "question": question}

    monkeypatch.setattr(chatbot, "ask", flaky_ask)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _go(at, "Chat")
    assert not at.exception, [e.value for e in at.exception]

    # first question fails: st.error, no exception, page stays usable
    at.chat_input[0].set_value("which runs failed?").run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Chatbot: model provider unavailable" in e.value for e in at.error)

    # second question succeeds: user + assistant messages rendered via st.chat_message
    at.chat_input[0].set_value("and why?").run()
    assert not at.exception, [e.value for e in at.exception]
    names = [m.name for m in at.chat_message]
    assert "user" in names and "assistant" in names
    assert any("recovered answer" in m.value for m in at.markdown)
    # history is kept in session state and passed to the chatbot core
    assert calls["n"] == 2
    assert at.session_state["chat_history"][-1] == {"role": "assistant",
                                                    "content": "recovered answer"}
