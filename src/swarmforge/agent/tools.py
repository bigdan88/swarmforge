"""Tools exposed to agents. Every file operation is confined to the agent's worktree;
commands run through the configured Sandbox (Docker by default)."""
from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..sandbox import DockerSandbox, Sandbox

MAX_READ_CHARS = 40_000
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv", ".mypy_cache",
             ".ruff_cache", ".swarmforge"}


class ToolError(Exception):
    pass


@dataclass
class ToolContext:
    workspace: Path
    sandbox: Sandbox | None = None
    command_timeout_s: int = 300
    artifacts_dir: Path | None = None
    files_written: set[str] = field(default_factory=set)
    commands_run: list[dict] = field(default_factory=list)
    installer: Callable[[Path], Awaitable[str]] | None = None   # dependency install step
    session: Any = None                                        # devtools.Session (processes/browser)
    processes: dict[str, str] = field(default_factory=dict)    # name -> command (start_process)
    browse_count: int = 0

    async def aclose(self) -> None:
        """Stop every process and the sandbox session this agent started."""
        if self.session is not None:
            try:
                await self.session.close()
            finally:
                self.session = None
                self.processes.clear()

    def resolve(self, rel: str, must_exist: bool = False) -> Path:
        if rel is None or rel == "":
            rel = "."
        rel = str(rel).replace("\\", "/")
        if rel.startswith("/workspace/"):
            rel = rel[len("/workspace/"):]
        elif rel == "/workspace":
            rel = "."
        p = Path(rel)
        if p.is_absolute() or (len(rel) > 1 and rel[1] == ":"):
            raise ToolError(f"absolute paths are not allowed: {rel}")
        root = self.workspace.resolve()
        full = (root / p).resolve()
        try:
            relp = full.relative_to(root)
        except ValueError:
            raise ToolError(f"path escapes the workspace: {rel}")
        if relp.parts and relp.parts[0] == ".git":
            raise ToolError("access to .git is not allowed")
        if must_exist and not full.exists():
            raise ToolError(f"no such file or directory: {rel}")
        return full

    def rel(self, full: Path) -> str:
        return full.resolve().relative_to(self.workspace.resolve()).as_posix()


# --------------------------------------------------------------------- tools
def list_files(ctx: ToolContext, path: str = ".", max_depth: int = 4) -> str:
    base = ctx.resolve(path, must_exist=True)
    if base.is_file():
        return ctx.rel(base)
    out = []
    base_depth = len(base.parts)
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        depth = len(Path(root).parts) - base_depth
        if depth >= max_depth:
            dirs[:] = []
        for f in sorted(files):
            out.append(ctx.rel(Path(root) / f))
        if len(out) > 1000:
            out.append("...[truncated]")
            break
    return "\n".join(out) or "(empty)"


def read_file(ctx: ToolContext, path: str, start_line: int | None = None,
              end_line: int | None = None) -> str:
    p = ctx.resolve(path, must_exist=True)
    if p.is_dir():
        raise ToolError(f"{path} is a directory; use list_files")
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    s = max(1, int(start_line or 1))
    e = min(len(lines), int(end_line or len(lines)))
    body = "\n".join(f"{i:5d}| {lines[i - 1]}" for i in range(s, e + 1))
    if len(body) > MAX_READ_CHARS:
        body = body[:MAX_READ_CHARS] + "\n...[truncated; use start_line/end_line]"
    return f"{ctx.rel(p)} (lines {s}-{e} of {len(lines)})\n{body}"


def search(ctx: ToolContext, pattern: str, path: str = ".", glob: str | None = None,
           max_results: int = 100) -> str:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ToolError(f"invalid regex: {e}")
    base = ctx.resolve(path, must_exist=True)
    results = []
    files = [base] if base.is_file() else None
    if files is None:
        files = []
        for root, dirs, fs in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in fs:
                if glob and not fnmatch.fnmatch(f, glob):
                    continue
                files.append(Path(root) / f)
    for fp in sorted(files):
        try:
            if fp.stat().st_size > 2_000_000:
                continue
            for i, line in enumerate(fp.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if rx.search(line):
                    results.append(f"{ctx.rel(fp)}:{i}: {line.strip()[:200]}")
                    if len(results) >= max_results:
                        return "\n".join(results) + "\n...[max results reached]"
        except OSError:
            continue
    return "\n".join(results) or "(no matches)"


def write_file(ctx: ToolContext, path: str, content: str) -> str:
    p = ctx.resolve(path)
    if p.is_dir():
        raise ToolError(f"{path} is a directory")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="\n")
    ctx.files_written.add(ctx.rel(p))
    return f"wrote {ctx.rel(p)} ({len(content.splitlines())} lines)"


def edit_file(ctx: ToolContext, path: str, old_text: str, new_text: str) -> str:
    p = ctx.resolve(path, must_exist=True)
    text = p.read_text(encoding="utf-8")
    n = text.count(old_text)
    if not old_text:
        raise ToolError("old_text must not be empty; use write_file to create files")
    if n == 0:
        raise ToolError("old_text not found in file (it must match exactly, including whitespace)")
    if n > 1:
        raise ToolError(f"old_text occurs {n} times; include more surrounding context to make it unique")
    p.write_text(text.replace(old_text, new_text, 1), encoding="utf-8", newline="\n")
    ctx.files_written.add(ctx.rel(p))
    return f"edited {ctx.rel(p)}"


def delete_file(ctx: ToolContext, path: str) -> str:
    p = ctx.resolve(path, must_exist=True)
    if p.is_dir():
        raise ToolError("deleting directories is not supported")
    p.unlink()
    ctx.files_written.add(ctx.rel(p))
    return f"deleted {ctx.rel(p)}"


async def run_command(ctx: ToolContext, command: str, timeout_s: int | None = None) -> str:
    if ctx.sandbox is None:
        raise ToolError("command execution is not available to this role")
    t = min(int(timeout_s or ctx.command_timeout_s), ctx.command_timeout_s)
    if isinstance(ctx.sandbox, DockerSandbox) and ctx.sandbox.cfg.reuse_container:
        from .devtools import session_run   # one long-lived container per agent: no startup per command
        res = await session_run(ctx, command, t)
    else:
        res = await ctx.sandbox.run(ctx.workspace, command, timeout_s=t, artifacts_dir=ctx.artifacts_dir)
    ctx.commands_run.append({"command": command, "exit_code": res.exit_code,
                             "timed_out": res.timed_out, "duration_s": round(res.duration_s, 2)})
    return res.summary()


# ------------------------------------------------------------------ schemas
def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required}}}


S = {"type": "string"}
I = {"type": "integer"}

TOOL_SCHEMAS = {
    "list_files": _fn("list_files", "List files under a directory of the repository (recursive).",
                      {"path": S, "max_depth": I}, []),
    "read_file": _fn("read_file", "Read a text file (with line numbers). Optionally a line range.",
                     {"path": S, "start_line": I, "end_line": I}, ["path"]),
    "search": _fn("search", "Regex search across repository files. Returns path:line: text.",
                  {"pattern": S, "path": S, "glob": {"type": "string", "description": "filename glob like *.py"}},
                  ["pattern"]),
    "write_file": _fn("write_file", "Create or overwrite a file with the full given content.",
                      {"path": S, "content": S}, ["path", "content"]),
    "edit_file": _fn("edit_file", "Replace one exact, unique occurrence of old_text with new_text in a file.",
                     {"path": S, "old_text": S, "new_text": S}, ["path", "old_text", "new_text"]),
    "delete_file": _fn("delete_file", "Delete a file in the repository.", {"path": S}, ["path"]),
    "install_dependencies": _fn(
        "install_dependencies",
        "Install the project's third-party packages declared in requirements*.txt or pyproject.toml "
        "(the only step with internet access). Call it after adding or changing a dependency; "
        "installed packages are importable in later run_command calls.", {}, []),
    "run_command": _fn("run_command",
                       "Run a shell command in an isolated sandbox container at the repository root "
                       "(no network access). Use it to run tests, e.g. `python -m pytest -q`.",
                       {"command": S, "timeout_s": I}, ["command"]),
}

SYNC_IMPLS: dict[str, Callable[..., str]] = {
    "list_files": list_files, "read_file": read_file, "search": search,
    "write_file": write_file, "edit_file": edit_file, "delete_file": delete_file,
}
async def install_dependencies(ctx: ToolContext) -> str:
    if ctx.installer is None:
        raise ToolError("dependency installation is disabled in this SwarmForge configuration")
    return await ctx.installer(ctx.workspace)


ASYNC_IMPLS: dict[str, Callable[..., Awaitable[str]]] = {"run_command": run_command,
                                                        "install_dependencies": install_dependencies}

from .devtools import DEV_IMPLS, DEV_SCHEMAS, DEV_TOOLS  # noqa: E402  (needs the names above)

TOOL_SCHEMAS.update(DEV_SCHEMAS)
ASYNC_IMPLS.update(DEV_IMPLS)

READ_TOOLS = ["list_files", "read_file", "search"]
WRITE_TOOLS = ["write_file", "edit_file", "delete_file"]
EXEC_TOOLS = ["run_command", "install_dependencies"]
# DEV_TOOLS (start_process, read_process_output, stop_process, browse, check_code) come from devtools


@dataclass
class TerminalTool:
    """A tool that ends the agent loop when its arguments validate.

    `validate(args) -> (value, error)`; if error is not None, the error text is returned to the
    model so it can correct itself."""
    name: str
    description: str
    parameters: dict
    validate: Callable[[dict], tuple[Any, str | None]]

    @property
    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


def parse_args(raw: str | dict | None) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ToolError(f"tool arguments are not valid JSON: {e}")
    if isinstance(v, str):  # some models double-encode
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            pass
    if not isinstance(v, dict):
        raise ToolError("tool arguments must be a JSON object")
    return v
