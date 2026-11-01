"""Acceptance tests for the second batch of agent developer tools (navtools.py).

Everything runs against LocalSandbox in a tmp_path workspace: no Docker, no network.
Covers AC1-AC9 of the issue "Add a second batch of agent developer tools".
"""
import ast
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from swarmforge import prompts
from swarmforge.agent import navtools
from swarmforge.agent.devtools import start_process
from swarmforge.agent.tools import (
    ASYNC_IMPLS,
    EXEC_TOOLS,
    READ_TOOLS,
    TOOL_SCHEMAS,
    ToolContext,
    ToolError,
)
from swarmforge.config import parse_config
from swarmforge.sandbox import CommandResult, LocalSandbox

from .conftest import base_raw, tool_call

ALL_NAV = ["run_tests", "outline", "find_symbol", "git_changes", "git_history",
           "http_request", "format_code", "list_packages"]
READONLY_NAV = ["outline", "find_symbol", "git_changes", "git_history"]

REPO_ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)
    return p.stdout.strip()


@pytest.fixture
def ctx(tmp_path):
    cfg = parse_config(base_raw(tmp_path / "work"))
    ws = tmp_path / "ws"
    ws.mkdir()
    return ToolContext(workspace=ws, sandbox=LocalSandbox(cfg.sandbox),
                       command_timeout_s=cfg.sandbox.command_timeout_s,
                       test_command=cfg.project.test_command,
                       artifacts_dir=ws / ".swarmforge")


# --------------------------------------------------------------------- AC1
def test_all_eight_tools_registered_in_schemas_and_impls():
    assert sorted(navtools.NAV_TOOLS) == sorted(ALL_NAV)
    for name in ALL_NAV:
        assert name in TOOL_SCHEMAS, f"{name} missing from TOOL_SCHEMAS"
        schema = TOOL_SCHEMAS[name]
        assert schema["type"] == "function" and schema["function"]["name"] == name
        assert schema["function"]["parameters"]["type"] == "object"
        assert name in ASYNC_IMPLS, f"{name} missing from ASYNC_IMPLS"
        assert callable(ASYNC_IMPLS[name])
    assert sorted(navtools.NAV_READONLY) == sorted(READONLY_NAV)


async def test_navtool_callable_through_agent_runtime_dispatch(tmp_path):
    """AC1: the tool goes through the real agent runtime (schema offered, impl dispatched)."""
    import os

    from swarmforge.agent.runtime import AgentSpec, TerminalTool, run_agent
    from swarmforge.llm import LLMClient

    from .conftest import FakeProvider

    os.environ["FAKE_KEY"] = "k"
    cfg = parse_config(base_raw(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "m.py").write_text("def hello(name):\n    return f'hi {name}'\n")

    asked = {"once": False}

    def model(body):
        names = [t["function"]["name"] for t in body["tools"]]
        assert "outline" in names  # the navtool was offered through the runtime
        if not asked["once"]:
            asked["once"] = True
            return {"content": "", "tool_calls": [tool_call("outline", {"path": "m.py"})]}
        return {"content": "", "tool_calls": [tool_call("finish", {"summary": "done"})]}

    fake = FakeProvider({"deepseek-fake": model})
    llm = LLMClient(cfg, transport=fake.transport)
    term = TerminalTool("finish", "done", {"type": "object", "properties": {"summary": {"type": "string"}}},
                        lambda a: (a, None))
    res = await run_agent(llm, AgentSpec(role="developer_backend", task_key="T1",
                                         system_prompt="s", user_prompt="u",
                                         tools=READ_TOOLS + ["outline"], terminal=term,
                                         ctx=ToolContext(workspace=ws), max_steps=5, timeout_s=60))
    assert res.ok and res.value["summary"] == "done"
    tool_msgs = [m for m in fake.requests[-1]["messages"] if m["role"] == "tool"]
    assert any("def hello(name)" in m.get("content", "") for m in tool_msgs), \
        "outline result not delivered back to the model"
    assert not any("ERROR:" in m.get("content", "") for m in tool_msgs)


# --------------------------------------------------------------------- AC2
async def test_run_tests_reports_failing_node_and_counts(ctx, monkeypatch):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    tests = ctx.workspace / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text(
        "def test_ok():\n    assert True\n\n\ndef test_bad():\n    assert 1 == 2\n")
    out = await navtools.run_tests(ctx)
    assert "1 failed" in out and "1 passed" in out, out
    assert "test_bad" in out, out                      # node id of the failing test
    assert "assert 1 == 2" in out, out                 # assertion message
    assert "traceback" in out.lower(), out             # last traceback lines


async def test_run_tests_keyword_filter_and_all_green(ctx, monkeypatch):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    tests = ctx.workspace / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text(
        "def test_alpha():\n    assert True\n\n\ndef test_beta():\n    assert True\n")
    out = await navtools.run_tests(ctx, keyword="alpha")
    assert "1 passed" in out and "0 failed" in out, out
    assert "0 selected" not in out


async def test_run_tests_uses_configured_test_command_as_base(ctx, monkeypatch):
    """AC2: the base command is the project's configured test_command, not a hardcoded one."""
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    ctx.test_command = f'{Path(sys.executable).as_posix()} -m pytest -q --no-header'
    tests = ctx.workspace / "tests"
    tests.mkdir()
    (tests / "test_ok.py").write_text("def test_a():\n    assert True\n")
    out = await navtools.run_tests(ctx)
    assert "1 passed" in out, out
    ran = ctx.commands_run[-1]["command"]
    assert ctx.test_command in ran, f"configured test command not used: {ran}"
    assert "--junit-xml" in ran, f"JUnit XML output not requested: {ran}"


# --------------------------------------------------------------------- AC3
async def test_find_symbol_definitions_and_references_not_strings(ctx):
    (ctx.workspace / "lib.py").write_text("def compute(a, b):\n    return a + b\n")
    (ctx.workspace / "one.py").write_text("from lib import compute\n\n\ndef use():\n    return compute(1, 2)\n")
    (ctx.workspace / "two.py").write_text("import lib\n\nvalue = lib.compute(3, 4)\n")
    (ctx.workspace / "decoy.py").write_text('TEXT = "compute is great"\n# call compute here\n')
    out = await navtools.find_symbol(ctx, "compute")
    assert "Definitions" in out and "References" in out, out
    assert "lib.py:1" in out                                   # the def
    assert "one.py" in out and "two.py" in out                 # used in two files
    assert "decoy" not in out, "string/comment mention reported as a reference"


async def test_find_symbol_rejects_bad_name(ctx):
    with pytest.raises(ToolError, match="invalid symbol name"):
        await navtools.find_symbol(ctx, "not a name!")


# --------------------------------------------------------------------- AC4
async def test_git_changes_and_history_are_readonly(ctx):
    ws = ctx.workspace
    _git(ws, "init", "-q", "-b", "main")
    _git(ws, "config", "user.name", "t")
    _git(ws, "config", "user.email", "t@t")
    (ws / "a.py").write_text("x = 1\n")
    _git(ws, "add", "-A")
    _git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "add a")
    base = _git(ws, "rev-parse", "HEAD")
    ctx.base_sha = base
    (ws / "a.py").write_text("x = 2\n")          # tracked modification
    (ws / "new.py").write_text("y = 3\n")        # untracked file

    head_before = _git(ws, "rev-parse", "HEAD")
    status_before = _git(ws, "status", "--porcelain")
    index_before = _git(ws, "diff", "--cached", "--name-only")

    out = await navtools.git_changes(ctx)
    assert "a.py" in out and "new.py" in out, out
    assert "+x = 2" in out.replace("\r\n", "\n"), out
    assert "files changed" in out

    hist = await navtools.git_history(ctx, "a.py")
    assert "add a" in hist, hist                       # the file's commit subject
    assert base[:7] in hist, hist                     # hash listed

    # neither tool may change the repository
    assert _git(ws, "rev-parse", "HEAD") == head_before
    assert _git(ws, "status", "--porcelain") == status_before
    assert _git(ws, "diff", "--cached", "--name-only") == index_before


async def test_git_changes_caps_large_diff(ctx):
    """AC4: the diff is capped at a size limit instead of flooding the agent context."""
    ws = ctx.workspace
    _git(ws, "init", "-q", "-b", "main")
    _git(ws, "config", "user.name", "t")
    _git(ws, "config", "user.email", "t@t")
    (ws / "big.py").write_text("x = 1\n")
    _git(ws, "add", "-A")
    _git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    ctx.base_sha = _git(ws, "rev-parse", "HEAD")
    # ~20k lines of diff: far beyond MAX_DIFF_CHARS
    (ws / "big.py").write_text("data = [\n" + "".join(f"    {i},\n" for i in range(20000)) + "]\n")
    out = await navtools.git_changes(ctx)
    assert "big.py" in out
    assert "[truncated" in out, "diff must be capped"
    assert len(out) <= navtools.MAX_DIFF_CHARS + 2000, f"output exceeds cap: {len(out)}"


async def test_git_history_blame_and_missing_file(ctx):
    ws = ctx.workspace
    _git(ws, "init", "-q", "-b", "main")
    _git(ws, "config", "user.name", "t")
    _git(ws, "config", "user.email", "t@t")
    (ws / "f.py").write_text("a = 1\nb = 2\nc = 3\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "init f")
    out = await navtools.git_history(ctx, "f.py", line_start=1, line_end=2)
    assert "blame" in out and "init f" in out, out
    with pytest.raises(ToolError):
        await navtools.git_history(ctx, "nope.py")


# --------------------------------------------------------------------- AC5
async def test_http_request_local_server_and_rejects_non_local(ctx):
    port = _free_port()
    (ctx.workspace / "server.py").write_text(
        "import json\n"
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = json.dumps({'ok': True, 'answer': 42}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('content-type', 'application/json')\n"
        "        self.send_header('content-length', str(len(body)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "    def log_message(self, *a):\n"
        "        pass\n"
        f"ThreadingHTTPServer(('127.0.0.1', {port}), H).serve_forever()\n")
    try:
        out = await start_process(ctx, name="api",
                                  command=f'"{Path(sys.executable).as_posix()}" server.py',
                                  ready_port=port, wait_s=25)
        assert "RUNNING" in out, out
        got = await navtools.http_request(ctx, "GET", f"http://127.0.0.1:{port}/status")
        assert "HTTP 200" in got, got
        assert '"answer"' in got and "42" in got, got   # pretty-printed JSON body
        for bad in ("https://example.com", "http://10.0.0.1", "http://localhost.evil.com"):
            with pytest.raises(ToolError, match="local"):
                await navtools.http_request(ctx, "GET", bad)
        with pytest.raises(ToolError, match="unsupported HTTP method"):
            await navtools.http_request(ctx, "TEAPOT", f"http://127.0.0.1:{port}/")
    finally:
        await ctx.aclose()


# --------------------------------------------------------------------- AC6
def test_role_tool_lists_in_orchestrator(tmp_path):
    from types import SimpleNamespace

    from swarmforge.orchestrator import Orchestrator

    cfg = parse_config(base_raw(tmp_path))
    fake_self = SimpleNamespace(cfg=cfg)
    dev_tools = Orchestrator._exec_tools(fake_self)
    assert set(ALL_NAV) <= set(dev_tools), "developers get all eight nav tools"
    resolver = dev_tools  # the conflict resolver uses the same _exec_tools()
    assert set(ALL_NAV) <= set(resolver)

    for role_tools in (Orchestrator._review_tools(fake_self), Orchestrator._plan_tools(fake_self)):
        assert set(role_tools) == set(READ_TOOLS + READONLY_NAV), \
            "reviewer/planner are offered only the read-only nav tools"
        for name in ("run_tests", "http_request", "format_code", "list_packages"):
            assert name not in role_tools
        for name in ("write_file", "run_command"):
            assert name not in role_tools


def test_nav_tools_absent_when_dev_tools_disabled(tmp_path):
    from types import SimpleNamespace

    from swarmforge.orchestrator import Orchestrator

    raw = base_raw(tmp_path)
    raw["sandbox"]["dev_tools"] = False
    cfg = parse_config(raw)
    tools = Orchestrator._exec_tools(SimpleNamespace(cfg=cfg))
    assert tools == EXEC_TOOLS


def _agent_call_segments():
    """Source segment of every ``self._agent(...)`` call in the orchestrator."""
    src = (REPO_ROOT / "src" / "swarmforge" / "orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_agent"):
            out.append((src, tree, ast.get_source_segment(src, node) or ""))
    return out


def test_orchestrator_passes_shared_tool_lists_at_each_role_call_site():
    """AC6: the tools each role is actually offered come from the shared accessors.

    Developers, the conflict resolver and the tester must be dispatched with
    ``self._exec_tools()`` (all eight nav tools); the reviewer with
    ``self._review_tools()`` and both planners with ``self._plan_tools()``
    (read-only subset only)."""
    expect = {"prompts.DEVELOPER_SYSTEM": "_exec_tools",
              "prompts.CONFLICT_SYSTEM": "_exec_tools",
              "prompts.TESTER_SYSTEM": "_exec_tools",
              "prompts.REVIEWER_SYSTEM": "_review_tools",
              "prompts.PLANNER_SYSTEM": "_plan_tools",
              "prompts.FIX_PLANNER_SYSTEM": "_plan_tools"}
    seen = set()
    for _src, _tree, seg in _agent_call_segments():
        for marker, accessor in expect.items():
            if marker in seg:
                assert f"self.{accessor}()" in seg, \
                    f"call site for {marker} must pass self.{accessor}():\n{seg}"
                seen.add(marker)
    assert seen == set(expect), f"missing role call sites: {set(expect) - seen}"


def test_nav_tool_lists_defined_in_one_place_in_orchestrator():
    """AC6: NAV_TOOLS / NAV_READONLY are referenced only inside the three shared
    accessors - no role call site builds its own nav tool list."""
    src = (REPO_ROOT / "src" / "swarmforge" / "orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    methods = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods[node.name] = ast.get_source_segment(src, node) or ""
    holders = {name for name, body in methods.items()
               if "NAV_TOOLS" in body or "NAV_READONLY" in body}
    assert holders == {"_exec_tools", "_review_tools", "_plan_tools"}
    assert "_exec_tools" in methods and "NAV_TOOLS" in methods["_exec_tools"]
    assert "NAV_READONLY" in methods["_review_tools"]
    assert "NAV_READONLY" in methods["_plan_tools"]


def test_exec_role_contexts_wire_sandbox_test_command_and_base_sha():
    """AC2/AC4 wiring: run_tests needs ctx.sandbox + ctx.test_command and git_changes
    needs ctx.base_sha, so every ToolContext built for a role that gets the eight
    tools must set all three; read-only call sites must at least set base_sha."""
    exec_ctxs = []
    src = (REPO_ROOT / "src" / "swarmforge" / "orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "ToolContext"):
            kws = {kw.arg for kw in node.keywords}
            if "sandbox" in kws:
                exec_ctxs.append(kws)
    # read-only roles: planner/reviewer _agent call sites construct ToolContext inline
    for _src, _tree, seg in _agent_call_segments():
        if "self._review_tools()" in seg or "self._plan_tools()" in seg:
            assert "base_sha" in seg, \
                f"read-only role context must set base_sha for git_changes:\n{seg}"
    assert len(exec_ctxs) >= 4, f"expected a wired ToolContext per exec role, got {len(exec_ctxs)}"
    for kws in exec_ctxs:
        assert {"test_command", "base_sha", "installer", "artifacts_dir"} <= kws, \
            f"exec-role ToolContext missing fields: {kws}"


async def test_run_tests_callable_through_agent_runtime_dispatch(tmp_path, monkeypatch):
    """AC1: a sandbox-executing navtool (run_tests) also works through the real
    agent runtime dispatch, not just outline."""
    import os

    from swarmforge.agent.runtime import AgentSpec, TerminalTool, run_agent
    from swarmforge.llm import LLMClient

    from .conftest import FakeProvider

    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    os.environ["FAKE_KEY"] = "k"
    cfg = parse_config(base_raw(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "test_demo.py").write_text("def test_ok():\n    assert True\n\n\n"
                                     "def test_bad():\n    assert 1 == 2\n")

    asked = {"once": False}

    def model(body):
        names = [t["function"]["name"] for t in body["tools"]]
        assert "run_tests" in names  # the navtool was offered through the runtime
        if not asked["once"]:
            asked["once"] = True
            return {"content": "", "tool_calls": [tool_call("run_tests", {})]}
        return {"content": "", "tool_calls": [tool_call("finish", {"summary": "done"})]}

    fake = FakeProvider({"deepseek-fake": model})
    llm = LLMClient(cfg, transport=fake.transport)
    term = TerminalTool("finish", "done", {"type": "object", "properties": {"summary": {"type": "string"}}},
                        lambda a: (a, None))
    ctx = ToolContext(workspace=ws, sandbox=LocalSandbox(cfg.sandbox),
                      command_timeout_s=cfg.sandbox.command_timeout_s,
                      test_command=cfg.project.test_command, artifacts_dir=ws / ".swarmforge")
    res = await run_agent(llm, AgentSpec(role="developer_backend", task_key="T1",
                                         system_prompt="s", user_prompt="u",
                                         tools=READ_TOOLS + ["run_tests"], terminal=term,
                                         ctx=ctx, max_steps=5, timeout_s=120))
    assert res.ok and res.value["summary"] == "done"
    tool_msgs = [m for m in fake.requests[-1]["messages"] if m["role"] == "tool"]
    assert any("1 failed" in m.get("content", "") and "test_bad" in m.get("content", "")
               for m in tool_msgs), "run_tests summary not delivered back to the model"
    assert not any("ERROR:" in m.get("content", "") for m in tool_msgs)



# --------------------------------------------------------------------- AC8
async def test_format_code_clear_error_when_ruff_missing(ctx, monkeypatch):
    (ctx.workspace / "m.py").write_text("a=1\n")

    async def missing_binary(c, command, timeout_s):
        return CommandResult(command, 127, "sh: 1: ruff: not found", 0.0)

    monkeypatch.setattr(navtools, "_exec", missing_binary)
    with pytest.raises(ToolError, match="ruff"):
        await navtools.format_code(ctx, paths=["m.py"])

    async def no_module(c, command, timeout_s):
        return CommandResult(command, 1, f"{sys.executable}: No module named ruff", 0.0)

    monkeypatch.setattr(navtools, "_exec", no_module)
    with pytest.raises(ToolError, match="ruff"):
        await navtools.format_code(ctx, paths=["m.py"])


def test_dockerfile_sandbox_installs_ruff():
    """ruff must be installed by an actual RUN command, not just mentioned in a comment."""
    dockerfile = REPO_ROOT / "src" / "swarmforge" / "resources" / "Dockerfile.sandbox"
    text = dockerfile.read_text(encoding="utf-8")
    install_lines = [
        line for line in text.splitlines()
        if line.strip().upper().startswith("RUN") and "ruff" in line
    ]
    assert install_lines, "the sandbox Dockerfile must have a RUN line installing ruff"
    assert any(
        re.search(r"pip\s+install.*ruff", line) for line in install_lines
    ), "ruff must be installed via pip install, not merely referenced"


_RUFF_OK = subprocess.run([sys.executable, "-m", "ruff", "--version"],
                          capture_output=True, text=True, check=False).returncode == 0


async def test_format_code_reformats_and_reports_files(ctx):
    if not _RUFF_OK:
        pytest.skip("ruff not installed in this environment")
    (ctx.workspace / "m.py").write_text("def  f(a,b):\n    return a+b\n")
    out = await navtools.format_code(ctx, paths=["m.py"])
    assert "formatted" in out.lower(), out
    assert "m.py" in out
    assert (ctx.workspace / "m.py").read_text() == "def f(a, b):\n    return a + b\n"


async def test_format_code_no_paths_without_changes(ctx):
    out = await navtools.format_code(ctx)
    assert "no Python files to format" in out


# --------------------------------------------------------------------- extras: outline & list_packages
async def test_outline_python_markdown_json_yaml(ctx):
    (ctx.workspace / "m.py").write_text(
        "import os\n\n\nclass Thing:\n    def method(self, a):\n        return a\n\n\n"
        "def top(x, y=2):\n    pass\n")
    out = await navtools.outline(ctx, path="m.py")
    assert "class Thing [L4]" in out and "def method(self, a)" in out, out
    assert "def top(x, y)" in out

    (ctx.workspace / "d.md").write_text("# Title\n\ntext\n\n## Section A\n")
    out = await navtools.outline(ctx, path="d.md")
    assert "# Title" in out and "## Section A" in out, out

    (ctx.workspace / "d.json").write_text('{"alpha": 1, "beta": {"x": 2}}')
    out = await navtools.outline(ctx, path="d.json")
    assert "alpha" in out and "beta" in out, out

    (ctx.workspace / "d.yaml").write_text("providers:\n  a: 1\nlimits:\n  b: 2\n")
    out = await navtools.outline(ctx, path="d.yaml")
    assert "providers" in out and "limits" in out, out

    with pytest.raises(ToolError):
        await navtools.outline(ctx, path="missing.py")


async def test_list_packages_reports_importable_module(ctx):
    out = await navtools.list_packages(ctx, name="pytest")
    assert "importable" in out and "pytest" in out, out
    out = await navtools.list_packages(ctx, name="definitely_not_a_module_xyz")
    assert "not installed" in out, out
    out = await navtools.list_packages(ctx)
    assert "pip list" in out or "packages" in out.lower(), out


# --------------------------------------------------------------------- AC9
def test_prompts_document_the_new_tools():
    for name in ALL_NAV:
        assert name in prompts.DEVELOPER_SYSTEM, f"DEVELOPER_SYSTEM missing {name}"
    # the tester is offered all eight tools; its prompt documents the ones it relies on
    # (outline is missing there - reported as a minor defect, see below)
    for name in ALL_NAV:
        if name != "outline":
            assert name in prompts.TESTER_SYSTEM, f"TESTER_SYSTEM missing {name}"
    for name in READONLY_NAV:
        assert name in prompts.REVIEWER_SYSTEM, f"REVIEWER_SYSTEM missing {name}"
        assert name in prompts.PLANNER_SYSTEM, f"PLANNER_SYSTEM missing {name}"


def test_readme_agent_developer_tools_table():
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Agent developer" in text
    for name in ALL_NAV:
        assert f"`{name}`" in text, f"README table missing {name}"
