"""Project dependency installation for sandboxes.

Agent commands and test runs have no network. Projects still need their third-party packages, so
SwarmForge runs a separate, explicit install step that DOES have network access:

* it runs in the same locked-down container (non-root, cap-drop ALL, read-only root fs, only the
  worktree mounted, no credentials), with network enabled only for that one pip command;
* packages are installed into `<worktree>/.swarmforge/pydeps` (git-excluded), which every later
  sandbox command sees via PYTHONPATH;
* a fingerprint of the dependency files skips reinstalls when nothing changed;
* a shared pip cache under the SwarmForge workdir makes repeat installs fast.

Sources, in order: `install_command` from swarmforge.project.yml; requirements*.txt /
dev-requirements.txt in the repo root; [project] dependencies + optional-dependencies (and PEP 735
[dependency-groups]) from pyproject.toml.
"""
from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None

from .sandbox import DEPS_CONTAINER_PATH, DEPS_DIR, CommandResult, Sandbox

STAMP = ".stamp"


@dataclass
class InstallPlan:
    command: str | None
    sources: list[str]
    fingerprint: str


def _req_files(ws: Path) -> list[Path]:
    files = sorted(p for p in ws.glob("requirements*.txt") if p.is_file())
    files += [p for p in (ws / "dev-requirements.txt", ws / "requirements" / "dev.txt",
                          ws / "requirements" / "base.txt") if p.is_file()]
    return files


def _pyproject_deps(ws: Path) -> list[str]:
    f = ws / "pyproject.toml"
    if not f.is_file() or tomllib is None:
        return []
    try:
        data = tomllib.loads(f.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []
    proj = data.get("project") or {}
    deps = list(proj.get("dependencies") or [])
    for group in (proj.get("optional-dependencies") or {}).values():
        deps += list(group or [])
    for group in (data.get("dependency-groups") or {}).values():
        deps += [d for d in (group or []) if isinstance(d, str)]
    return sorted({d for d in deps if isinstance(d, str) and d.strip()})


def plan_install(ws: Path, install_command: str | None = None) -> InstallPlan:
    h = hashlib.sha256()
    sources: list[str] = []
    if install_command:
        h.update(install_command.encode())
        sources.append("swarmforge.project.yml install_command")
    files = _req_files(ws)
    for f in files:
        h.update(f.name.encode() + f.read_bytes())
        sources.append(f.relative_to(ws).as_posix())
    pp = _pyproject_deps(ws)
    if pp:
        h.update("\n".join(pp).encode())
        sources.append("pyproject.toml")
    if install_command:
        cmd = install_command
    elif files or pp:
        args = []
        for f in files:
            args += ["-r", shlex.quote(f.relative_to(ws).as_posix())]
        args += [shlex.quote(d) for d in pp]
        cmd = ("python -m pip install --disable-pip-version-check --no-warn-script-location --root-user-action=ignore "
               f"--upgrade --target {DEPS_CONTAINER_PATH} " + " ".join(args))
    else:
        cmd = None
    return InstallPlan(cmd, sources, h.hexdigest()[:16])


def is_current(ws: Path, plan: InstallPlan) -> bool:
    stamp = ws / DEPS_DIR / STAMP
    return stamp.is_file() and stamp.read_text().strip() == plan.fingerprint


async def ensure_deps(sandbox: Sandbox, ws: Path, *, network: str = "bridge", timeout_s: int = 900,
                      cache_dir: Path | None = None, install_command: str | None = None,
                      force: bool = False) -> tuple[str, CommandResult | None, InstallPlan]:
    """Returns (status, result, plan); status in {"none", "current", "installed", "failed"}."""
    plan = plan_install(ws, install_command)
    if not plan.command:
        return "none", None, plan
    if not force and is_current(ws, plan):
        return "current", None, plan
    (ws / DEPS_DIR).mkdir(parents=True, exist_ok=True)
    res = await sandbox.run(ws, plan.command, timeout_s=timeout_s, network=network, cache_dir=cache_dir)
    if res.exit_code == 0 and not res.timed_out:
        (ws / DEPS_DIR / STAMP).write_text(plan.fingerprint)
        return "installed", res, plan
    return "failed", res, plan
