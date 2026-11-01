"""Fast environment checks run before a run starts (dashboard) and at the start of a run.

Each problem is reported as a plain-language message with the fix, instead of failing deep inside
a run with a stack trace. The low-level probes return structured (message, fix) pairs; both the
prose API (environment_problems) and the structured API (environment_check_results) are composed
from them so the two can never drift apart."""
from __future__ import annotations

import shutil
import subprocess

from .config import Config


def _missing_key_problems(cfg: Config) -> list[tuple[str, str]]:
    """One (message, fix) per provider in use whose API key is not set."""
    used = {cfg.roles[r].provider for r in ["planner", "reviewer", "tester", cfg.librarian_role,
                                             *cfg.developer_roles] if r in cfg.roles}
    out: list[tuple[str, str]] = []
    for name in sorted(used):
        p = cfg.providers[name]
        if not p.api_key():
            message = f"The API key for provider '{name}' is missing"
            fix = (f"add {p.api_key_env}=... to the .env file next to "
                   f"{cfg.source_path or 'swarmforge.yaml'} and restart the dashboard.")
            out.append((message, fix))
    return out


def _git_problem() -> tuple[str, str] | None:
    if not shutil.which("git"):
        return ("git is not installed or not on PATH.",
                "Install Git for Windows and reopen the terminal.")
    return None


def _docker_problem(cfg: Config, docker_timeout_s: int) -> tuple[str, str] | None:
    """(message, fix) when the docker binary is missing or the daemon does not answer."""
    d = cfg.sandbox.docker_binary
    if not shutil.which(d):
        return ("Docker is not installed (or not on PATH).",
                ("Install Docker Desktop, or set sandbox.mode: local in swarmforge.yaml "
                 "(unsandboxed)."))
    try:
        info = subprocess.run([d, "info", "--format", "{{.ServerVersion}}"], capture_output=True,
                              text=True, timeout=docker_timeout_s)
        ok = info.returncode == 0
        err = (info.stderr or info.stdout).strip().splitlines()[-1:] if not ok else []
    except subprocess.TimeoutExpired:
        ok, err = False, ["`docker info` did not answer"]
    if not ok:
        return ("Docker is not responding (" + (err[0][:160] if err else "no answer") + ").",
                ("Start Docker Desktop and wait for 'Engine running'; if it is already running, "
                 "quit it, run `wsl --shutdown`, and start it again."))
    return None


def _image_problems(cfg: Config, image: str | None) -> list[tuple[str, str]]:
    """(message, fix) when the sandbox image is not built yet (docker mode only)."""
    img = image or cfg.sandbox.image
    if subprocess.run([cfg.sandbox.docker_binary, "image", "inspect", img],
                      capture_output=True).returncode != 0:
        fix = "swarmforge sandbox build --dev" if "dev-sandbox" in img else "swarmforge sandbox build"
        return [(f"The sandbox image '{img}' is not built yet.", fix)]
    return []


def environment_problems(cfg: Config, image: str | None = None, *, docker_timeout_s: int = 20) -> list[str]:
    problems: list[str] = []
    for message, fix in _missing_key_problems(cfg):
        problems.append(f"{message}: {fix}")
    git = _git_problem()
    if git:
        problems.append(f"{git[0]} {git[1]}")
    if cfg.sandbox.mode == "docker":
        d = _docker_problem(cfg, docker_timeout_s)
        if d:
            problems.append(f"{d[0]} {d[1]}")
            return problems
        for message, fix in _image_problems(cfg, image):
            problems.append(f"{message} Run `{fix}` once.")
    return problems


def environment_check_results(cfg: Config, image: str | None = None, *,
                              docker_timeout_s: int = 20) -> list[dict]:
    """Structured rows ({'name','status','message','fix'}, status 'ok'|'fail'|'skipped')
    for the same probes. 'skipped' means the check could not be performed.

    In local mode no docker/image rows are emitted, so callers can show the
    'not checked (sandbox.mode: local)' note themselves."""
    rows: list[dict] = []
    keys = _missing_key_problems(cfg)
    rows.append({"name": "Provider API keys",
                 "status": "fail" if keys else "ok",
                 "message": "; ".join(m for m, _ in keys),
                 "fix": "; ".join(f for _, f in keys)})
    git = _git_problem()
    rows.append({"name": "Git", "status": "fail" if git else "ok",
                 "message": git[0] if git else "", "fix": git[1] if git else ""})
    if cfg.sandbox.mode == "docker":
        d = _docker_problem(cfg, docker_timeout_s)
        rows.append({"name": "Docker running", "status": "fail" if d else "ok",
                     "message": d[0] if d else "", "fix": d[1] if d else ""})
        if d:
            rows.append({"name": "Sandbox image built", "status": "skipped",
                         "message": "not checked (Docker is not responding)", "fix": d[1]})
        else:
            imgs = _image_problems(cfg, image)
            rows.append({"name": "Sandbox image built",
                         "status": "fail" if imgs else "ok",
                         "message": "; ".join(m for m, _ in imgs),
                         "fix": "; ".join(f for _, f in imgs)})
    return rows
