"""Developer tools for coding agents: long-running processes, a headless browser and structured
lint/type checks.

All of them run where `run_command` runs (the Docker sandbox, or the host in local mode), so they
add no new privileges: no network, no credentials, only the task's worktree.

Processes and the browser share one *session* per agent: in Docker that is a long-lived container
(same isolation flags as `run_command`) that commands are `docker exec`-ed into, so a server started
with `start_process` is reachable from `browse` at http://localhost:<port>. The session is closed,
and every process in it killed, when the agent finishes (`ToolContext.aclose`).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from importlib import resources
from pathlib import Path
from typing import Awaitable, Callable

from ..sandbox import (DEPS_DIR, CommandResult, DockerSandbox, LocalSandbox, _kill_tree, _run_quiet,
                       _scrubbed_env, _truncate)
from .tools import ToolContext, ToolError, _fn

MAX_PROCESSES = 4
SESSION_LIFETIME_S = 4 * 3600          # session containers die on their own after this
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
LOCAL_URL_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(/.*)?$", re.I)
BROWSE_MARK = "__SF_BROWSE__"
NO_BROWSER = ("the browser is not available in this sandbox (Playwright/Chromium missing). Rebuild the "
              "image with `swarmforge sandbox build` (and `--dev` for SwarmForge's own runs).")


# ================================================================= sessions
class Session:
    async def exec(self, command: str, *, timeout_s: int = 60, stdin: str | None = None,
                   env: dict[str, str] | None = None) -> CommandResult:
        raise NotImplementedError

    async def start(self, name: str, command: str) -> None:
        raise NotImplementedError

    async def status(self, name: str, tail_chars: int) -> tuple[bool, str]:
        """(still running, last output)"""
        raise NotImplementedError

    async def stop(self, name: str) -> None:
        raise NotImplementedError

    async def port_open(self, port: int) -> bool:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    python = "python"
    artifacts_path: str | None = None


class DockerSession(Session):
    def __init__(self, sandbox: DockerSandbox, workspace: Path, artifacts_dir: Path | None,
                 network: str | None = None):
        self.sb, self.workspace, self.artifacts_dir = sandbox, workspace, artifacts_dir
        self.network = network
        self.name = f"swarmforge-session-{uuid.uuid4().hex[:12]}"
        self.started = False
        self.artifacts_path = "/artifacts" if artifacts_dir is not None else None

    def run_args(self) -> list[str]:
        args = self.sb.build_args(self.workspace, f"sleep {SESSION_LIFETIME_S}", self.name, self.artifacts_dir,
                                  network=self.network)
        extra = ["--add-host", "host.docker.internal:host-gateway"] if self.network not in (None, "none") else []
        for k, v in (getattr(self.sb, "agent_env", None) or {}).items():   # agent-only env (e.g. PYTEST_ADDOPTS)
            extra += ["-e", f"{k}={v}"]
        return [args[0], "run", "-d", *extra, *args[2:]]

    async def _ensure(self) -> None:
        if self.started:
            return
        p = await asyncio.create_subprocess_exec(*self.run_args(), stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT, env=_scrubbed_env())
        out, _ = await asyncio.wait_for(p.communicate(), timeout=120)
        if p.returncode != 0:
            raise ToolError(f"could not start the sandbox session: {out.decode(errors='replace')[-500:]}")
        self.started = True

    async def exec(self, command, *, timeout_s=60, stdin=None, env=None):
        await self._ensure()
        args = [self.sb.cfg.docker_binary, "exec", "-w", "/workspace"]
        if stdin is not None:
            args.append("-i")
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        args += [self.name, "sh", "-c", command]
        start = time.time()
        p = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_scrubbed_env())
        try:
            out, _ = await asyncio.wait_for(p.communicate(stdin.encode() if stdin is not None else None),
                                            timeout=timeout_s)
            return CommandResult(command, p.returncode, _truncate(out.decode(errors="replace")),
                                 time.time() - start)
        except asyncio.TimeoutError:
            p.kill()
            return CommandResult(command, 124, "", time.time() - start, timed_out=True)

    async def exec_argv(self, argv: list[str], *, timeout_s: int = 60,
                        env: dict[str, str] | None = None) -> CommandResult:
        """Run a program directly (no shell quoting) inside the session container."""
        await self._ensure()
        args = [self.sb.cfg.docker_binary, "exec", "-w", "/workspace"]
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        start = time.time()
        p = await asyncio.create_subprocess_exec(*args, self.name, *argv, stdin=asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                 env=_scrubbed_env())
        try:
            out, _ = await asyncio.wait_for(p.communicate(), timeout=timeout_s)
            return CommandResult(" ".join(argv[:3]), p.returncode, _truncate(out.decode(errors="replace")),
                                 time.time() - start)
        except asyncio.TimeoutError:
            p.kill()
            return CommandResult(" ".join(argv[:3]), 124, "", time.time() - start, timed_out=True)

    async def start(self, name, command):
        # the inner shell records its own pid; under setsid it leads a new process group, so
        # stop() can kill the command together with every child it spawned
        script = ('mkdir -p /tmp/sf-proc && rm -f /tmp/sf-proc/$SF_NAME.pid && S=""; '
                  'command -v setsid >/dev/null 2>&1 && S=setsid; '
                  '$S nohup sh -c \'echo $$ > /tmp/sf-proc/$SF_NAME.pid; eval "$SF_CMD"; '
                  'echo "[process exited with code $?]"\' '
                  '> /tmp/sf-proc/$SF_NAME.log 2>&1 < /dev/null &')
        r = await self.exec(script, env={"SF_CMD": command, "SF_NAME": name}, timeout_s=30)
        if r.exit_code != 0:
            raise ToolError(f"could not start process: {r.output[-500:]}")

    async def status(self, name, tail_chars):
        r = await self.exec(f'tail -c {int(tail_chars)} /tmp/sf-proc/{name}.log 2>/dev/null; echo; '
                            f'kill -0 $(cat /tmp/sf-proc/{name}.pid) 2>/dev/null && echo __SF_RUNNING__ '
                            f'|| echo __SF_EXITED__', timeout_s=30)
        out = r.output.rstrip()
        running = out.endswith("__SF_RUNNING__")
        return running, out.rsplit("\n", 1)[0] if "\n" in out else ""

    async def stop(self, name):
        await self.exec(f'P=$(cat /tmp/sf-proc/{name}.pid 2>/dev/null) && '
                        f'{{ kill -TERM -$P 2>/dev/null || kill -TERM $P 2>/dev/null; sleep 1; '
                        f'kill -KILL -$P 2>/dev/null || kill -KILL $P 2>/dev/null; }}; true', timeout_s=30)

    async def port_open(self, port):
        r = await self.exec(f'{self.python} -c "import socket,sys; s=socket.socket(); s.settimeout(1); '
                            f'sys.exit(s.connect_ex((\'127.0.0.1\', {int(port)})))"', timeout_s=15)
        return r.exit_code == 0

    async def close(self):
        if self.started:
            await _run_quiet([self.sb.cfg.docker_binary, "kill", self.name])
            self.started = False


class LocalSession(Session):
    """Host processes (local sandbox mode). Same caveat as LocalSandbox: NOT isolated."""

    def __init__(self, sandbox: LocalSandbox, workspace: Path, artifacts_dir: Path | None):
        self.sb, self.workspace = sandbox, workspace
        self.procs: dict[str, tuple[subprocess.Popen, Path]] = {}
        self.logdir = workspace / ".swarmforge" / "proc"
        self.python = f'"{sys.executable}"'
        if artifacts_dir is not None:
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            self.artifacts_path = str(artifacts_dir.resolve())

    def _env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = _scrubbed_env()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        deps = str((self.workspace / DEPS_DIR).resolve())
        env["PYTHONPATH"] = deps + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env.update(getattr(self.sb, "agent_env", None) or {})
        env.update(extra or {})
        return env

    async def exec(self, command, *, timeout_s=60, stdin=None, env=None):
        start = time.time()
        p = await asyncio.create_subprocess_shell(
            command, cwd=str(self.workspace), env=self._env(env),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(p.communicate(stdin.encode() if stdin is not None else None),
                                            timeout=timeout_s)
            return CommandResult(command, p.returncode, _truncate(out.decode(errors="replace")),
                                 time.time() - start)
        except asyncio.TimeoutError:
            _kill_tree(p)
            return CommandResult(command, 124, "", time.time() - start, timed_out=True)

    async def start(self, name, command):
        self.logdir.mkdir(parents=True, exist_ok=True)
        log = self.logdir / f"{name}.log"
        fh = log.open("wb")
        kw: dict = {}
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        p = subprocess.Popen(command, shell=True, cwd=str(self.workspace), env=self._env(),
                             stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, **kw)
        fh.close()
        self.procs[name] = (p, log)

    async def status(self, name, tail_chars):
        p, log = self.procs[name]
        data = log.read_bytes()[-int(tail_chars):].decode(errors="replace") if log.exists() else ""
        code = p.poll()
        if code is not None:
            data += f"\n[process exited with code {code}]"
        return code is None, data

    async def stop(self, name):
        p, _ = self.procs[name]
        if p.poll() is None:
            _kill_tree(p)
            try:
                await asyncio.to_thread(p.wait, 10)
            except subprocess.TimeoutExpired:
                pass

    async def port_open(self, port):
        with socket.socket() as s:
            s.settimeout(1)
            return s.connect_ex(("127.0.0.1", int(port))) == 0

    async def close(self):
        for name in list(self.procs):
            await self.stop(name)


def open_session(ctx: ToolContext) -> Session:
    if ctx.session is None:
        if ctx.sandbox is None:
            raise ToolError("process and browser tools are not available to this role")
        if isinstance(ctx.sandbox, DockerSandbox):
            ctx.session = DockerSession(ctx.sandbox, ctx.workspace, ctx.artifacts_dir, network=ctx.session_network)
        elif isinstance(ctx.sandbox, LocalSandbox):
            ctx.session = LocalSession(ctx.sandbox, ctx.workspace, ctx.artifacts_dir)
        else:
            raise ToolError("this sandbox does not support long-running processes")
    return ctx.session


async def session_run(ctx: ToolContext, command: str, timeout_s: int) -> CommandResult:
    """run_command inside the agent's session container (same isolation, no per-command startup).
    `timeout` inside the container enforces the limit, so a runaway command can't outlive it."""
    s = open_session(ctx)
    r = await s.exec('timeout -k 5 "$SF_T" sh -c "$SF_CMD"', env={"SF_CMD": command, "SF_T": str(int(timeout_s))},
                     timeout_s=int(timeout_s) + 30)
    r.command = command
    if r.exit_code == 124:
        r.timed_out = True
    return r


# ================================================================= process tools
def _check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise ToolError("name must be 1-32 letters, digits, '-' or '_' (e.g. 'web' or 'api')")
    return name


async def start_process(ctx: ToolContext, name: str, command: str, ready_port: int | None = None,
                        ready_text: str | None = None, wait_s: int = 30) -> str:
    _check_name(name)
    s = open_session(ctx)
    if name in ctx.processes:
        running, _ = await s.status(name, 200)
        if running:
            raise ToolError(f"a process named '{name}' is already running; stop_process it first")
    elif len(ctx.processes) >= MAX_PROCESSES:
        raise ToolError(f"at most {MAX_PROCESSES} processes; stop one first ({', '.join(ctx.processes)})")
    await s.start(name, command)
    ctx.processes[name] = command
    ctx.commands_run.append({"command": f"[start {name}] {command}", "exit_code": None,
                             "timed_out": False, "duration_s": 0})
    wait_s = max(1, min(int(wait_s or 30), 120))
    deadline = time.time() + (wait_s if (ready_port or ready_text) else min(wait_s, 3))
    ready, why = False, ""
    while True:
        await asyncio.sleep(1)
        running, out = await s.status(name, 3000)
        if ready_text and ready_text in out:
            ready, why = True, f"output contains {ready_text!r}"
        if ready_port and not ready and await s.port_open(ready_port):
            ready, why = True, f"port {ready_port} is accepting connections"
        if ready or not running or time.time() >= deadline:
            break
    if not running:
        state = "EXITED (it is no longer running - see the output)"
    elif ready:
        state = f"RUNNING and ready ({why})"
    elif ready_port or ready_text:
        state = f"RUNNING but NOT ready after {wait_s}s (check the output; call read_process_output later)"
    else:
        state = "RUNNING"
    return f"process '{name}': {state}\n$ {command}\n--- output (last 3000 chars) ---\n{out.strip() or '(none yet)'}"


async def read_process_output(ctx: ToolContext, name: str, tail_chars: int = 4000) -> str:
    if name not in ctx.processes:
        raise ToolError(f"no process named '{name}'. Started: {', '.join(ctx.processes) or 'none'}")
    running, out = await open_session(ctx).status(name, max(200, min(int(tail_chars or 4000), 20000)))
    return (f"process '{name}': {'RUNNING' if running else 'EXITED'}\n--- output ---\n"
            f"{out.strip() or '(no output)'}")


async def stop_process(ctx: ToolContext, name: str) -> str:
    if name not in ctx.processes:
        raise ToolError(f"no process named '{name}'. Started: {', '.join(ctx.processes) or 'none'}")
    s = open_session(ctx)
    await s.stop(name)
    _, out = await s.status(name, 1500)
    del ctx.processes[name]
    return f"stopped '{name}'\n--- last output ---\n{out.strip() or '(no output)'}"


# ================================================================= browser
def _browse_script() -> str:
    return resources.files("swarmforge").joinpath("resources/browse.py").read_text(encoding="utf-8")


def format_browse(data: dict, max_chars: int) -> str:
    L = [f"URL: {data.get('url')}  (HTTP {data.get('status') or '?'})", f"Title: {data.get('title') or ''}"]
    if data.get("action_errors"):
        L.append("ACTION ERRORS:")
        L += [f"- {e}" for e in data["action_errors"]]
    if data.get("page_errors"):
        L.append("UNCAUGHT PAGE ERRORS:")
        L += [f"- {e}" for e in data["page_errors"]]
    if data.get("app_errors"):
        L.append("ERRORS SHOWN ON THE PAGE:")
        L += [f"- {e}" for e in data["app_errors"]]
    if data.get("console"):
        L.append("CONSOLE (errors/warnings):")
        L += [f"- {e}" for e in data["console"]]
    if data.get("failed_requests"):
        L.append("FAILED REQUESTS:")
        L += [f"- {e}" for e in data["failed_requests"]]
    if not any(data.get(k) for k in ("action_errors", "page_errors", "app_errors", "console", "failed_requests")):
        L.append("No errors detected (no page errors, no error elements, no console errors).")
    if data.get("screenshot"):
        L.append(f"Screenshot saved: {data['screenshot']}")
    if data.get("controls"):
        L.append("Interactive elements (use as selectors, e.g. `role=button[name=\"Save\"]` or `text=Save`):")
        L += [f"- {c}" for c in data["controls"]]
    text = data.get("text") or ""
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"
    L += ["--- visible page text ---", text or "(empty page)"]
    return "\n".join(L)


async def browse_raw(ctx: ToolContext, url: str, *, actions: list | None = None, screenshot_name: str | None = None,
                     color_scheme: str | None = None, viewport: dict | None = None, full_page: bool = True,
                     timeout_s: int = 60) -> dict:
    """Load a local page in the sandbox's headless Chromium and return the probe's raw result (text,
    errors, controls, and `screenshot_file`: the PNG on the host when `screenshot_name` was given)."""
    if not LOCAL_URL_RE.match(url or ""):
        raise ToolError("browse only opens local apps, e.g. http://localhost:8501 (start the app with "
                        "start_process first). The sandbox has no internet access.")
    if actions is not None and not isinstance(actions, list):
        raise ToolError("actions must be a list of objects, e.g. [{\"click\": \"text=Save\"}]")
    if screenshot_name is not None and not re.fullmatch(r"[\w.-]+/?[\w.-]*\.png", screenshot_name):
        raise ToolError(f"bad screenshot name {screenshot_name!r}")
    s = open_session(ctx)
    args = {"url": url, "actions": actions or [], "timeout_ms": max(5, min(int(timeout_s or 60), 180)) * 1000,
            "screenshot": (f"{s.artifacts_path}/{screenshot_name}" if screenshot_name and s.artifacts_path else None),
            "color_scheme": color_scheme if color_scheme in ("light", "dark") else None,
            "viewport": viewport or None, "full_page": full_page}
    r = await s.exec(f"{s.python} -", stdin=_browse_script(), env={"SF_BROWSE_ARGS": json.dumps(args)},
                     timeout_s=args["timeout_ms"] // 1000 + 60)
    ctx.commands_run.append({"command": f"[browse] {url}", "exit_code": r.exit_code,
                             "timed_out": r.timed_out, "duration_s": round(r.duration_s, 2)})
    if r.timed_out:
        raise ToolError(f"the browser timed out after {r.duration_s:.0f}s")
    line = next((ln for ln in reversed(r.output.splitlines()) if ln.startswith(BROWSE_MARK)), None)
    if line is None:
        if "No module named 'playwright'" in r.output or "Executable doesn't exist" in r.output:
            raise ToolError(NO_BROWSER)
        raise ToolError(f"the browser failed: {r.output[-1500:]}")
    data = json.loads(line[len(BROWSE_MARK):])
    if data.get("fatal"):
        msg = data["fatal"]
        if "Executable doesn't exist" in msg:
            raise ToolError(NO_BROWSER)
        raise ToolError(f"could not load {url}: {msg}")
    data["screenshot_file"] = None
    if data.get("screenshot") and ctx.artifacts_dir is not None and screenshot_name:
        f = Path(ctx.artifacts_dir) / screenshot_name
        data["screenshot_file"] = str(f) if f.is_file() else None
    return data


async def browse(ctx: ToolContext, url: str, actions: list | None = None, screenshot: bool = False,
                 max_chars: int = 6000, timeout_s: int = 60, color_scheme: str | None = None) -> str:
    ctx.browse_count += 1
    data = await browse_raw(ctx, url, actions=actions, timeout_s=timeout_s, color_scheme=color_scheme,
                            screenshot_name=f"browser/shot-{ctx.browse_count}.png" if screenshot else None)
    if screenshot and not open_session(ctx).artifacts_path:
        data["screenshot"] = None
    return format_browse(data, max(500, min(int(max_chars or 6000), 30000)))


# ================================================================= code checks
def _targets(ctx: ToolContext, paths: list | None) -> list[str]:
    if paths:
        if isinstance(paths, str):
            paths = [paths]
        out = []
        for p in paths:
            full = ctx.resolve(str(p), must_exist=True)
            rel = ctx.rel(full) or "."
            if not SAFE_PATH_RE.match(rel):
                raise ToolError(f"unsupported characters in path: {rel}")
            out.append(rel)
        return out
    return sorted(f for f in ctx.files_written
                  if f.endswith(".py") and (ctx.workspace / f).is_file() and SAFE_PATH_RE.match(f))


def _relpath(ctx: ToolContext, filename: str) -> str:
    f = filename.replace("\\", "/")
    root = str(ctx.workspace.resolve()).replace("\\", "/")
    for pre in (root + "/", "/workspace/"):
        if f.startswith(pre):
            return f[len(pre):]
    return f


def parse_ruff(ctx: ToolContext, output: str) -> list[str] | None:
    start = output.find("[")
    if start < 0:
        return None
    try:
        items = json.loads(output[start:output.rfind("]") + 1])
    except json.JSONDecodeError:
        return None
    out = []
    for d in items:
        loc = d.get("location") or {}
        fix = " (auto-fixable)" if d.get("fix") else ""
        out.append(f"{_relpath(ctx, d.get('filename', '?'))}:{loc.get('row', '?')}:{loc.get('column', '?')} "
                   f"{d.get('code') or ''} {d.get('message', '').strip()}{fix}")
    return out


def parse_mypy(ctx: ToolContext, output: str) -> list[str] | None:
    out, seen_json = [], False
    for ln in output.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        seen_json = True
        if d.get("severity") == "note":
            continue
        code = f" [{d['code']}]" if d.get("code") else ""
        hint = f" (hint: {d['hint']})" if d.get("hint") else ""
        out.append(f"{_relpath(ctx, d.get('file', '?'))}:{d.get('line', '?')}:{d.get('column', '?')} "
                   f"{d.get('message', '').strip()}{code}{hint}")
    if not seen_json and output.strip() and "Success" not in output:
        return None
    return out


def _section(title: str, items: list[str] | None, raw: str, limit: int = 60) -> list[str]:
    if items is None:
        if "No module named" in raw:
            return [f"{title}: not installed in the sandbox (rebuild with `swarmforge sandbox build`)"]
        return [f"{title}: could not run", raw.strip()[-1500:]]
    L = [f"{title}: {len(items)} issue(s)"]
    L += [f"  {i}" for i in items[:limit]]
    if len(items) > limit:
        L.append(f"  ...and {len(items) - limit} more")
    return L


async def check_code(ctx: ToolContext, paths: list | None = None, type_check: bool = True) -> str:
    if ctx.sandbox is None:
        raise ToolError("check_code is not available to this role")
    targets = _targets(ctx, paths)
    docker = isinstance(ctx.sandbox, DockerSandbox)
    py = "python" if docker else f'"{sys.executable}"'
    t = min(ctx.command_timeout_s, 300)
    lint_targets = targets or ["."]
    ruff = await ctx.sandbox.run(ctx.workspace, f"{py} -m ruff check --output-format=json --exit-zero "
                                                f"--no-cache {' '.join(lint_targets)}", timeout_s=t)
    L = [f"Checked: {', '.join(lint_targets)}"]
    L += _section("ruff (lint)", parse_ruff(ctx, ruff.output), ruff.output)
    if not type_check:
        pass
    elif not targets:
        L.append("mypy (types): skipped - no Python files changed yet; pass `paths` to type-check specific files")
    else:
        cache = "/tmp/.mypy_cache" if docker else ".swarmforge/mypy_cache"
        mypy = await ctx.sandbox.run(ctx.workspace, f"{py} -m mypy --output=json --ignore-missing-imports "
                                                    f"--follow-imports=silent --no-error-summary "
                                                    f"--cache-dir={cache} {' '.join(targets)}", timeout_s=t)
        L += _section("mypy (types)", parse_mypy(ctx, mypy.output), mypy.output)
    ctx.commands_run.append({"command": f"[check_code] {' '.join(lint_targets)}", "exit_code": 0,
                             "timed_out": False, "duration_s": 0})
    return "\n".join(L)


# ================================================================= schemas
S = {"type": "string"}
I = {"type": "integer"}

DEV_SCHEMAS = {
    "start_process": _fn(
        "start_process",
        "Start a long-running command in the background (a web server, an app, a worker) in your "
        "sandbox, e.g. `streamlit run app.py --server.headless true --server.port 8501` or "
        "`python -m http.server 8000`. Give ready_port and/or ready_text to wait until it is up. "
        "Returns its status and first output. Everything you start is stopped when you finish.",
        {"name": {"type": "string", "description": "short id, e.g. 'web'"}, "command": S,
         "ready_port": {"type": "integer", "description": "wait until this local port accepts connections"},
         "ready_text": {"type": "string", "description": "wait until the output contains this text"},
         "wait_s": {"type": "integer", "description": "max seconds to wait for readiness (default 30)"}},
        ["name", "command"]),
    "read_process_output": _fn("read_process_output",
                               "Show whether a started process is still running and its latest output.",
                               {"name": S, "tail_chars": I}, ["name"]),
    "stop_process": _fn("stop_process", "Stop a process started with start_process.", {"name": S}, ["name"]),
    "browse": _fn(
        "browse",
        "Open a LOCAL web app (started with start_process) in a headless Chromium and report what a user "
        "would see: visible text, errors shown on the page (e.g. Streamlit exceptions), uncaught JS "
        "errors, console errors, failed requests and the clickable elements. Optional actions run in "
        "order before the report, each an object like {\"click\": \"text=Save\"}, "
        "{\"fill\": \"#name\", \"value\": \"Bob\"}, {\"press\": \"Enter\"}, {\"select\": \"#color\", "
        "\"value\": \"red\"}, {\"wait_for\": \"text=Done\"}, {\"wait_ms\": 1000} or {\"goto\": url}. "
        "Selectors are Playwright selectors (text=..., role=button[name=\"...\"], CSS).",
        {"url": {"type": "string", "description": "e.g. http://localhost:8501"},
         "actions": {"type": "array", "items": {"type": "object"}},
         "screenshot": {"type": "boolean", "description": "also save a PNG screenshot to the run artifacts"},
         "color_scheme": {"type": "string", "enum": ["light", "dark"],
                          "description": "the browser's light/dark preference (pages that follow it switch theme)"},
         "max_chars": I, "timeout_s": I},
        ["url"]),
    "check_code": _fn(
        "check_code",
        "Run the linter (ruff) and type checker (mypy) and get a structured list of problems "
        "(file:line:col code message). Defaults to the Python files you changed. Run it before `finish` "
        "and fix what it reports in code you wrote.",
        {"paths": {"type": "array", "items": {"type": "string"}, "description": "files/dirs to check"},
         "type_check": {"type": "boolean", "description": "also run mypy (default true)"}},
        []),
}

DEV_IMPLS: dict[str, Callable[..., Awaitable[str]]] = {
    "start_process": start_process, "read_process_output": read_process_output,
    "stop_process": stop_process, "browse": browse, "check_code": check_code,
}
DEV_TOOLS = list(DEV_SCHEMAS)
