"""Fast environment checks run before a run starts (dashboard) and at the start of a run.

Each problem is reported as a plain-language message with the fix, instead of failing deep inside
a run with a stack trace."""
from __future__ import annotations

import shutil
import subprocess

from .config import Config


def environment_problems(cfg: Config, image: str | None = None, *, docker_timeout_s: int = 20) -> list[str]:
    problems: list[str] = []
    used = {cfg.roles[r].provider for r in ["planner", "reviewer", "tester", cfg.librarian_role,
                                              *cfg.developer_roles] if r in cfg.roles}
    for name in sorted(used):
        p = cfg.providers[name]
        if not p.api_key():
            problems.append(f"The API key for provider '{name}' is missing: add {p.api_key_env}=... to the .env "
                            f"file next to {cfg.source_path or 'swarmforge.yaml'} and restart the dashboard.")
    if not shutil.which("git"):
        problems.append("git is not installed or not on PATH. Install Git for Windows and reopen the terminal.")
    if cfg.sandbox.mode == "docker":
        d = cfg.sandbox.docker_binary
        if not shutil.which(d):
            problems.append("Docker is not installed (or not on PATH). Install Docker Desktop, or set "
                            "sandbox.mode: local in swarmforge.yaml (unsandboxed).")
            return problems
        try:
            info = subprocess.run([d, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True,
                                  timeout=docker_timeout_s)
            ok = info.returncode == 0
            err = (info.stderr or info.stdout).strip().splitlines()[-1:] if not ok else []
        except subprocess.TimeoutExpired:
            ok, err = False, ["`docker info` did not answer"]
        if not ok:
            problems.append("Docker is not responding (" + (err[0][:160] if err else "no answer") + "). Start Docker "
                            "Desktop and wait for 'Engine running'; if it is already running, quit it, run "
                            "`wsl --shutdown`, and start it again.")
            return problems
        img = image or cfg.sandbox.image
        if subprocess.run([d, "image", "inspect", img], capture_output=True).returncode != 0:
            fix = "swarmforge sandbox build --dev" if "dev-sandbox" in img else "swarmforge sandbox build"
            problems.append(f"The sandbox image '{img}' is not built yet. Run `{fix}` once.")
    return problems
