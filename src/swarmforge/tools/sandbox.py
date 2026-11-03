"""Sandbox shell & file-read tools for the chatbot (opt-in via chatbot.enable_shell).

Provides run_command (executes through the swarmforge.sandbox abstraction: docker mode =
no network + read-only rootfs; local mode = host execution for tests), read_file
(path-traversal safe, confined to the project root) and grep (ripgrep with a pure-Python
fallback), all with bounded output.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from ..config import SandboxConfig
from ..sandbox import DockerSandbox, SandboxError, make_sandbox

DEFAULT_TIMEOUT_S = 30
GREP_MAX_LINES = 200
MAX_READ_CHARS = 20_000
MAX_OUTPUT_CHARS = 12_000

_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
}
_MAX_GREP_FILE_BYTES = 1024 * 1024  # 1 MiB


class SandboxToolError(Exception):
    """Raised for invalid paths, bad patterns or sandbox failures."""


class ReadOnlyChatSandbox(DockerSandbox):
    """Chat commands may inspect the project, but cannot change its bind mount."""

    def build_args(self, *args, **kwargs):
        argv = super().build_args(*args, **kwargs)
        for index, value in enumerate(argv):
            if value == "--mount" and "target=/workspace" in argv[index + 1]:
                argv[index + 1] += ",readonly"
        return argv


def resolve_within(root: Path, user_path: str) -> Path:
    """Resolve user_path inside root, rejecting traversal, absolute-outside and symlink escapes."""
    if not user_path or not user_path.strip():
        raise SandboxToolError("path must not be empty")
    root_resolved = Path(root).resolve()
    candidate = Path(user_path)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root_resolved / candidate).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise SandboxToolError(f"path escapes the project root: {user_path}") from None
    if not resolved.exists():
        raise SandboxToolError(f"path does not exist: {user_path}")
    return resolved


class ChatSandboxTools:
    """Shell & file-read tools scoped to a project root.

    sandbox_cfg defaults to local mode so unit tests need no Docker; production wiring
    passes cfg.sandbox so docker mode gives no-network + read-only rootfs execution.
    """

    def __init__(self, root: Path, sandbox_cfg: SandboxConfig | None = None):
        self.root = Path(root).resolve()
        self.sandbox_cfg = sandbox_cfg or SandboxConfig(mode="local")

    async def run_command(
        self, command: str, timeout_s: int = DEFAULT_TIMEOUT_S
    ) -> str:
        """Run a shell command in the sandbox; returns stdout+stderr combined (stderr=STDOUT)."""
        if not command or not command.strip():
            raise SandboxToolError("command must not be empty")
        try:
            sandbox = make_sandbox(self.sandbox_cfg)
            if isinstance(sandbox, DockerSandbox):
                sandbox = ReadOnlyChatSandbox(self.sandbox_cfg)
        except SandboxError as e:
            raise SandboxToolError(str(e)) from e
        timeout_s = max(1, min(DEFAULT_TIMEOUT_S, int(timeout_s)))
        result = await sandbox.run(self.root, command, timeout_s=timeout_s, network="none")
        status = (
            f"[TIMED OUT after {timeout_s}s]"
            if result.timed_out
            else f"[exit {result.exit_code}, {result.duration_s:.1f}s]"
        )
        body = result.output
        if len(body) > MAX_OUTPUT_CHARS:
            half = MAX_OUTPUT_CHARS // 2
            body = body[:half] + "\n... [output truncated] ...\n" + body[-half:]
        return f"{status}\n{command}\n{body}"

    def read_file(self, path: str, max_chars: int = MAX_READ_CHARS) -> str:
        """Read a text file under the project root, truncated to max_chars."""
        p = resolve_within(self.root, path)
        if not p.is_file():
            raise SandboxToolError(f"not a file: {path}")
        text = p.read_bytes().decode("utf-8", errors="replace")
        if len(text) > max_chars:
            return text[:max_chars] + "\n... [truncated]"
        return text

    def grep(
        self, pattern: str, path: str = ".", max_lines: int = GREP_MAX_LINES
    ) -> str:
        """Search for a regex pattern under a path; results truncated to max_lines."""
        target = resolve_within(self.root, path)
        rg = shutil.which("rg")
        if rg:
            try:
                proc = subprocess.run(
                    [
                        rg,
                        "--no-heading",
                        "--line-number",
                        "--color",
                        "never",
                        "--",
                        pattern,
                        str(target),
                    ],
                    cwd=str(self.root),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            except subprocess.TimeoutExpired as e:
                raise SandboxToolError(f"grep timed out: {e}") from e
            if proc.returncode == 1:
                return f"No matches for {pattern!r}"
            if proc.returncode > 1:
                raise SandboxToolError(f"grep failed: {proc.stderr.strip()}")
            return self._truncate_lines(proc.stdout.splitlines(), pattern, max_lines)
        return self._python_grep(pattern, target, max_lines)

    def _truncate_lines(self, lines: list[str], pattern: str, max_lines: int) -> str:
        if not lines:
            return f"No matches for {pattern!r}"
        if len(lines) <= max_lines:
            return "\n".join(lines)
        return (
            "\n".join(lines[:max_lines])
            + f"\n... [truncated: showing first {max_lines} matches]"
        )

    def _python_grep(self, pattern: str, target: Path, max_lines: int) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise SandboxToolError(f"invalid regex {pattern!r}: {e}") from e
        matches: list[str] = []
        if target.is_file():
            files = [target]
        else:
            files = []
            for dirpath, dirnames, filenames in os.walk(target):
                dirnames[:] = [
                    d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")
                ]
                for name in filenames:
                    files.append(Path(dirpath) / name)
        for f in files:
            try:
                if f.stat().st_size > _MAX_GREP_FILE_BYTES or not f.is_file():
                    continue
                text = f.read_bytes().decode("utf-8", errors="replace")
            except OSError:
                continue
            rel = f.relative_to(self.root).as_posix()
            for lineno, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    matches.append(f"{rel}:{lineno}:{line}")
                    if len(matches) > max_lines:
                        return self._truncate_lines(matches, pattern, max_lines)
        return self._truncate_lines(matches, pattern, max_lines)
