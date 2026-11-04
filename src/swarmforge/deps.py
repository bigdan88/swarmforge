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
import json
import re
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


HELPER = r"""
# Written by SwarmForge into <worktree>/.swarmforge/ and run inside the sandbox.
# 1) If every declared requirement is already satisfied (by the image or a previous install) do nothing.
# 2) Otherwise pip-install into .swarmforge/pydeps, unpacking in an on-disk temp dir (the sandbox's
#    /tmp is a small RAM disk that big wheels like pyarrow overflow).
import json, os, shutil, subprocess, sys
here = os.path.dirname(os.path.abspath(__file__))
spec = json.load(open(os.path.join(here, "sf_deps.json")))
target = os.path.join(here, "pydeps")

def satisfied():
    if spec["req_files"] and not spec["file_reqs_parsed"]:
        return False
    try:
        from pip._vendor.packaging.requirements import Requirement
    except Exception:
        return False
    from importlib import metadata
    for line in spec["requirements"]:
        try:
            req = Requirement(line)
        except Exception:
            return False
        if req.marker is not None and not req.marker.evaluate():
            continue
        try:
            ver = metadata.version(req.name)
        except metadata.PackageNotFoundError:
            return False
        if req.specifier and not req.specifier.contains(ver, prereleases=True):
            return False
    return True

if satisfied():
    print("SWARMFORGE-DEPS: all requirements already satisfied; nothing to install")
    sys.exit(0)
tmp = os.path.join(here, "piptmp")
os.makedirs(tmp, exist_ok=True)
env = dict(os.environ, TMPDIR=tmp, TEMP=tmp, TMP=tmp)
args = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
        "--root-user-action=ignore", "--upgrade", "--target", target]
for f in spec["req_files"]:
    args += ["-r", f]
args += spec["pyproject_reqs"]
rc = subprocess.call(args, env=env)
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(rc)
"""

_SIMPLE_REQ = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[^\]]*\])?\s*([<>=!~].*)?(;.*)?$")


def _file_requirements(files: list[Path]) -> tuple[list[str], bool]:
    """Requirement lines from requirements files; False if some line can't be checked offline."""
    reqs, ok = [], True
    for f in files:
        for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.split(" #", 1)[0].strip()
            if not line or line.startswith("#"):
                continue
            if _SIMPLE_REQ.match(line):
                reqs.append(line)
            else:  # -r/-e/--options/paths/URLs: install to be safe
                ok = False
    return reqs, ok


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
    commands = ["python .swarmforge/sf_deps.py"] if files or pp else []
    manifest = ws / "package.json"
    lockfile = next((ws / name for name in ("npm-shrinkwrap.json", "package-lock.json")
                     if (ws / name).is_file()), None)
    other_manager = any((ws / name).is_file() for name in ("pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"))
    if manifest.is_file() and (lockfile or not other_manager):
        for path in (manifest, lockfile, ws / ".npmrc"):
            if path is not None and path.is_file():
                h.update(path.name.encode() + path.read_bytes())
                sources.append(path.name)
        npm = "npm ci" if lockfile else "npm install --no-package-lock"
        commands.append(f"{npm} --no-audit --no-fund --cache .swarmforge/npm-cache")
    cmd = install_command or (" && ".join(commands) if commands else None)
    return InstallPlan(cmd, sources, h.hexdigest()[:16])


def _write_helper(ws: Path) -> None:
    files = _req_files(ws)
    file_reqs, parsed = _file_requirements(files)
    d = ws / ".swarmforge"
    d.mkdir(parents=True, exist_ok=True)
    (d / "sf_deps.py").write_text(HELPER, encoding="utf-8")
    pp = _pyproject_deps(ws)
    (d / "sf_deps.json").write_text(json.dumps({
        "req_files": [f.relative_to(ws).as_posix() for f in files],
        "file_reqs_parsed": parsed,
        "requirements": file_reqs + pp,
        "pyproject_reqs": pp,
    }), encoding="utf-8")


def is_current(ws: Path, plan: InstallPlan) -> bool:
    stamp = ws / DEPS_DIR / STAMP
    return stamp.is_file() and stamp.read_text().strip() == plan.fingerprint


async def ensure_deps(sandbox: Sandbox, ws: Path, *, network: str = "bridge", timeout_s: int = 900,
                      cache_dir: Path | None = None, install_command: str | None = None,
                      force: bool = False) -> tuple[str, CommandResult | None, InstallPlan]:
    """Returns (status, result, plan); status in {"none", "current", "satisfied", "installed", "failed"}."""
    plan = plan_install(ws, install_command)
    if not plan.command:
        return "none", None, plan
    if not force and is_current(ws, plan):
        return "current", None, plan
    (ws / DEPS_DIR).mkdir(parents=True, exist_ok=True)
    if plan.command.split(" && ", 1)[0].endswith(".swarmforge/sf_deps.py"):
        _write_helper(ws)
    res = await sandbox.run(ws, plan.command, timeout_s=timeout_s, network=network, cache_dir=cache_dir)
    if res.exit_code == 0 and not res.timed_out:
        (ws / DEPS_DIR / STAMP).write_text(plan.fingerprint)
        if "SWARMFORGE-DEPS: all requirements already satisfied" in res.output:
            return "satisfied", res, plan
        return "installed", res, plan
    return "failed", res, plan
