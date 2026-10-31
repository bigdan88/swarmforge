"""Command execution sandboxes for agent- and orchestrator-issued commands.

DockerSandbox (default) runs each command in a fresh, disposable container:
  * only the task's worktree is bind-mounted (plus an optional artifacts dir)
  * --network none by default, --cap-drop ALL, no-new-privileges, read-only root fs,
    pids/memory/cpu limits, non-root user, no host environment variables, no docker socket
LocalSandbox runs directly on the host with a scrubbed environment. It is NOT isolation and
must be opted into explicitly (sandbox.mode: local).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .config import SandboxConfig

MAX_OUTPUT_CHARS = 30_000
DEPS_DIR = ".swarmforge/pydeps"                 # relative to a worktree; git-excluded
DEPS_CONTAINER_PATH = "/workspace/.swarmforge/pydeps"
SECRET_ENV_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)", re.I)


@dataclass
class CommandResult:
    command: str
    exit_code: int
    output: str
    duration_s: float
    timed_out: bool = False

    def summary(self, limit: int = 8000) -> str:
        out = self.output
        if len(out) > limit:
            out = f"...[truncated {len(out) - limit} chars]...\n" + out[-limit:]
        status = "TIMED OUT" if self.timed_out else f"exit code {self.exit_code}"
        return f"$ {self.command}\n[{status}, {self.duration_s:.1f}s]\n{out}"


def _truncate(s: str) -> str:
    if len(s) <= MAX_OUTPUT_CHARS:
        return s
    half = MAX_OUTPUT_CHARS // 2
    return s[:half] + f"\n...[truncated {len(s) - MAX_OUTPUT_CHARS} chars]...\n" + s[-half:]


class SandboxError(RuntimeError):
    pass


class Sandbox:
    async def kill_all(self) -> None:
        return None

    async def run(self, workspace: Path, command: str, *, timeout_s: int | None = None,
                  artifacts_dir: Path | None = None, network: str | None = None,
                  cache_dir: Path | None = None) -> CommandResult:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


class DockerSandbox(Sandbox):
    def __init__(self, cfg: SandboxConfig, run_label: str | None = None):
        self.cfg = cfg
        self.run_label = run_label

    def describe(self) -> str:
        c = self.cfg
        return (f"docker image={c.image} network={c.network} memory={c.memory} cpus={c.cpus} "
                f"pids={c.pids_limit} cap-drop=ALL read-only-rootfs no-new-privileges")

    def build_args(self, workspace: Path, command: str, name: str,
                   artifacts_dir: Path | None = None, network: str | None = None,
                   cache_dir: Path | None = None) -> list[str]:
        c = self.cfg
        args = [
            c.docker_binary, "run", "--rm", "--name", name,
            "--network", network or c.network,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(c.pids_limit),
            "--memory", c.memory,
            "--cpus", str(c.cpus),
            "--read-only",
            "--tmpfs", "/tmp:rw,exec,size=512m",
            "-e", "HOME=/tmp",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "PIP_NO_INPUT=1",
            # project dependencies installed by SwarmForge's install step live inside the worktree
            "-e", f"PYTHONPATH={DEPS_CONTAINER_PATH}",
            "--mount", f"type=bind,source={workspace.resolve()},target=/workspace",
            "-w", "/workspace",
        ]
        if self.run_label:
            args += ["--label", f"swarmforge.run={self.run_label}"]
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)
            args += ["--mount", f"type=bind,source={cache_dir.resolve()},target=/pipcache",
                     "-e", "PIP_CACHE_DIR=/pipcache"]
        if artifacts_dir is not None:
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            args += ["--mount", f"type=bind,source={artifacts_dir.resolve()},target=/artifacts"]
        if hasattr(os, "getuid"):
            args += ["--user", f"{os.getuid()}:{os.getgid()}"]
        args += [c.image, "sh", "-c", command]
        return args

    async def run(self, workspace: Path, command: str, *, timeout_s: int | None = None,
                  artifacts_dir: Path | None = None, network: str | None = None,
                  cache_dir: Path | None = None) -> CommandResult:
        timeout_s = timeout_s or self.cfg.command_timeout_s
        name = f"swarmforge-{uuid.uuid4().hex[:12]}"
        args = self.build_args(workspace, command, name, artifacts_dir, network, cache_dir)
        start = time.time()
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=_scrubbed_env())
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            return CommandResult(command, proc.returncode, _truncate(out.decode(errors="replace")),
                                 time.time() - start)
        except asyncio.TimeoutError:
            await _run_quiet([self.cfg.docker_binary, "kill", name])
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            except asyncio.TimeoutError:
                proc.kill()
                out = b""
            return CommandResult(command, 124, _truncate(out.decode(errors="replace")),
                                 time.time() - start, timed_out=True)

    async def kill_all(self) -> None:
        """Kill every container started for this run (used on cancel/timeout)."""
        if not self.run_label:
            return
        p = await asyncio.create_subprocess_exec(
            self.cfg.docker_binary, "ps", "-q", "--filter", f"label=swarmforge.run={self.run_label}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await p.communicate()
        ids = out.decode().split()
        if ids:
            await _run_quiet([self.cfg.docker_binary, "kill", *ids])

    def image_exists(self) -> bool:
        r = subprocess.run([self.cfg.docker_binary, "image", "inspect", self.cfg.image],
                           capture_output=True)
        return r.returncode == 0

    def build_image(self, dockerfile: str = "Dockerfile.sandbox", tag: str | None = None,
                    build_args: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        text = resources.files("swarmforge").joinpath(f"resources/{dockerfile}").read_text()
        build_args = {"BASE_IMAGE": self.cfg.base_image, **(build_args or {})}
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            Path(td, "Dockerfile").write_text(text)
            args = [self.cfg.docker_binary, "build", "--progress=plain"]
            for k, v in build_args.items():
                args += ["--build-arg", f"{k}={v}"]
            # Output streams straight to the terminal so the user sees pull/pip progress.
            return subprocess.run(args + ["-t", tag or self.cfg.image, td])


class LocalSandbox(Sandbox):
    """Runs on the host. Only for environments where Docker is unavailable. NOT isolated."""

    def __init__(self, cfg: SandboxConfig):
        self.cfg = cfg

    def describe(self) -> str:
        return "local host execution (UNSANDBOXED; scrubbed env only)"

    async def run(self, workspace: Path, command: str, *, timeout_s: int | None = None,
                  artifacts_dir: Path | None = None, network: str | None = None,
                  cache_dir: Path | None = None) -> CommandResult:
        timeout_s = timeout_s or self.cfg.command_timeout_s
        env = _scrubbed_env()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        deps = str((workspace / DEPS_DIR).resolve())
        env["PYTHONPATH"] = deps + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        if artifacts_dir is not None:
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            # Commands written for docker use /artifacts; map it for local mode.
            command = command.replace("/artifacts", str(artifacts_dir.resolve()).replace("\\", "/"))
        command = command.replace(DEPS_CONTAINER_PATH, deps.replace("\\", "/"))
        start = time.time()
        kwargs = {}
        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_shell(
            command, cwd=str(workspace), env=env, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, **kwargs)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            return CommandResult(command, proc.returncode, _truncate(out.decode(errors="replace")),
                                 time.time() - start)
        except asyncio.TimeoutError:
            _kill_tree(proc)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            except asyncio.TimeoutError:
                out = b""
            return CommandResult(command, 124, _truncate(out.decode(errors="replace")),
                                 time.time() - start, timed_out=True)


def _kill_tree(proc) -> None:
    try:
        if sys.platform != "win32":
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    except Exception:
        proc.kill()


def _scrubbed_env() -> dict[str, str]:
    """Host env minus anything that looks like a credential."""
    return {k: v for k, v in os.environ.items() if not SECRET_ENV_RE.search(k)}


async def _run_quiet(args: list[str]) -> None:
    try:
        p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.wait(), timeout=30)
    except Exception:
        pass


def make_sandbox(cfg: SandboxConfig, run_label: str | None = None) -> Sandbox:
    if cfg.mode == "docker":
        if not shutil.which(cfg.docker_binary):
            raise SandboxError(
                f"docker binary '{cfg.docker_binary}' not found. Install Docker or set "
                "sandbox.mode: local (unsandboxed) in the config.")
        return DockerSandbox(cfg, run_label)
    return LocalSandbox(cfg)
