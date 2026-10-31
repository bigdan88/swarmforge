"""Start SwarmForge runs as background processes (used by the dashboard).

Each run is an ordinary `swarmforge run` process, detached from the dashboard, so closing the
browser or restarting Streamlit does not stop it. Its console output goes to
<workdir>/logs/<run_id>.log; everything else is in the SQLite store as usual.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import Config
from .orchestrator import new_run_id


def start_run(cfg: Config, *, repo: str | None, issue_url: str | None = None,
              issue_title: str | None = None, issue_body: str | None = None,
              mode: str = "swarm", budget: float | None = None, max_cycles: int | None = None,
              approval: str = "dashboard", single_role: str | None = None,
              test_command: str | None = None, sandbox_image: str | None = None) -> tuple[str, Path]:
    if not issue_url and not (issue_title and issue_body):
        raise ValueError("give a GitHub issue URL, or a title and description")
    if not issue_url and not repo:
        raise ValueError("a repository (local path or git URL) is required for a local issue")
    if cfg.source_path is None:
        raise ValueError("config path unknown; launch the dashboard with `swarmforge dashboard`")
    run_id = new_run_id()
    logs = cfg.workdir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, "-m", "swarmforge.cli", "-c", str(cfg.source_path), "run",
            "--run-id", run_id, "--approval", approval]
    if issue_url:
        args.append(issue_url.strip())
    else:
        issues = cfg.workdir / "issues"
        issues.mkdir(parents=True, exist_ok=True)
        f = issues / f"{run_id}.md"
        f.write_text(f"# {issue_title.strip()}\n\n{issue_body.strip()}\n", encoding="utf-8")
        args += ["--issue-file", str(f)]
    if repo:
        args += ["--repo", repo.strip()]
    if mode == "single":
        args.append("--single")
        if single_role:
            args += ["--role", single_role]
    if budget is not None:
        args += ["--budget", str(budget)]
    if max_cycles is not None:
        args += ["--max-cycles", str(max_cycles)]
    if test_command:
        args += ["--test-command", test_command]
    if sandbox_image:
        args += ["--sandbox-image", sandbox_image]

    log_path = logs / f"{run_id}.log"
    log = open(log_path, "w", encoding="utf-8")
    kwargs: dict = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                    "cwd": str(cfg.source_path.parent),
                    "env": {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)
    log.close()
    return run_id, log_path


def pid_alive(pid: int | None) -> bool:
    """Cross-platform 'is this process still running' (never signals the process)."""
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    try:  # reap it if it is our own exited child (avoids zombies counting as alive)
        done, _ = os.waitpid(int(pid), os.WNOHANG)
        if done:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
