"""Second batch of developer tools: tests, code navigation, git, HTTP and environment introspection.

Like the tools in devtools.py, everything here runs where `run_command` runs (the sandbox, or the
host in local mode) or reads files through `ctx.resolve`: no new privileges, no network beyond
localhost apps started with `start_process`, no writes to `.git` (the git tools are strictly
read-only and run on the host, because the worktree's `.git` points outside the sandbox container).
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import shlex
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Awaitable, Callable

from ..sandbox import DEPS_DIR, CommandResult, DockerSandbox
from .devtools import LOCAL_URL_RE, SAFE_PATH_RE, open_session, session_run
from .tools import ToolContext, ToolError, _fn

MAX_NAV_CHARS = 20_000
MAX_DIFF_CHARS = 40_000
HTTP_MARK = "__SF_HTTP__"
SHELL_META_RE = re.compile(r"[;&|`$\n\r]")
SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
GIT_SUBCOMMANDS = {"diff", "status", "rev-parse", "log", "blame", "show"}
GIT_REF_RE = re.compile(r"^[0-9a-fA-F]{4,40}$|^[A-Za-z_][A-Za-z0-9_.~/^-]{0,63}$")
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", ".ruff_cache",
             ".pytest_cache", ".swarmforge"}


# ---------------------------------------------------------------- helpers
def _cap(text: str, limit: int = MAX_NAV_CHARS) -> str:
    """Tail-truncate long output, keeping the end (where errors usually are)."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _validate_shell_arg(s: str) -> str:
    if s is None or not str(s).strip():
        raise ToolError("empty argument")
    if SHELL_META_RE.search(str(s)):
        raise ToolError(f"unsupported characters in argument: {s!r}")
    return str(s)


def _quote(s: str) -> str:
    return shlex.quote(_validate_shell_arg(s))


async def _exec(ctx: ToolContext, command: str, timeout_s: int) -> CommandResult:
    if ctx.sandbox is None:
        raise ToolError("command execution is not available to this role")
    res = await session_run(ctx, command, int(timeout_s))
    ctx.commands_run.append({"command": command, "exit_code": res.exit_code,
                             "timed_out": res.timed_out, "duration_s": round(res.duration_s, 2)})
    return res


def _py(ctx: ToolContext) -> str:
    """The python interpreter to use inside the sandbox (mirrors check_code)."""
    return "python" if isinstance(ctx.sandbox, DockerSandbox) else f'"{sys.executable}"'


async def _git(ctx: ToolContext, *args: str, timeout_s: int = 60) -> "asyncio.subprocess.Process":
    """Run a whitelisted, read-only git subcommand on the host with cwd=ctx.workspace."""
    if not args or args[0] not in GIT_SUBCOMMANDS:
        raise ToolError(f"git subcommand not allowed: {args[0] if args else '(none)'} "
                        f"(allowed: {', '.join(sorted(GIT_SUBCOMMANDS))})")
    return await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(ctx.workspace), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)


async def _git_out(ctx: ToolContext, *args: str, timeout_s: int = 60) -> tuple[int, str]:
    p = await _git(ctx, *args, timeout_s=timeout_s)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        p.kill()
        raise ToolError(f"git {' '.join(args[:2])} timed out") from None
    return int(p.returncode or 0), out.decode(errors="replace")


async def _require_git_repo(ctx: ToolContext) -> None:
    code, out = await _git_out(ctx, "rev-parse", "--git-dir", timeout_s=15)
    if code != 0:
        raise ToolError(f"not a git repository (or git is unavailable): {out.strip()[-300:]}")


def _git_ref(base: str) -> str:
    base = str(base or "HEAD").strip()
    if not GIT_REF_RE.match(base):
        raise ToolError(f"invalid git ref: {base!r}")
    return base


# ---------------------------------------------------------------- run_tests
async def run_tests(ctx: ToolContext, paths: list[str] | None = None, keyword: str | None = None,
                    last_failed: bool = False) -> str:
    if ctx.sandbox is None:
        raise ToolError("command execution is not available to this role")
    parts = [ctx.test_command, "-p", "no:cacheprovider",
             "--junit-xml=.swarmforge/sf-navtests.xml"]
    if paths:
        if isinstance(paths, str):
            paths = [paths]
        parts += [_quote(str(p)) for p in paths]
    if keyword:
        parts += ["-k", _quote(str(keyword))]
    if last_failed:
        parts.append("--lf")
    command = f"mkdir -p .swarmforge && {' '.join(parts)}"
    res = await _exec(ctx, command, min(ctx.command_timeout_s, 600))
    if res.timed_out:
        raise ToolError(f"tests timed out after {res.duration_s:.0f}s\n{_cap(res.output, 4000)}")
    xml_path = ctx.workspace / ".swarmforge" / "sf-navtests.xml"
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError) as e:
        raise ToolError(f"could not read the pytest JUnit report ({e}); raw output:\n"
                        f"{_cap(res.output, 4000)}") from e
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise ToolError(f"no test results in the JUnit report; raw output:\n{_cap(res.output, 4000)}")
    total = sum(int(s.get("tests", 0) or 0) for s in suites)
    failures = sum(int(s.get("failures", 0) or 0) for s in suites)
    errors = sum(int(s.get("errors", 0) or 0) for s in suites)
    skipped = sum(int(s.get("skipped", 0) or 0) for s in suites)
    passed = max(total - failures - errors - skipped, 0)
    L = [(f"{passed} passed, {failures} failed, {errors} error(s), {skipped} skipped "
          f"({total} total, exit code {res.exit_code})")]
    n = 0
    for suite in suites:
        for tc in suite.iter("testcase"):
            child = tc.find("failure")
            if child is None:
                child = tc.find("error")
            if child is None:
                continue
            n += 1
            cls, name = tc.get("classname", ""), tc.get("name", "")
            node = f"{cls}::{name}" if cls else name
            msg = (child.get("message") or "").strip()
            tb = (child.text or "").strip().splitlines()
            L += [f"--- FAIL {n}: {node}", f"    {msg}" if msg else "    (no message)"]
            if tb:
                tail = tb[-15:]
                L.append("    traceback (last 15 lines):")
                L += [f"      {ln}" for ln in tail]
    if failures + errors > 0 and n == 0:
        L.append("(the JUnit report lists failures but no details; see the raw output below)")
        L.append(_cap(res.output, 4000))
    return _cap("\n".join(L))


# ---------------------------------------------------------------- outline
def _format_args(node: "ast.FunctionDef | ast.AsyncFunctionDef") -> str:
    a = node.args
    args = [x.arg for x in getattr(a, "posonlyargs", [])] + [x.arg for x in a.args]
    if a.vararg:
        args.append("*" + a.vararg.arg)
    for x in a.kwonlyargs:
        args.append(x.arg)
    if a.kwarg:
        args.append("**" + a.kwarg.arg)
    return ", ".join(args)


def _outline_py(tree: ast.Module, lines: list[str]) -> None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                lines.append(f"  @{ast.unparse(dec)} [L{node.lineno}]")
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            lines.append(f"{prefix} {node.name}({_format_args(node)}) [L{node.lineno}]")
        elif isinstance(node, ast.ClassDef):
            for dec in node.decorator_list:
                lines.append(f"  @{ast.unparse(dec)} [L{node.lineno}]")
            lines.append(f"class {node.name} [L{node.lineno}]")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = "async def" if isinstance(sub, ast.AsyncFunctionDef) else "def"
                    lines.append(f"  {prefix} {sub.name}({_format_args(sub)}) [L{sub.lineno}]")


async def outline(ctx: ToolContext, path: str) -> str:
    p = ctx.resolve(path, must_exist=True)
    if p.is_dir():
        raise ToolError(f"{path} is a directory; outline a single file")
    if p.stat().st_size > 1_000_000:
        raise ToolError(f"{path} is larger than 1 MB; too big to outline")
    text = p.read_text(encoding="utf-8", errors="replace")
    rel = ctx.rel(p)
    suffix = p.suffix.lower()
    if suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError as e:
            raise ToolError(f"could not parse {rel}: {e}") from e
        lines: list[str] = [f"{rel}:"]
        _outline_py(tree, lines)
        return _cap("\n".join(lines) if len(lines) > 1 else f"{rel}: (no top-level definitions)")
    if suffix == ".md":
        lines = [f"{rel} (headings):"]
        lines += [f"  L{i}: {ln.strip()}" for i, ln in enumerate(text.splitlines(), 1)
                  if re.match(r"^#{1,6}\s", ln)]
        return _cap("\n".join(lines) if len(lines) > 1 else f"{rel}: (no headings)")
    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ToolError(f"could not parse {rel}: {e}") from e
        keys = list(data.keys()) if isinstance(data, dict) else [f"(top-level {type(data).__name__})"]
        return _cap(f"{rel} (top-level keys):\n" + "\n".join(f"  {k}" for k in keys))
    if suffix in (".yaml", ".yml"):
        keys = []
        for ln in text.splitlines():
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_.-]*):", ln)
            if m and m.group(1) not in keys:
                keys.append(m.group(1))
        return _cap(f"{rel} (top-level keys):\n" + ("\n".join(f"  {k}" for k in keys)
                                                    or "  (none found)"))
    head = "\n".join(text.splitlines()[:40])
    return _cap(f"{rel}: no outline for this file type; first 40 lines:\n{head}")


# ---------------------------------------------------------------- find_symbol
class _SymbolVisitor(ast.NodeVisitor):
    """Collects definitions and references of one symbol in a single parsed file."""

    def __init__(self, ctx: ToolContext, name: str, path: Path, lines: list[str],
                 definitions: list[str], references: list[str]):
        self.ctx, self.name, self.path, self.lines = ctx, name, path, lines
        self.definitions, self.references = definitions, references
        self.cls = ""

    def _report(self, bucket: list[str], lineno: int, label: str = "") -> None:
        if 1 <= lineno <= len(self.lines):
            text = self.lines[lineno - 1].strip()[:200]
            bucket.append(f"{self.ctx.rel(self.path)}:{lineno}: {label + '  ' + text if label else text}")

    def visit_FunctionDef(self, node):
        if node.name == self.name:
            label = f"{self.cls}.{node.name}" if self.cls else ""
            self._report(self.definitions, node.lineno, label)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self.visit_FunctionDef(node)

    def visit_ClassDef(self, node):
        if node.name == self.name:
            self._report(self.definitions, node.lineno)
        outer, self.cls = self.cls, node.name
        self.generic_visit(node)
        self.cls = outer

    def visit_Assign(self, node):
        for t in node.targets:
            for tgt in (t.elts if isinstance(t, ast.Tuple) else [t]):
                if isinstance(tgt, ast.Name) and tgt.id == self.name:
                    self._report(self.definitions, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if isinstance(node.target, ast.Name) and node.target.id == self.name:
            self._report(self.definitions, node.lineno)
        self.generic_visit(node)

    def visit_Import(self, node):
        for a in node.names:
            if a.asname == self.name or (a.asname is None and a.name.split(".")[0] == self.name):
                self._report(self.definitions, node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for a in node.names:
            if a.asname == self.name or (a.asname is None and a.name == self.name):
                self._report(self.definitions, node.lineno)
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id == self.name and isinstance(node.ctx, ast.Load):
            self._report(self.references, node.lineno)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr == self.name:
            self._report(self.references, node.lineno)
        self.generic_visit(node)


async def find_symbol(ctx: ToolContext, name: str) -> str:
    if not SYMBOL_RE.match(str(name or "")):
        raise ToolError(f"invalid symbol name: {name!r} (letters, digits and '_' only)")
    definitions: list[str] = []
    references: list[str] = []

    files = []
    for fp in ctx.workspace.rglob("*.py"):
        rel_parts = fp.relative_to(ctx.workspace).parts
        if any(part.startswith(".") or part in SKIP_DIRS or part == DEPS_DIR for part in rel_parts[:-1]):
            continue
        files.append(fp)
        if len(files) >= 2000:
            break
    for fp in sorted(files):
        try:
            if fp.stat().st_size > 512_000:
                continue
            src = fp.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src)
        except (OSError, SyntaxError, ValueError):
            continue
        lines = src.splitlines()
        _SymbolVisitor(ctx, name, fp, lines, definitions, references).visit(tree)

    L = [f"Definitions ({len(definitions)}):"]
    L += [f"  {d}" for d in definitions[:100]]
    if len(definitions) > 100:
        L.append(f"  ...and {len(definitions) - 100} more")
    L.append(f"References ({len(references)}):")
    L += [f"  {r}" for r in references[:100]]
    if len(references) > 100:
        L.append(f"  ...and {len(references) - 100} more")
    if not definitions and not references:
        return f"no definitions or references of '{name}' found in the repository's Python files"
    return _cap("\n".join(L))


# ---------------------------------------------------------------- git tools
async def git_changes(ctx: ToolContext, path: str | None = None) -> str:
    await _require_git_repo(ctx)
    base = _git_ref(ctx.base_sha or "HEAD")
    rel_scope = ""
    if path:
        full = ctx.resolve(str(path), must_exist=True)
        rel_scope = ctx.rel(full)
        if not SAFE_PATH_RE.match(rel_scope):
            raise ToolError(f"unsupported characters in path: {rel_scope}")
    code, names = await _git_out(ctx, "diff", "--name-status", base, timeout_s=30)
    if code != 0:
        raise ToolError(f"git diff failed: {names.strip()[-500:]}")
    code, status = await _git_out(ctx, "status", "--porcelain", timeout_s=15)
    if code != 0:
        raise ToolError(f"git status failed: {status.strip()[-500:]}")
    untracked = [ln for ln in status.splitlines()
                 if ln.startswith("??") and (not rel_scope or rel_scope in ln)]
    L = [f"files changed vs {base[:8]}:"]
    changed = [ln.strip() for ln in names.splitlines() if ln.strip()]
    if rel_scope:
        changed = [ln for ln in changed if rel_scope in ln]
    L += [f"  {ln}" for ln in changed] or ["  (none)"]
    if untracked:
        L.append("untracked:")
        L += [f"  {ln.strip()}" for ln in untracked]
    diff_args = ["diff", base] + (["--", rel_scope] if rel_scope else [])
    code, diff = await _git_out(ctx, *diff_args, timeout_s=60)
    if code != 0:
        raise ToolError(f"git diff failed: {diff.strip()[-500:]}")
    L.append("--- diff ---")
    L.append(_cap(diff, MAX_DIFF_CHARS))
    return "\n".join(L)


async def git_history(ctx: ToolContext, path: str, max_commits: int = 20,
                      line_start: int | None = None, line_end: int | None = None) -> str:
    await _require_git_repo(ctx)
    full = ctx.resolve(str(path), must_exist=True)
    rel = ctx.rel(full)
    if not SAFE_PATH_RE.match(rel):
        raise ToolError(f"unsupported characters in path: {rel}")
    n = max(1, min(int(max_commits or 20), 100))
    code, log = await _git_out(ctx, "log", "-n", str(n), "--date=short",
                              "--format=%h | %ad | %s", "--", rel, timeout_s=30)
    if code != 0:
        raise ToolError(f"git log failed for {rel}: {log.strip()[-500:]}")
    L = [f"history of {rel} (last {n} commits):"]
    L += [f"  {ln}" for ln in log.splitlines() if ln.strip()] or ["  (no commits)"]
    if line_start is not None or line_end is not None:
        s = max(1, int(line_start or 1))
        e = max(s, int(line_end or s))
        code, blame = await _git_out(ctx, "blame", "-L", f"{s},{e}", "--", rel, timeout_s=30)
        if code != 0:
            L.append(f"git blame failed: {blame.strip()[-300:]}")
        else:
            L.append(f"--- blame lines {s}-{e} ---")
            L.append(_cap(blame, 8000))
    return _cap("\n".join(L))


# ---------------------------------------------------------------- http_request
def _http_script() -> str:
    return r'''
import json, os, urllib.request, urllib.error
args = json.loads(os.environ["SF_HTTP_ARGS"])
req = urllib.request.Request(args["url"], method=args["method"])
body = None
if args.get("json") is not None:
    body = json.dumps(args["json"]).encode()
    req.add_header("Content-Type", "application/json")
for k, v in (args.get("headers") or {}).items():
    req.add_header(str(k), str(v))
try:
    with urllib.request.urlopen(req, data=body, timeout=20) as r:
        raw = r.read().decode(errors="replace")
        print("__SF_HTTP__" + json.dumps({"status": r.status,
                                          "headers": dict(list(r.headers.items())[:20]),
                                          "body": raw[:8000]}))
except urllib.error.HTTPError as e:
    raw = e.read().decode(errors="replace") if e.fp else ""
    print("__SF_HTTP__" + json.dumps({"status": e.code, "headers": dict(list(e.headers.items())[:20]),
                                      "body": raw[:8000]}))
except Exception as e:
    print("__SF_HTTP__" + json.dumps({"error": f"{type(e).__name__}: {e}"}))
'''


async def http_request(ctx: ToolContext, method: str, url: str, json_body=None, headers=None) -> str:
    if not LOCAL_URL_RE.match(url or ""):
        raise ToolError("http_request only talks to local apps started with start_process "
                        "(e.g. http://localhost:8501). The sandbox has no internet access.")
    method = str(method or "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        raise ToolError(f"unsupported HTTP method: {method!r}")
    if headers is not None and not isinstance(headers, dict):
        raise ToolError("headers must be an object of name/value pairs")
    s = open_session(ctx)
    args = {"method": method, "url": url, "json": json_body, "headers": headers}
    r = await s.exec(f"{s.python} -", stdin=_http_script(),
                     env={"SF_HTTP_ARGS": json.dumps(args)}, timeout_s=30)
    ctx.commands_run.append({"command": f"[http_request] {method} {url}", "exit_code": r.exit_code,
                             "timed_out": r.timed_out, "duration_s": round(r.duration_s, 2)})
    if r.timed_out:
        raise ToolError(f"the request timed out after {r.duration_s:.0f}s")
    line = next((ln for ln in reversed(r.output.splitlines()) if ln.startswith(HTTP_MARK)), None)
    if line is None:
        if "No module named" in r.output:
            raise ToolError("python is not available in the sandbox session")
        raise ToolError(f"the request failed: {r.output[-1500:]}")
    data = json.loads(line[len(HTTP_MARK):])
    if data.get("error"):
        msg = data["error"]
        if "ConnectionRefused" in msg or "refused" in msg:
            raise ToolError(f"could not connect to {url} - the app may not be running "
                            f"(start it with start_process and check read_process_output). {msg}")
        raise ToolError(f"request to {url} failed: {msg}")
    body = data.get("body") or ""
    try:
        parsed = json.loads(body)
        body = json.dumps(parsed, indent=2)
    except (json.JSONDecodeError, ValueError):
        pass
    hdrs = "\n".join(f"  {k}: {v}" for k, v in (data.get("headers") or {}).items())
    return _cap(f"HTTP {data.get('status')}\n--- headers ---\n{hdrs or '  (none)'}\n"
                f"--- body ---\n{body or '(empty)'}")


# ---------------------------------------------------------------- format_code
async def format_code(ctx: ToolContext, paths: list[str] | None = None) -> str:
    if ctx.sandbox is None:
        raise ToolError("command execution is not available to this role")
    if paths:
        if isinstance(paths, str):
            paths = [paths]
        targets = []
        for p in paths:
            full = ctx.resolve(str(p), must_exist=True)
            rel = ctx.rel(full)
            if not SAFE_PATH_RE.match(rel):
                raise ToolError(f"unsupported characters in path: {rel}")
            targets.append(rel)
    else:
        targets = sorted(f for f in ctx.files_written
                         if f.endswith(".py") and (ctx.workspace / f).is_file()
                         and SAFE_PATH_RE.match(f))
    if not targets:
        return "no Python files to format (pass `paths` or write some files first)"
    quoted = " ".join(shlex.quote(t) for t in targets)
    py = _py(ctx)
    probe = await _exec(ctx, f"{py} -m ruff --version", 60)
    if probe.exit_code != 0 or "No module named" in probe.output or "command not found" in probe.output \
            or probe.exit_code == 127:
        raise ToolError("ruff is not available in this sandbox. Rebuild the image with "
                        "`swarmforge sandbox build`, or in local mode run `pip install -e \".[tools]\"`.")
    check = await _exec(ctx, f"{py} -m ruff format --check {quoted}", 120)
    if check.exit_code == 0:
        return f"already formatted: {', '.join(targets)}"
    if check.exit_code == 1 and "error" not in check.output.lower():
        res = await _exec(ctx, f"{py} -m ruff format {quoted}", 120)
        if res.exit_code != 0:
            raise ToolError(f"ruff format failed:\n{_cap(res.output, 4000)}")
        changed = [ln.split(":", 1)[1].strip() for ln in check.output.splitlines()
                   if ln.startswith("Would reformat:")]
        changed = [c for c in changed if c] or targets
        return f"formatted: {', '.join(changed)}"
    raise ToolError(f"ruff format --check failed:\n{_cap(check.output, 4000)}")


# ---------------------------------------------------------------- list_packages
async def list_packages(ctx: ToolContext, name: str | None = None) -> str:
    if ctx.sandbox is None:
        raise ToolError("command execution is not available to this role")
    py = _py(ctx)
    if not name:
        res = await _exec(ctx, f"{py} -m pip list --format=freeze", 120)
        if res.exit_code != 0:
            raise ToolError(f"pip list failed:\n{_cap(res.output, 4000)}")
        out = res.output.strip()
        if len(out) > MAX_NAV_CHARS:
            out = out[:MAX_NAV_CHARS] + "\n...[truncated]"
        return f"installed packages:\n{out or '(none)'}"
    name = str(name)
    if not MODULE_RE.match(name):
        raise ToolError(f"invalid module name: {name!r}")
    probe = (
        "import importlib.util, importlib.metadata, json\n"
        f"spec = importlib.util.find_spec({name!r})\n"
        "if spec is None:\n"
        "    print(json.dumps({'found': False}))\n"
        "else:\n"
        "    ver = ''\n"
        "    try:\n"
        f"        ver = importlib.metadata.version({name.split('.')[0]!r})\n"
        "    except Exception:\n"
        "        pass\n"
        "    print(json.dumps({'found': True, 'origin': spec.origin, 'version': ver}))\n"
    )
    res = await _exec(ctx, f'{py} -c {shlex.quote(probe)}', 60)
    if res.exit_code != 0:
        raise ToolError(f"could not check module {name!r}:\n{_cap(res.output, 2000)}") from None
    line = res.output.strip().splitlines()[-1] if res.output.strip() else ""
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, IndexError):
        raise ToolError(f"could not check module {name!r}:\n{_cap(res.output, 2000)}") from None
    if not data.get("found"):
        return f"{name}: not installed"
    ver = f" (version {data['version']})" if data.get("version") else ""
    return f"{name}: importable{ver}\n  from {data.get('origin')}"


# ---------------------------------------------------------------- schemas
S = {"type": "string"}
I = {"type": "integer"}

NAV_SCHEMAS = {
    "run_tests": _fn(
        "run_tests",
        "Run the project's tests (pytest) and get a structured summary: pass/fail counts and, for each "
        "failure, the test's node id, the assertion message and the last traceback lines. Faster and "
        "clearer than reading raw pytest output through run_command. Optional paths, a `-k` keyword or "
        "`--lf` (last failed only).",
        {"paths": {"type": "array", "items": {"type": "string"}, "description": "files/dirs to test"},
         "keyword": {"type": "string", "description": "pytest -k expression"},
         "last_failed": {"type": "boolean", "description": "re-run only the tests that failed last time"}},
        []),
    "outline": _fn(
        "outline",
        "Show the structure of a file: classes, functions and methods with signatures and line numbers "
        "(Python, via ast), Markdown headings, or the top-level keys of JSON/YAML. Use it to get your "
        "bearings in a file before reading the whole thing.",
        {"path": S}, ["path"]),
    "find_symbol": _fn(
        "find_symbol",
        "Find where a function, class or variable is DEFINED and every place it is REFERENCED across "
        "the repository's Python files (real syntax analysis, so strings and comments that merely "
        "contain the name are not reported). Returns file:line plus the source line.",
        {"name": {"type": "string", "description": "e.g. 'run_tests' or 'ToolContext'"}}, ["name"]),
    "git_changes": _fn(
        "git_changes",
        "Show what changed in your worktree compared to the run's base commit: a summary of the files "
        "changed (including untracked ones) followed by the diff. Read-only; use it to review your own "
        "work before finishing.",
        {"path": {"type": "string", "description": "limit the diff to this file/dir"}}, []),
    "git_history": _fn(
        "git_history",
        "Read-only git log for one file (hash, date, subject), optionally with git blame for a line "
        "range. Use it to understand why code looks the way it does before changing it.",
        {"path": S, "max_commits": I, "line_start": I, "line_end": I}, ["path"]),
    "http_request": _fn(
        "http_request",
        "Send an HTTP request to a LOCAL app started with start_process (localhost/127.0.0.1 only, "
        "like browse) and get the status, headers and body (pretty-printed when JSON). Use it to test "
        "an API endpoint you just wrote.",
        {"method": {"type": "string", "description": "GET, POST, PUT, PATCH, DELETE, HEAD or OPTIONS"},
         "url": {"type": "string", "description": "e.g. http://localhost:8000/api/health"},
         "json": {"description": "JSON-serializable request body"},
         "headers": {"type": "object", "description": "request headers"}},
        ["method", "url"]),
    "format_code": _fn(
        "format_code",
        "Run ruff format on the given files (by default: the Python files you changed) and report "
        "which files were reformatted. Run it before `finish`.",
        {"paths": {"type": "array", "items": {"type": "string"}, "description": "files/dirs to format"}},
        []),
    "list_packages": _fn(
        "list_packages",
        "List the packages installed in your sandbox with their versions, or check whether one module "
        "can be imported and from where. Use it instead of guessing what is available.",
        {"name": {"type": "string", "description": "module to check, e.g. 'numpy' or 'swarmforge'"}}, []),
}

NAV_IMPLS: dict[str, Callable[..., Awaitable[str]]] = {
    "run_tests": run_tests, "outline": outline, "find_symbol": find_symbol,
    "git_changes": git_changes, "git_history": git_history, "http_request": http_request,
    "format_code": format_code, "list_packages": list_packages,
}
NAV_TOOLS = list(NAV_SCHEMAS)
NAV_READONLY = ["outline", "find_symbol", "git_changes", "git_history"]
