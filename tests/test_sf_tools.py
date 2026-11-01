"""`sf`: SwarmForge's developer tools for agents in the Pi harness (through the model relay)."""
import json
import shutil
import socket
import subprocess
import sys

import httpx
import pytest

from swarmforge import prompts
from swarmforge.agent import pi_runtime as PR
from swarmforge.agent.tools import TerminalTool, ToolContext
from swarmforge.config import SandboxConfig
from swarmforge.resources import sf as SF   # noqa: F401  (imported as a module for parse tests)
from swarmforge.sandbox import LocalSandbox

from .test_pi import _llm


def test_sf_parses_every_command():
    p = SF.parse
    assert p(["start", "web", "--port", "8501", "--", "streamlit", "run", "app.py", "--server.port", "8501"]) == (
        "start_process", {"name": "web", "ready_port": 8501, "command": "streamlit run app.py --server.port 8501"})
    assert p(["start", "w", "--", "echo", "hi there"])[1]["command"] == "echo 'hi there'"
    assert p(["logs", "web", "--tail", "500"]) == ("read_process_output", {"name": "web", "tail_chars": 500})
    assert p(["stop", "web"]) == ("stop_process", {"name": "web"})
    tool, a = p(["browse", "http://localhost:8501", "--click", "text=Settings", "--fill", "#name=Bob",
                 "--press", "Enter", "--wait-ms", "500", "--screenshot", "--max-chars", "900"])
    assert tool == "browse" and a["screenshot"] is True and a["max_chars"] == 900
    assert a["actions"] == [{"click": "text=Settings"}, {"fill": "#name", "value": "Bob"}, {"press": "Enter"},
                            {"wait_ms": 500}]
    assert p(["tests", "tests/test_a.py", "-k", "foo", "--lf"]) == (
        "run_tests", {"keyword": "foo", "last_failed": True, "paths": ["tests/test_a.py"]})
    assert p(["check", "--no-types"]) == ("check_code", {"type_check": False})
    assert p(["fmt"]) == ("format_code", {})
    assert p(["outline", "a.py"]) == ("outline", {"path": "a.py"})
    assert p(["symbol", "Store"]) == ("find_symbol", {"name": "Store"})
    assert p(["changes"]) == ("git_changes", {})
    assert p(["history", "a.py", "--lines", "3-9"]) == ("git_history", {"path": "a.py", "line_start": 3, "line_end": 9})
    assert p(["http", "post", "http://localhost:8000/x", "--json", '{"a": 1}', "-H", "X-Y: z"]) == (
        "http_request", {"method": "POST", "url": "http://localhost:8000/x", "json": {"a": 1}, "headers": {"X-Y": "z"}})
    assert p(["call", "outline", '{"path": "a.py"}']) == ("outline", {"path": "a.py"})
    for bad in (["start", "web"], ["browse"], ["fill"], ["browse", "u", "--fill", "noequals"], ["nope"]):
        with pytest.raises(SF.Usage):
            p(bad)
    assert SF.main(["help"]) == 0
    assert SF.main(["symbol", "x"]) == 1          # outside an agent session: a clear error, not a crash


def _ctx(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "mod.py").write_text("def greet(name):\n    return 'hi ' + name\n\n\nprint(greet('a'))\n")
    return ToolContext(workspace=ws, sandbox=LocalSandbox(SandboxConfig(mode="local")),
                       artifacts_dir=tmp_path / "artifacts")


async def test_relay_runs_tools_only_for_agents_that_were_given_them(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    llm, _ = _llm(cfg, lambda b: {"content": "x"})
    relay = PR.ModelRelay(llm)
    await relay.start()
    events = []
    ctx = _ctx(tmp_path)
    tok = relay.register("developer_backend", "T1", max_steps=5, ctx=ctx,
                         on_event=lambda k, m, d: events.append(m))
    plain = relay.register("developer_backend", "T2", max_steps=5)            # no ctx: no tools
    url = f"http://127.0.0.1:{relay.port}"
    try:
        ok, out = await _acall("find_symbol", {"name": "greet"}, url, tok)
        assert ok and "mod.py:1" in out and "mod.py:5" in out
        ok, out = await _acall("write_file", {"path": "x", "content": "y"}, url, tok)
        assert not ok and "unknown tool" in out and not (ctx.workspace / "x").exists()
        ok, out = await _acall("outline", {"path": "../../etc/passwd"}, url, tok)
        assert not ok and out.startswith("ERROR")
        ok, out = await _acall("outline", {"path": "mod.py"}, url, plain)
        assert not ok and "unknown tool" in out
        ok, out = await _acall("outline", {"path": "mod.py"}, url, "sf-forged")
        assert not ok and "401" in out
        assert [(n, ok) for n, _, ok in relay.agents[tok].sf_calls] == [("find_symbol", True), ("outline", False)]
        assert any(m.startswith("sf find_symbol") for m in events)
    finally:
        await relay.stop()


async def _acall(tool, args, url, token):
    import asyncio
    return await asyncio.to_thread(SF.call, tool, args, url, token, 120)


def test_install_sf_writes_a_runnable_command(tmp_path):
    bindir = PR.install_sf(tmp_path)
    assert (bindir / "sf").stat().st_mode & 0o111 and (bindir / "sf.py").exists()
    if shutil.which("sh"):
        r = subprocess.run(["sh", str(bindir / "sf"), "help"], capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin:" + str(__import__("pathlib").Path(sys.executable).parent)})
        assert r.returncode == 0 and "sf browse URL" in r.stdout


def test_pi_prompt_teaches_sf_and_requires_browsing_ui_changes():
    s = prompts.PI_DEVELOPER_SYSTEM
    assert "sf browse" in s and "sf start" in s and "MUST look at the result" in s and "sf help" in s
    assert "MUST also start the app" in prompts.TESTER_SYSTEM


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.slow
@pytest.mark.skipif(not shutil.which("pi"), reason="Pi (pi.dev) is not installed")
async def test_real_pi_agent_uses_sf_to_start_browse_and_stop_an_app(cfg, tmp_path, monkeypatch):
    """A real Pi process: bash -> sf -> relay -> start_process / browse (real Chromium) / stop_process."""
    pytest.importorskip("playwright")
    monkeypatch.setenv("FAKE_KEY", "k")
    port = _free_port()
    script = [f"sf symbol greet",
              f"sf start web --port {port} -- {sys.executable} -m http.server {port} --bind 127.0.0.1",
              f"sf browse http://localhost:{port}/page.html --click 'text=Go' --screenshot",
              "sf stop web"]
    outputs = []

    def model(body):
        last = body["messages"][-1]
        if last.get("role") == "tool":
            c = last.get("content")
            outputs.append(c if isinstance(c, str) else "\n".join(p.get("text", "") for p in c))
        i = len(outputs)
        if i < len(script):
            return {"content": None, "tool_calls": [{"id": f"c{i}", "type": "function", "function": {
                "name": "bash", "arguments": json.dumps({"command": script[i], "timeout": 120})}}]}
        return {"content": "Checked the page with sf browse: it shows Clicked, no errors."}
    llm, _ = _llm(cfg, model)
    relay = PR.ModelRelay(llm)
    await relay.start()
    ctx = _ctx(tmp_path)
    (ctx.workspace / "page.html").write_text(
        "<html><body><h1>Hello page</h1><button onclick=\"document.getElementById('o').textContent='Clicked'\">"
        "Go</button><p id='o'>not yet</p></body></html>")
    term = TerminalTool("finish", "", {}, lambda a: ({"summary": a["summary"]}, None))
    events = []
    try:
        res = await PR.run_pi_agent(relay=relay, ctx=ctx, role="developer_frontend", task_key="T1",
                                    system=prompts.PI_DEVELOPER_SYSTEM, prompt="Check the page.", terminal=term,
                                    max_steps=10, timeout_s=300, max_tokens=4096, thinking=None,
                                    command=shutil.which("pi"), transcript=tmp_path / "t.jsonl",
                                    on_event=lambda k, m, d: events.append(m))
    finally:
        await relay.stop()
        await ctx.aclose()
    assert res.status == "completed", (res.error, outputs)
    assert "mod.py:1" in outputs[0]
    assert "is running" in outputs[1].lower() or "ready" in outputs[1].lower(), outputs[1]
    assert "Clicked" in outputs[2] and "Hello page" in outputs[2] and "No errors detected" in outputs[2], outputs[2]
    assert list((tmp_path / "artifacts").rglob("*.png")), "screenshot was saved to the run artifacts"
    assert "stopped" in outputs[3].lower(), outputs[3]
    assert any("SwarmForge tools used: " in m and "browse x1" in m for m in events)
    assert res.tool_calls >= 8           # 4 bash calls + 4 sf tool calls


async def test_docker_pi_gets_sf_on_path_and_the_tools_url(cfg, tmp_path, monkeypatch):
    """Docker branch with a recording session: sf is installed, made executable and put on PATH."""
    from types import SimpleNamespace

    from swarmforge.agent import devtools
    from swarmforge.sandbox import DockerSandbox
    monkeypatch.setenv("FAKE_KEY", "k")
    llm, _ = _llm(cfg, lambda b: {"content": "done"})
    relay = PR.ModelRelay(llm)
    await relay.start()
    seen = {}

    class FakeDocker(devtools.DockerSession):
        def __init__(self):
            pass

        async def exec(self, command, stdin=None, timeout_s=60, env=None):
            return SimpleNamespace(exit_code=0, output="", timed_out=False)

        async def exec_argv(self, argv, timeout_s=60, env=None):
            seen["argv"], seen["env"] = argv, env
            out = tmp_path / "ws" / env["SF_OUT"].removeprefix("/workspace/")
            out.write_text(json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "done"}]}}) + "\n")
            return SimpleNamespace(exit_code=0, output="", timed_out=False)

    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = ToolContext(workspace=ws, sandbox=DockerSandbox(SandboxConfig()))
    ctx.session = FakeDocker()
    term = TerminalTool("finish", "", {}, lambda a: ({"summary": a["summary"]}, None))
    try:
        res = await PR.run_pi_agent(relay=relay, ctx=ctx, role="developer_frontend", task_key="T1", system="s",
                                    prompt="p", terminal=term, max_steps=5, timeout_s=60, max_tokens=100,
                                    thinking=None)
    finally:
        await relay.stop()
    script = seen["argv"][2]
    assert "chmod +x /workspace/.swarmforge/bin/sf" in script and 'PATH="/workspace/.swarmforge/bin:$PATH"' in script
    assert seen["env"]["SF_TOOLS_URL"] == f"http://host.docker.internal:{relay.port}"
    assert seen["env"]["SF_RELAY_TOKEN"].startswith("sf-")
    assert (ws / ".swarmforge" / "bin" / "sf.py").exists()
    assert res.status == "completed", res.error
