import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from swarmforge.agent.tools import ToolContext, ToolError, edit_file, read_file, write_file
from swarmforge.config import ConfigError, parse_config
from swarmforge.github import GitHubClient, GitHubError, check_branch_safe, parse_issue_url
from swarmforge.llm import Budget, BudgetExceeded, LLMClient, Usage, parse_usage
from swarmforge.plan import (PlanValidationError, Task, topo_waves, validate_plan, validate_review,
                             validate_test_report)
from swarmforge.scheduler import Scheduler
from swarmforge.state import InvalidTransition, RunState, StateMachine, TaskState
from swarmforge.testing import TestEvidence, evaluate_criteria, normalize_test_id

from .conftest import ROOT, base_raw


# ------------------------------------------------------------------ config
def test_example_config_parses(monkeypatch):
    import yaml
    raw = yaml.safe_load((ROOT / "swarmforge.example.yaml").read_text())
    cfg = parse_config(raw)
    assert cfg.roles["planner"].model.startswith("Qwen/")
    assert cfg.developer_roles == ["developer_backend", "developer_frontend"]
    assert cfg.limits.budget_usd == 20.0 and cfg.limits.max_repair_cycles == 3


def test_reviewer_cannot_share_developer_model(tmp_path):
    raw = base_raw(tmp_path)
    raw["roles"]["reviewer"]["model"] = "deepseek-fake"
    with pytest.raises(ConfigError, match="must not approve its own work"):
        parse_config(raw)
    raw["review"] = {"allow_same_model_review": True}
    parse_config(raw)


def test_unknown_config_key_rejected(tmp_path):
    raw = base_raw(tmp_path)
    raw["limits"]["budgett"] = 3
    with pytest.raises(ConfigError, match="unknown keys"):
        parse_config(raw)


# -------------------------------------------------------------------- plan
DEV = ["developer_backend", "developer_frontend"]


def _plan(tasks, ac=True):
    return {"summary": "s", "tasks": tasks,
            "acceptance_criteria": [{"id": "AC1", "text": "works"}] if ac else []}


def _t(i, deps=(), role="developer_backend"):
    return {"id": i, "title": i, "description": "d", "role": role, "depends_on": list(deps),
            "acceptance_criteria": ["x"], "expected_files": [f"{i}.py"]}


def test_plan_valid_and_waves():
    p = validate_plan(_plan([_t("T1"), _t("T2", role="developer_frontend"), _t("T3", ["T1", "T2"])]), DEV, 8)
    assert p.waves() == [["T1", "T2"], ["T3"]]


@pytest.mark.parametrize("tasks,msg", [
    ([_t("T1", ["T2"]), _t("T2", ["T1"])], "cycle"),
    ([_t("T1", ["T9"])], "unknown task"),
    ([_t("T1", role="hacker")], "must be one of"),
    ([_t("T1"), _t("T1")], "duplicated"),
])
def test_plan_invalid(tasks, msg):
    with pytest.raises(PlanValidationError, match=msg):
        validate_plan(_plan(tasks), DEV, 8)


def test_plan_rejects_escaping_paths_and_too_many():
    t = _t("T1")
    t["expected_files"] = ["../etc/passwd"]
    with pytest.raises(PlanValidationError, match="relative path"):
        validate_plan(_plan([t]), DEV, 8)
    with pytest.raises(PlanValidationError, match="too many"):
        validate_plan(_plan([_t(f"T{i}") for i in range(5)]), DEV, 3)


def test_fix_plan_may_depend_on_existing():
    p = validate_plan(_plan([_t("F1", ["T1"])], ac=False), DEV, 8, existing_ids={"T1"}, require_criteria=False)
    assert p.tasks[0].depends_on == ["T1"]


def test_review_validation_consistency():
    v, err = validate_review({"verdict": "approve", "summary": "", "findings": [
        {"severity": "major", "file": "a", "title": "t", "description": "d", "recommendation": "r"}]})
    assert v is None and "inconsistent" in err
    v, err = validate_review({"verdict": "request_changes", "summary": "x", "findings": []})
    assert err is None


def test_test_report_validation():
    v, err = validate_test_report({"summary": "s", "criteria_tests": [{"criterion_id": "AC9", "tests": []}],
                                   "defects": []}, {"AC1"})
    assert v is None and "AC9" in err


# ------------------------------------------------------------ state machine
def test_state_machine_rejects_illegal(tmp_path):
    from swarmforge.db import Store
    st = Store(tmp_path / "db.sqlite")
    st.create_run("r1")
    sm = StateMachine(st, "r1")
    with pytest.raises(InvalidTransition):
        sm.transition(RunState.APPROVED)
    sm.transition(RunState.REPOSITORY_INSPECTION)
    sm.transition(RunState.BUDGET_EXCEEDED, "x")
    assert sm.terminal
    with pytest.raises(InvalidTransition):
        sm.transition(RunState.PLANNING)


# ---------------------------------------------------------------- scheduler
async def test_scheduler_parallel_and_dependencies():
    events = []

    async def run(t: Task):
        events.append(("start", t.id, time.monotonic()))
        await asyncio.sleep(0.2)
        events.append(("end", t.id, time.monotonic()))
        return t.id != "T2"

    tasks = [Task("T1", "", "", "r"), Task("T2", "", "", "r"), Task("T3", "", "", "r", ["T1"]),
             Task("T4", "", "", "r", ["T2"]), Task("T5", "", "", "r", ["T4"])]
    res = await Scheduler(tasks, run, max_concurrency=2).run()
    assert res.max_parallel == 2
    assert res.states["T1"] == TaskState.SUCCEEDED and res.states["T3"] == TaskState.SUCCEEDED
    assert res.states["T2"] == TaskState.FAILED
    assert res.states["T4"] == TaskState.BLOCKED and res.states["T5"] == TaskState.BLOCKED
    start = {e[1]: e[2] for e in events if e[0] == "start"}
    end = {e[1]: e[2] for e in events if e[0] == "end"}
    assert start["T3"] >= end["T1"]
    assert abs(start["T1"] - start["T2"]) < 0.1  # truly concurrent


async def test_scheduler_respects_concurrency_limit():
    live = 0
    peak = 0

    async def run(t):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.05)
        live -= 1
        return True

    await Scheduler([Task(f"T{i}", "", "", "r") for i in range(6)], run, max_concurrency=3).run()
    assert peak == 3


# -------------------------------------------------------------------- tools
def test_tool_path_confinement(tmp_path):
    ctx = ToolContext(workspace=tmp_path)
    (tmp_path / ".git").mkdir()
    for bad in ["../x", "/etc/passwd", ".git/config", "a/../../x", "C:/Windows/x"]:
        with pytest.raises(ToolError):
            write_file(ctx, bad, "x")
    write_file(ctx, "/workspace/pkg/a.py", "print(1)\n")
    assert (tmp_path / "pkg/a.py").read_text() == "print(1)\n"
    assert "1| print(1)" in read_file(ctx, "pkg/a.py")


def test_edit_file_requires_unique_match(tmp_path):
    ctx = ToolContext(workspace=tmp_path)
    write_file(ctx, "a.py", "x = 1\nx = 1\n")
    with pytest.raises(ToolError, match="occurs 2 times"):
        edit_file(ctx, "a.py", "x = 1", "x = 2")
    edit_file(ctx, "a.py", "x = 1\nx = 1", "x = 2")
    assert (tmp_path / "a.py").read_text() == "x = 2\n"


# ---------------------------------------------------------------- llm/cost
def test_parse_usage_sources():
    u = parse_usage({"prompt_tokens": 10, "completion_tokens": 5, "estimated_cost": 0.002}, None)
    assert u.cost_source == "provider_reported" and u.cost_usd == 0.002
    u = parse_usage({"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}, {"in": 0.5, "out": 2.0, "cached": None})
    assert u.cost_source == "estimated" and u.cost_usd == pytest.approx(2.5)
    u = parse_usage({"prompt_tokens": 1, "completion_tokens": 1}, None)
    assert u.cost_source == "unavailable" and u.cost_usd is None
    assert parse_usage(None, None).prompt_tokens is None


def test_budget():
    b = Budget(0.01)
    b.add(Usage(cost_usd=0.02))
    with pytest.raises(BudgetExceeded):
        b.check()


async def test_llm_retries_then_records(cfg, tmp_path):
    from swarmforge.db import Store
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "rate"})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hi"},
                                                      "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5,
                                                   "estimated_cost": 0.5}})
    store = Store(tmp_path / "db.sqlite")
    store.create_run("r")
    llm = LLMClient(cfg, store, "r", Budget(0.4), transport=httpx.MockTransport(handler))
    res = await llm.chat("planner", [{"role": "user", "content": "x"}])
    assert res.content == "hi" and calls["n"] == 2
    assert store.cost_summary("r")["measured"] == 0.5
    with pytest.raises(BudgetExceeded):  # 0.5 spent > 0.4 limit
        await llm.chat("planner", [{"role": "user", "content": "x"}])


# --------------------------------------------------------------- evidence
def test_normalize_and_criteria():
    assert normalize_test_id("tests/test_a.py::TestX::test_b[1]") == normalize_test_id("tests.test_a.TestX.test_b[1]")
    ev = TestEvidence("l", "c", 0, False, 1.0, junit_found=True, passed=2, failed=1,
                      cases={"tests.test_a.test_ok": "passed", "tests.test_a.test_ok2": "passed",
                             "tests.test_b.test_bad": "failed"})
    res = evaluate_criteria({"AC1": ["tests/test_a.py"], "AC2": ["tests/test_b.py::test_bad"], "AC3": []},
                            [("AC1", "a"), ("AC2", "b"), ("AC3", "c")], ev)
    assert [r["status"] for r in res] == ["satisfied", "failed", "unverified"]
    assert not ev.ok


def test_evidence_requires_executed_tests():
    ev = TestEvidence("l", "c", 0, False, 1.0, junit_found=True)
    assert not ev.ok  # exit 0 but zero tests collected is not a pass


# ------------------------------------------------------------------ github
def test_issue_url_parsing_and_branch_safety():
    assert parse_issue_url("https://github.com/o/r/issues/42") == ("o", "r", 42)
    with pytest.raises(GitHubError):
        parse_issue_url("https://github.com/o/r/pull/42")
    check_branch_safe("swarmforge/issue-1-abc", "main", "swarmforge/")
    for bad in ("main", "feature/x", "swarmforge/main"):
        with pytest.raises(GitHubError):
            check_branch_safe(bad, "main", "swarmforge/")


def test_github_fetch_issue_and_draft_pr(cfg, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0k")
    seen = {}

    def handler(req: httpx.Request):
        if req.method == "GET":
            return httpx.Response(200, json={"title": "Add export", "body": "please", "html_url": "u",
                                             "labels": [{"name": "enhancement"}]})
        seen["pr"] = json.loads(req.content)
        assert req.headers["authorization"] == "Bearer t0k"
        return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/7"})
    gh = GitHubClient(cfg.github, transport=httpx.MockTransport(handler))
    issue = gh.fetch_issue("https://github.com/o/r/issues/3")
    assert issue.title == "Add export" and issue.labels == ["enhancement"] and issue.clone_url.endswith("o/r.git")
    url = gh.create_draft_pr("o", "r", "swarmforge/x", "main", "t", "b")
    assert url.endswith("/pull/7") and seen["pr"]["draft"] is True


def test_knowledge_search_scopes_other_repositories(tmp_path):
    from swarmforge.knowledge import Vault, render_for_prompt
    v = Vault(tmp_path)
    v.write("Export CLI json flag parsing quirk", "json cli export flag parsing details " * 3, type="repo-fact", repo="other")
    v.write("Reviewer GLM flags json cli edge cases", "json cli export flag review lesson " * 3, type="review-lesson", repo="other")
    v.write("Status command json output lives in cli.py", "json status cli output " * 3, type="repo-fact", repo="mine")
    got = {n.title: n for _, n in v.search("add json output to cli status export flag", repo="mine", min_other=0)}
    assert "Status command json output lives in cli.py" in got          # same repo
    assert "Reviewer GLM flags json cli edge cases" in got              # transferable type
    assert "Export CLI json flag parsing quirk" not in got              # other repo's facts don't transfer
    assert "(repo: other;" in render_for_prompt([(1.0, got["Reviewer GLM flags json cli edge cases"])])


async def test_agent_gets_wrap_up_call_after_limit(tmp_path):
    from swarmforge.agent.runtime import AgentSpec, run_agent
    from swarmforge.agent.tools import TerminalTool, ToolContext
    from .conftest import FakeProvider, base_raw, tool_call
    import os
    os.environ["FAKE_KEY"] = "k"
    cfg = parse_config(base_raw(tmp_path))
    seen = []

    def model(body):
        names = [t["function"]["name"] for t in body["tools"]]
        seen.append(names)
        if names == ["finish"]:  # wrap-up call: only the terminal tool is offered
            return {"content": "", "tool_calls": [tool_call("finish", {"summary": "partial: tests not run"})]}
        return {"content": "", "tool_calls": [tool_call("list_files", {})]}
    fake = FakeProvider({"deepseek-fake": model})
    llm = LLMClient(cfg, transport=fake.transport)
    term = TerminalTool("finish", "done", {"type": "object", "properties": {"summary": {"type": "string"}}},
                        lambda a: (a, None))
    res = await run_agent(llm, AgentSpec(role="developer_backend", task_key="T1", system_prompt="s",
                                         user_prompt="u", tools=["list_files"], terminal=term,
                                         ctx=ToolContext(workspace=tmp_path), max_steps=3, timeout_s=60))
    assert res.ok and res.hit_limit and "3 steps" in res.hit_limit
    assert res.value["summary"].startswith("partial")
    assert len(seen) == 4 and seen[-1] == ["finish"]
    last_user = [m for m in fake.requests[-1]["messages"] if m["role"] == "user"][-1]["content"]
    assert "LIMIT REACHED" in last_user
    # a reminder was appended to a tool result before the limit
    assert any("Wrap up" in m.get("content", "") for m in fake.requests[-1]["messages"] if m["role"] == "tool")


async def test_busy_model_waits_then_falls_back(cfg, tmp_path):
    from swarmforge.db import Store
    cfg.roles["planner"].fallback_models = ["qwen-backup"]
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body["model"])
        if body["model"] == "qwen-fake":
            return httpx.Response(429, headers={"retry-after": "0"},
                                  json={"error": {"message": "Model busy, retry later", "code": "engine_overloaded"}})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "estimated_cost": 0.001}})
    store = Store(tmp_path / "db.sqlite")
    store.create_run("r")
    llm = LLMClient(cfg, store, "r", transport=httpx.MockTransport(handler), busy_wait_s=1.5)
    res = await llm.chat("planner", [{"role": "user", "content": "x"}])
    assert res.content == "ok"
    assert calls.count("qwen-fake") >= 2 and calls[-1] == "qwen-backup"   # retried, then fell back
    recorded = store.llm_calls("r")
    assert recorded[-1]["model"] == "qwen-backup" and recorded[-1]["status"] == "ok"
    assert any("falling back to qwen-backup" in e["message"] for e in store.events("r"))


async def test_all_models_busy_raises_clear_error(cfg):
    def handler(req):
        return httpx.Response(503, headers={"retry-after": "0"}, json={"error": "overloaded"})
    llm = LLMClient(cfg, transport=httpx.MockTransport(handler), busy_wait_s=0.5)
    from swarmforge.llm import LLMError
    with pytest.raises(LLMError, match="every configured model"):
        await llm.chat("planner", [{"role": "user", "content": "x"}])


def test_reviewer_fallback_cannot_overlap_developer(tmp_path):
    raw = base_raw(tmp_path)
    raw["roles"]["reviewer"]["fallback_models"] = ["kimi-fake"]
    with pytest.raises(ConfigError, match="must not approve its own work"):
        parse_config(raw)


def test_generous_runtime_default():
    from swarmforge.config import Limits
    assert Limits().max_runtime_s >= 6 * 3600 and Limits().busy_wait_s >= 600
