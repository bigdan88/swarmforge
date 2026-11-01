"""First-run welcome helpers: structured environment checks and sample-run setup.

The dashboard calls these functions via module attributes (``welcome.health_checks`` etc.) so
tests can monkeypatch them. All checks are cheap and non-network except where unavoidable."""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Config

SAMPLE_BUDGET_USD = 3.0
SAMPLE_MAX_CYCLES = 1
SAMPLE_REPO_DEFAULT = "~/swarmforge-sample"


@dataclass
class CheckItem:
    name: str
    ok: bool
    optional: bool = False
    fix: str = ""


def docker_running(cfg: Config) -> bool:
    if cfg.sandbox.mode != "docker":
        return True
    d = cfg.sandbox.docker_binary
    if not shutil.which(d):
        return False
    try:
        info = subprocess.run([d, "info", "--format", "{{.ServerVersion}}"], check=False,
                              capture_output=True, timeout=10)
        return info.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def image_built(cfg: Config) -> bool:
    if cfg.sandbox.mode != "docker":
        return True
    d = cfg.sandbox.docker_binary
    try:
        return subprocess.run([d, "image", "inspect", cfg.sandbox.image],
                              check=False, capture_output=True).returncode == 0
    except OSError:
        return False


def missing_api_keys(cfg: Config) -> list[str]:
    used = {cfg.roles[r].provider for r in ["planner", "reviewer", "tester", cfg.librarian_role,
                                            *cfg.developer_roles] if r in cfg.roles}
    missing: list[str] = []
    for name in sorted(used):
        p = cfg.providers[name]
        if not p.api_key():
            missing.append(p.api_key_env)
    return missing


def github_logged_in(cfg: Config) -> bool:
    try:
        from . import github
        return github.get_token(cfg.github) is not None
    except Exception:
        return False


def health_checks(cfg: Config) -> list[CheckItem]:
    items: list[CheckItem] = []

    def add(name: str, fn, fix: str, optional: bool = False) -> None:
        try:
            ok = bool(fn(cfg))
        except Exception as exc:  # a broken check must never crash the panel
            items.append(CheckItem(name, False, optional, f"Check failed: {exc}. {fix}"))
            return
        items.append(CheckItem(name, ok, optional, "" if ok else fix))

    add("Docker running", docker_running,
        "Start Docker Desktop and wait for 'Engine running'; verify with `docker info`")
    add("Sandbox image built", image_built, "`swarmforge sandbox build`")
    env_file = str(cfg.source_path or "swarmforge.yaml")
    for env in missing_api_keys(cfg):
        items.append(CheckItem(f"API key ({env})", False, False,
                                f"add `{env}=...` to the .env file next to {env_file} "
                                "and restart the dashboard"))
    if not missing_api_keys(cfg):
        items.append(CheckItem("API keys", True))
    add("GitHub login", github_logged_in,
        "`gh auth login` or set `GITHUB_TOKEN=...` in .env (only needed for draft pull requests)",
        optional=True)
    return items


def sample_repo_path() -> Path:
    return Path(SAMPLE_REPO_DEFAULT).expanduser()


def sample_issue_text() -> tuple[str, str]:
    path = Path(__file__).resolve().parents[2] / "samples" / "issues" / "export-csv-json.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "Sample issue", ""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            return line[2:].strip(), "\n".join(lines[i + 1:]).strip()
    return "Sample issue", text.strip()


def prepare_sample_repo(dest: Path | None = None) -> Path:
    dest = dest or sample_repo_path()
    if (dest / ".git").is_dir():
        return dest
    script = Path(__file__).resolve().parents[2] / "samples" / "make_sample_repo.py"
    result = subprocess.run([sys.executable, str(script), str(dest)],
                            check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"failed to prepare the sample repository: {result.stderr.strip()}")
    return dest


def start_sample_run(cfg: Config, store) -> str:
    dest = prepare_sample_repo()
    title, body = sample_issue_text()
    from . import launcher
    run_id, _ = launcher.start_run(cfg, repo=str(dest), issue_title=title, issue_body=body,
                                   mode="swarm", budget=SAMPLE_BUDGET_USD,
                                   max_cycles=SAMPLE_MAX_CYCLES, approval="dashboard")
    store.remember_repo(str(dest))
    return run_id
