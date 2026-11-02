"""Developer tools: background processes, headless browser, structured lint/type checks.
Run against the local sandbox (no Docker needed); browser/linter tests skip when not installed."""
import asyncio
import importlib.util
import socket
import sys
import time
from pathlib import Path

import pytest

from swarmforge.agent import devtools
from swarmforge.agent.tools import TOOL_SCHEMAS, ToolContext, ToolError
from swarmforge.config import SandboxConfig
from swarmforge.sandbox import DockerSandbox, LocalSandbox

from .conftest import browser_lock

PY = f'"{sys.executable}"'


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def ctx(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ToolContext(workspace=ws, sandbox=LocalSandbox(SandboxConfig(mode="local")),
                       artifacts_dir=tmp_path / "artifacts")


def test_dev_tools_are_registered():
    for name in devtools.DEV_TOOLS:
        assert name in TOOL_SCHEMAS and TOOL_SCHEMAS[name]["function"]["name"] == name


async def test_process_lifecycle_and_cleanup(ctx):
    port = free_port()
    (ctx.workspace / "index.html").write_text("<h1>hello</h1>")
    out = await devtools.start_process(ctx, "web", f"{PY} -m http.server {port} --bind 127.0.0.1",
                                       ready_port=port, wait_s=30)
    assert "RUNNING and ready" in out
    out = await devtools.read_process_output(ctx, "web")
    assert "RUNNING" in out
    with pytest.raises(ToolError, match="already running"):
        await devtools.start_process(ctx, "web", "echo again")
    await ctx.aclose()                                   # what the orchestrator does after each agent
    for _ in range(30):                                  # the server goes away (allow the OS a moment)
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                break
        await asyncio.sleep(0.2)
    else:
        pytest.fail("server still listening after aclose()")
    assert ctx.processes == {} and ctx.session is None


async def test_process_that_exits_reports_output_and_code(ctx):
    out = await devtools.start_process(ctx, "job", f'{PY} -c "print(\'boom\'); raise SystemExit(3)"',
                                       ready_text="never", wait_s=10)
    assert "EXITED" in out and "boom" in out and "code 3" in out
    out = await devtools.stop_process(ctx, "job")
    assert "stopped 'job'" in out
    with pytest.raises(ToolError, match="no process named"):
        await devtools.read_process_output(ctx, "job")
    await ctx.aclose()


async def test_process_name_validation_and_limit(ctx):
    with pytest.raises(ToolError, match="name must be"):
        await devtools.start_process(ctx, "bad name;rm", "echo x")
    for i in range(devtools.MAX_PROCESSES):
        await devtools.start_process(ctx, f"p{i}", f'{PY} -c "import time; time.sleep(30)"', wait_s=1)
    with pytest.raises(ToolError, match="at most"):
        await devtools.start_process(ctx, "extra", "echo x")
    await ctx.aclose()


async def test_browse_only_allows_local_urls(ctx):
    for url in ("https://example.com", "http://10.0.0.1:80", "file:///etc/passwd", "http://localhost.evil.com"):
        with pytest.raises(ToolError, match="only opens local apps"):
            await devtools.browse(ctx, url)


def test_docker_session_uses_the_same_isolation_flags(tmp_path):
    sess = devtools.DockerSession(DockerSandbox(SandboxConfig(), run_label="r1"), tmp_path, None)
    args = sess.run_args()
    assert args[1:3] == ["run", "-d"] and "--rm" in args
    for flag in ("--read-only", "--cap-drop", "no-new-privileges", "swarmforge.run=r1"):
        assert any(flag in a for a in args), flag
    assert args[args.index("--network") + 1] == "none"
    assert args[-1].startswith("sleep ")


def test_format_browse_highlights_errors():
    text = devtools.format_browse({"url": "http://localhost:1/", "status": 200, "title": "App",
                                   "app_errors": ["KeyError: 'x'"], "console": ["error: boom"],
                                   "text": "a" * 50, "controls": ['button "Save"']}, max_chars=10)
    assert "ERRORS SHOWN ON THE PAGE" in text and "KeyError" in text and "error: boom" in text
    assert 'button "Save"' in text and "truncated" in text
    clean = devtools.format_browse({"url": "u", "text": "ok"}, 100)
    assert "No errors detected" in clean


def _has_browser() -> bool:
    if importlib.util.find_spec("playwright") is None:
        return False
    try:
        from playwright.sync_api import sync_playwright
        with browser_lock():  # don't probe while another worker is inside its own browser test
            for _ in range(2):  # a second chance: a launch can still lose a race for /dev/shm
                try:
                    with sync_playwright() as p:
                        p.chromium.launch(args=["--no-sandbox"]).close()
                    return True
                except Exception:  # noqa: BLE001 - retry once, then treat as "no browser"
                    time.sleep(0.5)
    except Exception:  # noqa: BLE001
        return False
    return False


@pytest.mark.browser
@pytest.mark.skipif(not _has_browser(), reason="playwright/chromium not installed")
async def test_browse_reports_page_text_errors_and_actions(ctx):
    port = free_port()
    (ctx.workspace / "index.html").write_text(
        "<html><head><title>Demo</title></head><body><h1>Inventory</h1>"
        "<button id='go' onclick=\"document.getElementById('out').innerText='clicked!'\">Go</button>"
        "<div id='out'></div><div role='alert'>Traceback: ValueError bad</div>"
        "<script>console.error('broken widget'); setTimeout(() => { throw new Error('kaput') }, 10)</script>"
        "</body></html>")
    await devtools.start_process(ctx, "web", f"{PY} -m http.server {port} --bind 127.0.0.1",
                                 ready_port=port)
    out = await devtools.browse(ctx, f"http://localhost:{port}/index.html",
                                actions=[{"click": "#go"}, {"wait_for": "text=clicked!"}], screenshot=True)
    await ctx.aclose()
    assert "Title: Demo" in out and "Inventory" in out and "clicked!" in out
    assert "ValueError bad" in out and "broken widget" in out and "kaput" in out
    assert 'button #go "Go"' in out
    assert "Screenshot saved" in out and any((Path(ctx.artifacts_dir) / "browser").glob("*.png"))


@pytest.mark.skipif(importlib.util.find_spec("ruff") is None or importlib.util.find_spec("mypy") is None,
                    reason="ruff/mypy not installed")
async def test_check_code_returns_structured_problems(ctx):
    (ctx.workspace / "ruff.toml").write_text('[lint]\nselect = ["E", "F"]\n')   # the project's own config is respected
    (ctx.workspace / "mod.py").write_text("import os\n\ndef f(x: int) -> str:\n    return x + 1\n")
    (ctx.workspace / "clean.py").write_text("def g() -> int:\n    return 1\n")
    ctx.files_written.update({"mod.py", "clean.py"})
    out = await devtools.check_code(ctx)
    assert "ruff (lint): 1 issue(s)" in out and "mod.py:1:8 F401" in out
    assert "mypy (types): 1 issue(s)" in out and "mod.py:4" in out and "[return-value]" in out
    out = await devtools.check_code(ctx, paths=["clean.py"])
    assert "ruff (lint): 0 issue(s)" in out and "mypy (types): 0 issue(s)" in out
    with pytest.raises(ToolError):
        await devtools.check_code(ctx, paths=["../outside.py"])


async def test_check_code_without_changes_skips_mypy(ctx):
    out = await devtools.check_code(ctx)
    assert "mypy (types): skipped" in out
