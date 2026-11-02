"""Pi (pi.dev) harness: the model relay, Pi's event stream, routing, and a real Pi run when installed."""
import json
import shutil

import httpx
import pytest

from swarmforge import prompts
from swarmforge.agent import pi_runtime as PR
from swarmforge.agent.devtools import DockerSession
from swarmforge.agent.runtime import AgentResult
from swarmforge.agent.tools import TerminalTool, ToolContext
from swarmforge.config import ConfigError, SandboxConfig, parse_config
from swarmforge.db import Store
from swarmforge.llm import Budget, LLMClient
from swarmforge.orchestrator import Orchestrator
from swarmforge.sandbox import DockerSandbox, LocalSandbox

from .conftest import FakeProvider, base_raw


def _llm(cfg, handler, store=None, run_id=None, budget=5.0):
    fake = FakeProvider({m: handler for m in ("qwen-fake", "deepseek-fake", "kimi-fake", "glm-fake", "minimax-fake")})
    return LLMClient(cfg, store, run_id, Budget(budget), transport=fake.transport), fake


async def _post(relay, token, body):
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{relay.port}") as c:
        return await c.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {token}"})


async def test_relay_meters_calls_enforces_steps_and_auth(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    st = Store(tmp_path / "db")
    st.create_run("r1")
    llm, fake = _llm(cfg, lambda b: {"content": "hello"}, st, "r1")
    relay = PR.ModelRelay(llm)
    await relay.start()
    try:
        assert (await _post(relay, "bad", {"messages": []})).status_code == 401
        tok = relay.register("developer_backend", "T1", max_steps=2, transcript=tmp_path / "t.jsonl")
        msgs = [{"role": "user", "content": "hi"}]
        r = await _post(relay, tok, {"model": "swarmforge-developer_backend", "messages": msgs})
        assert r.json()["choices"][0]["message"]["content"] == "hello"
        assert fake.requests[-1]["model"] == "deepseek-fake"          # the role's real model, not Pi's id
        await _post(relay, tok, {"messages": msgs})
        r = await _post(relay, tok, {"messages": msgs})                # third call: over the step limit
        assert "Step limit of 2 reached" in r.json()["choices"][0]["message"]["content"]
        assert len(fake.requests) == 2 and relay.agents[tok].stopped == "max_steps"
        calls = st.llm_calls("r1")
        assert len(calls) == 2 and calls[0]["role"] == "developer_backend" and calls[0]["task_key"] == "T1"
        types = [json.loads(x)["type"] for x in (tmp_path / "t.jsonl").read_text().splitlines()]
        assert types == ["assistant", "assistant"]
    finally:
        await relay.stop()


async def test_relay_streams_tool_calls_as_sse_and_stops_on_budget(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    tc = [{"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{\"command\":\"ls\"}"}}]
    llm, _ = _llm(cfg, lambda b: {"content": None, "tool_calls": tc}, budget=5.0)
    relay = PR.ModelRelay(llm)
    await relay.start()
    try:
        tok = relay.register("developer_backend", "T1", max_steps=10)
        r = await _post(relay, tok, {"messages": [{"role": "user", "content": "x"}], "stream": True})
        assert r.headers["content-type"].startswith("text/event-stream")
        data = [json.loads(x[6:]) for x in r.text.split("\n\n") if x.startswith("data: {")]
        assert data[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "bash"
        assert data[1]["choices"][0]["finish_reason"] == "tool_calls" and "usage" in data[2]
        assert r.text.rstrip().endswith("data: [DONE]")
        llm.budget.spent = llm.budget.limit
        r = await _post(relay, tok, {"messages": [{"role": "user", "content": "x"}]})
        assert relay.agents[tok].stopped == "budget" and "budget" in r.json()["choices"][0]["message"]["content"]
    finally:
        await relay.stop()


def test_parse_events_and_argv():
    lines = [{"type": "session"}, {"type": "turn_start"},
             {"type": "tool_execution_start", "toolName": "write", "args": {"path": "a.py", "content": "x"}},
             {"type": "tool_execution_end", "toolName": "bash", "isError": True},
             {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
                                                  "content": [{"type": "thinking", "thinking": "hm"},
                                                              {"type": "text", "text": "Done: added a.py"}]}},
             {"type": "agent_end"}]
    ev = PR.parse_events("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    assert ev["final"] == "Done: added a.py" and ev["turns"] == 1 and ev["ended"]
    assert ev["tools"][0]["name"] == "write" and ev["errors"] == ["bash failed"]
    argv = PR.pi_argv("pi", "swarmforge-dev", "medium", "SYS", "do it")
    for flag in ("--no-extensions", "--no-approve", "--no-mcp", "--no-session", "--offline"):
        assert flag in argv
    assert argv[-2:] == ["--", "do it"] and argv[argv.index("--thinking") + 1] == "medium"
    mj = PR.models_json("http://h:1/v1", "swarmforge-dev", 4096)["providers"]["swarmforge"]
    assert mj["apiKey"] == "$SF_RELAY_TOKEN" and mj["models"][0]["cost"]["input"] == 0


def test_pi_session_container_can_reach_the_relay(tmp_path):
    s = DockerSession(DockerSandbox(SandboxConfig()), tmp_path, None, network="bridge")
    args = s.run_args()
    assert args[args.index("--network") + 1] == "bridge" and "host.docker.internal:host-gateway" in args
    plain = DockerSession(DockerSandbox(SandboxConfig()), tmp_path, None).run_args()
    assert plain[plain.index("--network") + 1] == "none" and "--add-host" not in plain


def test_runtime_config_validation(tmp_path):
    raw = base_raw(tmp_path)
    raw["roles"]["developer_backend"]["runtime"] = "opencode"
    with pytest.raises(ConfigError, match="runtime must be"):
        parse_config(raw)


async def test_only_finish_roles_use_pi_with_pi_prompt(cfg, tmp_path, monkeypatch):
    cfg.roles["developer_backend"].runtime = "pi"
    cfg.roles["tester"].runtime = "pi"
    o = Orchestrator(cfg, Store(tmp_path / "db"), echo=None)
    o.run_id, o.run_dir, o.llm = "r", tmp_path, None
    o.store.create_run("r")
    seen = {}

    async def fake_pi(spec, rc):
        seen["pi"] = spec.system_prompt
        return AgentResult(status="completed", value={"summary": "ok"})

    async def fake_builtin(llm, spec):
        seen["builtin"] = spec.role
        return AgentResult(status="completed", value={})
    monkeypatch.setattr(o, "_run_pi", fake_pi)
    monkeypatch.setattr("swarmforge.orchestrator.run_agent", fake_builtin)
    ctx = ToolContext(workspace=tmp_path, sandbox=LocalSandbox(SandboxConfig(mode="local")))
    await o._agent("developer_backend", "T1", prompts.DEVELOPER_SYSTEM, "task", [], o._finish_terminal(), ctx)
    assert seen["pi"].startswith(prompts.PI_DEVELOPER_SYSTEM[:40]) and "run_command" not in seen["pi"]
    report = TerminalTool("submit_test_report", "", {}, lambda a: (a, None))
    await o._agent("tester", "tc", prompts.TESTER_SYSTEM, "t", [], report, ctx)
    assert seen["builtin"] == "tester"                                   # structured reports stay built-in


@pytest.mark.skipif(not shutil.which("pi"), reason="Pi (pi.dev) is not installed")
async def test_real_pi_writes_files_runs_commands_and_hands_back(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    calls = []

    def model(body):
        calls.append(body)
        names = {t["function"]["name"] for t in body.get("tools") or []}
        assert {"read", "write", "edit", "bash"} <= names
        if len(calls) == 1:
            return {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "write", "arguments": json.dumps({"path": "hello.py", "content": "print('hi')\n"})}}]}
        if len(calls) == 2:
            return {"content": None, "tool_calls": [{"id": "c2", "type": "function", "function": {
                "name": "bash", "arguments": json.dumps({"command": "python hello.py"})}}]}
        assert body["messages"][-1]["content"].strip() == "hi"
        return {"content": "Created hello.py and ran it (prints hi)."}
    llm, _ = _llm(cfg, model)
    relay = PR.ModelRelay(llm)
    await relay.start()
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = ToolContext(workspace=ws, sandbox=LocalSandbox(SandboxConfig(mode="local")))
    term = TerminalTool("finish", "", {}, lambda a: ({"summary": a["summary"]}, None))
    try:
        res = await PR.run_pi_agent(relay=relay, ctx=ctx, role="developer_backend", task_key="T1",
                                    system=prompts.PI_DEVELOPER_SYSTEM, prompt="Create hello.py and run it.",
                                    terminal=term, max_steps=10, timeout_s=120, max_tokens=4096, thinking="medium",
                                    command=shutil.which("pi"), transcript=tmp_path / "t.jsonl")
    finally:
        await relay.stop()
    assert res.status == "completed", res.error
    assert res.value == {"summary": "Created hello.py and ran it (prints hi)."}
    assert (ws / "hello.py").read_text() == "print('hi')\n" and res.files_written == ["hello.py"]
    assert res.commands_run[0]["command"] == "python hello.py" and res.steps == 3
    tx = [json.loads(x) for x in (tmp_path / "t.jsonl").read_text().splitlines()]
    assert [t["type"] for t in tx] == ["start", "assistant", "tool", "assistant", "tool", "assistant", "terminal", "end"]
    assert tx[2]["name"] == "write" and tx[4]["name"] == "bash"


def _pi_developer(body):
    """fake_team.developer, speaking Pi's tool names (write/edit/bash) and finishing with plain text."""
    from . import fake_team
    msgs = []
    for m in body["messages"]:
        c = m.get("content")
        if isinstance(c, list):
            c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
        msgs.append({**m, "content": c})
    resp = fake_team.developer({**body, "messages": msgs})
    fn = resp["tool_calls"][0]["function"]
    name, args = fn["name"], json.loads(fn["arguments"])
    if name == "finish":
        return {"content": args["summary"]}
    if name == "write_file":
        name = "write"
    elif name == "edit_file":
        name, args = "edit", {"path": args["path"], "edits": [{"oldText": args["old_text"], "newText": args["new_text"]}]}
    elif name == "run_command":
        name = "bash"
    return {"content": "", "tool_calls": [{"id": resp["tool_calls"][0]["id"], "type": "function",
                                           "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.mark.slow
@pytest.mark.skipif(not shutil.which("pi"), reason="Pi (pi.dev) is not installed")
async def test_full_swarm_run_with_pi_developers(cfg, sample_repo):
    from . import fake_team
    cfg.roles["developer_backend"].runtime = cfg.roles["developer_frontend"].runtime = "pi"
    fake = FakeProvider({"qwen-fake": fake_team.planner, "deepseek-fake": _pi_developer,
                         "kimi-fake": _pi_developer, "minimax-fake": fake_team.tester,
                         "glm-fake": fake_team.Reviewer()})
    store = Store(cfg.db_path)
    run_id = await Orchestrator(cfg, store, echo=None, llm_transport=fake.transport).execute(
        issue_file=str(fake_team.__file__).replace("fake_team.py", "../samples/issues/export-csv-json.md"),
        repo=str(sample_repo))
    run = store.get_run(run_id)
    assert run["state"] == "COMPLETED", run["state_reason"]
    tasks = {t["task_key"]: t for t in store.tasks(run_id)}
    assert all(tasks[k]["state"] == "INTEGRATED" for k in ("T1", "T2", "T3", "F1"))
    assert tasks["T1"]["summary"] == "T1 done"
    dev_calls = [c for c in store.llm_calls(run_id) if c["role"].startswith("developer")]
    assert dev_calls and all(c["task_key"] for c in dev_calls)          # metered per task through the relay
    assert any("Pi harness" in e["message"] for e in store.events(run_id))
    assert store.test_runs(run_id)[-1]["failed"] == 0


async def test_relay_dispatches_skill_tools(cfg, tmp_path, monkeypatch):
    """load_skill/list_skills are registered tools the Pi relay can run offline."""
    from swarmforge.agent import tools as agent_tools

    monkeypatch.setenv("FAKE_KEY", "k")
    for name in ("load_skill", "list_skills"):
        assert name in PR.SF_TOOLS
        assert name in agent_tools.TOOL_SCHEMAS
        assert name in agent_tools.SYNC_IMPLS
    llm, _ = _llm(cfg, lambda b: {"content": "x"})
    relay = PR.ModelRelay(llm)
    await relay.start()
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = ToolContext(workspace=ws, sandbox=LocalSandbox(SandboxConfig(mode="local")))
    tok = relay.register("developer_backend", "T1", max_steps=5, ctx=ctx)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{relay.port}") as c:
            headers = {"Authorization": f"Bearer {tok}"}

            async def call(tool, args):
                r = await c.post("/sf/tool", json={"tool": tool, "args": args}, headers=headers)
                return r.json()

            res = await call("load_skill", {"name": "apptest-button-key"})
            assert res["ok"] and "proto.key" in res["output"]
            res = await call("load_skill", {"name": "no-such-skill"})
            assert not res["ok"] and res["output"].startswith("ERROR")
            assert "apptest-button-key" in res["output"] and "cap-truncation" in res["output"]
            res = await call("list_skills", {})
            assert res["ok"] and "cap-truncation" in res["output"]
    finally:
        await relay.stop()
