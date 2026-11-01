"""Chat as the self-improvement driver: evidence, suggested tasks launched with one click, failed tasks
fixed with a chosen or written answer."""
import json
import time

import pytest

from swarmforge import chat_actions as CA
from swarmforge import chatbot
from swarmforge.db import Store
from swarmforge.knowledge import Vault

from .conftest import FakeProvider

REPLY = """Two things stand out: runs time out on slow tests, and the export task failed.

```actions
{"tasks": [{"title": "Cache the baseline per commit", "description": "Do X.\\n**Acceptance criteria**\\n- AC1", "why": "3 time_limit failures", "size": "s", "repo": "bigdan88/swarmforge"},
           {"title": "", "description": "missing title is dropped"}],
 "fixes": [{"run_id": "R_FAILED", "summary": "ran out of time in tests",
            "options": [{"label": "Give it 4 hours", "instructions": "Same task, more time.", "time_limit_h": 4, "budget_usd": null, "max_cycles": null, "continue_code": true},
                        {"label": "Smaller scope", "instructions": "Only do the CLI part.", "time_limit_h": 999}]},
           {"run_id": "nope", "summary": "unknown run", "options": [{"label": "x", "instructions": "y"}]}]}
```"""


@pytest.fixture
def st_(cfg):
    st = Store(cfg.db_path)
    st.create_run("R_FAILED", state="FAILED", issue_title="Export CSV", issue_body="Add CSV export.",
                  repo="bigdan88/app", state_reason="maximum runtime exceeded", max_runtime_s=5400,
                  budget_usd=10, max_repair_cycles=2)
    st.failure_save("R_FAILED", title="Export CSV", repo="bigdan88/app", category="time_limit",
                    root_cause="ran out of time running the full test suite", fix="Give it more time.",
                    lesson="Run only relevant tests.", retry=0, outcome="needs you: not retried automatically")
    return st


def test_context_has_the_evidence(cfg, st_):
    Vault(cfg.vault_path).write("Agents rerun the full suite", "Five minutes each time.", type="pitfall")
    st_.queue_add("Dark mode", "x", "bigdan88/swarmforge")
    ctx = chatbot.improvement_context(cfg, st_)
    assert "run R_FAILED" in ctx and "time_limit" in ctx and "resumable: False" in ctx
    assert "Agents rerun the full suite" in ctx and "Dark mode" in ctx
    assert "Features (README sections)" in ctx and "Modules:" in ctx and "selfupdate" in ctx


def test_parse_actions_validates(st_):
    text, acts = chatbot.parse_actions(REPLY.replace("R_FAILED", "R_FAILED"), st_)
    assert "```" not in text and text.startswith("Two things stand out")
    assert [t["title"] for t in acts["tasks"]] == ["Cache the baseline per commit"] and acts["tasks"][0]["size"] == "S"
    assert [f["run_id"] for f in acts["fixes"]] == ["R_FAILED"]                  # unknown run dropped
    opts = acts["fixes"][0]["options"]
    assert opts[0]["time_limit_h"] == 4 and opts[0]["continue_code"] and opts[1]["time_limit_h"] is None


async def test_answer_returns_actions_and_sends_context(cfg, st_, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    fake = FakeProvider({"qwen-fake": lambda b: {"content": REPLY}})
    res = await chatbot.answer(cfg, st_, "what should we improve?", transport=fake.transport)
    sys_msg = fake.requests[0]["messages"][0]["content"]
    assert "<improvement_context>" in sys_msg and "```actions" in sys_msg
    assert res["actions"]["tasks"][0]["title"] == "Cache the baseline per commit" and "```" not in res["answer"]


def test_launch_task_queue_or_now(cfg, st_):
    workers, runs = [], []
    task = {"title": "Cache the baseline", "description": "Do X.", "why": "slow", "repo": "bigdan88/swarmforge"}
    msg = CA.launch_task(cfg, st_, task, how="queue", queue="self-improvement", start_worker=lambda c, s: workers.append(1))
    item = [q for q in st_.queue_items() if q["title"] == "Cache the baseline"][0]
    assert "Queued" in msg and item["queue"] == "self-improvement" and item["chain"] == 0
    assert "Why (from the improvement chat): slow" in item["body"] and workers == [1]
    CA.launch_task(cfg, st_, task, how="now", start_run=lambda c, **kw: runs.append(kw) or ("RUN1", None))
    assert runs[0]["issue_title"] == "Cache the baseline" and "bigdan88/swarmforge" in runs[0]["repo"]


def test_retry_failed_queue_task_with_guidance_and_settings(cfg, st_):
    iid = st_.queue_add("Export CSV", "Add CSV export.", "https://github.com/bigdan88/app", max_runtime_s=5400)
    st_.queue_update(iid, status="failed", run_id="R_FAILED")
    msg = CA.retry_failure(cfg, st_, "R_FAILED", "Only do the CLI part.", time_limit_h=4, budget=15, max_cycles=3,
                           continue_code=True, start_worker=lambda c, s: None)
    new = [q for q in st_.queue_items() if q["status"] == "queued"][0]
    assert "Queued again" in msg and new["max_runtime_s"] == 4 * 3600 and new["budget"] == 15 and new["max_cycles"] == 3
    assert new["body"].startswith("Add CSV export.") and "Only do the CLI part." in new["body"]
    assert "ran out of time running the full test suite" in new["body"]
    assert new["seed_run_id"] is None                                   # nothing reusable -> starts fresh
    assert st_.failure("R_FAILED")["outcome"].startswith("retried from chat")
    assert chatbot.failures_needing_attention(st_) == []
    # retrying again replaces the guidance instead of stacking it
    CA.retry_failure(cfg, st_, "R_FAILED", "Second idea.", start_worker=lambda c, s: None)
    newest = sorted(st_.queue_items(), key=lambda q: q["id"])[-1]
    assert newest["body"].count(CA.GUIDANCE_HEADER) == 1 and "Second idea." in newest["body"]


def test_retry_direct_run_and_dismiss(cfg, st_):
    calls = []
    CA.retry_failure(cfg, st_, "R_FAILED", "More time.", time_limit_h=3,
                     start_run=lambda c, **kw: calls.append(kw) or ("RUN2", None))
    assert calls[0]["repo"] == "bigdan88/app" and "More time." in calls[0]["issue_body"] and calls[0]["max_runtime_s"] == 10800
    with pytest.raises(ValueError, match="write what the next attempt"):
        CA.retry_failure(cfg, st_, "R_FAILED", "  ")
    st_.failure_update("R_FAILED", outcome="needs you: x")
    CA.dismiss_failure(st_, "R_FAILED")
    assert chatbot.failures_needing_attention(st_) == []
    assert "Saved to the knowledge base" in CA.save_lesson(cfg, "Use squash merges", "One commit per task.")


def test_chat_page_buttons(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    from .test_dashboard import APP, _go, _write_cfg
    cfg2 = _write_cfg(tmp_path, monkeypatch)
    st2 = Store(cfg2.db_path)
    for r in ("R_FAILED",):
        st2.create_run(r, state="FAILED", issue_title="Export CSV", issue_body="Add CSV export.", repo="bigdan88/app",
                       max_runtime_s=5400, budget_usd=10, max_repair_cycles=2)
        st2.failure_save(r, title="Export CSV", category="time_limit", root_cause="slow tests", fix="more time",
                         retry=0, outcome="needs you: not retried automatically")
    iid = st2.queue_add("Export CSV", "Add CSV export.", "https://github.com/bigdan88/app")
    st2.queue_update(iid, status="failed", run_id="R_FAILED")
    asked = []

    def fake_ask(c, store, question, history=None, **kw):
        asked.append(question)
        text, acts = chatbot.parse_actions(REPLY, store)
        return {"answer": text, "role": "planner", "model": "qwen-fake", "question": question, "actions": acts}
    monkeypatch.setattr(chatbot, "ask", fake_ask)
    from swarmforge import runqueue as rq
    monkeypatch.setattr(rq, "start_worker", lambda c, s, keep_pause=False: None)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _go(at, "Chat")
    assert any("1 failed task(s) are waiting" in i.value for i in at.info)
    at.button(key="chat_q_failures").click().run()                           # one-click "review my failed tasks"
    assert not at.exception, [e.value for e in at.exception]
    assert asked and "failed tasks" in asked[0]
    at.radio(key="cf_pick_1_0").set_value(1).run()                      # pick "Smaller scope"
    assert at.text_area(key="cf_txt_1_0_1").value == "Only do the CLI part."
    at.button(key="cf_go_1_0").click().run()
    assert not at.exception, [e.value for e in at.exception]
    again = [q for q in Store(cfg2.db_path).queue_items() if q["status"] == "queued"]
    assert again and "Only do the CLI part." in again[0]["body"]
    at.button(key="ct_go_1").click().run()                              # launch the suggested task(s)
    assert not at.exception, [e.value for e in at.exception]
    titles = [q["title"] for q in Store(cfg2.db_path).queue_items()]
    assert "Cache the baseline per commit" in titles
    # the conversation and its buttons survive a reload
    at2 = AppTest.from_file(str(APP), default_timeout=60)
    at2.run()
    _go(at2, "Chat")
    assert len(at2.session_state["chat_history"]) == 2 and not at2.exception
