"""Update SwarmForge to the latest code (everything merged on GitHub) and restart it.

* `status()` fetches and reports how far the local checkout is behind / ahead, what's new, and
  whether local edits would get in the way.
* `update()` pulls (fast-forward, or a normal merge when you have local commits), installs any new
  third-party dependencies, and tells you if the sandbox images need rebuilding.
* `restart_dashboard()` hands over to a small helper process that stops this dashboard and starts a
  new one with the same options (the browser tab reconnects by itself).
* `request_worker_restart()` asks the queue worker to start nothing new, and to restart itself with
  the new code as soon as its running tasks finish.
SwarmForge never discards your work: uncommitted changes to tracked files stop the update.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # the SwarmForge checkout (editable install)
RESTART_KEY = "queue_worker_restart"
IMAGE_FILES = ("src/swarmforge/resources/Dockerfile.sandbox", "src/swarmforge/resources/Dockerfile.dev-sandbox")


class UpdateError(RuntimeError):
    pass


def _git(root: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if check and p.returncode != 0:
        raise UpdateError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()[:600]}")
    return p.stdout


def status(root: Path = ROOT, fetch: bool = True) -> dict:
    if not (root / ".git").exists():
        return {"ok": False, "error": f"{root} is not a git checkout; update it the way you installed it"}
    out = {"ok": True, "root": str(root), "error": None}
    out["branch"] = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    upstream = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False).strip()
    out["upstream"] = upstream or None
    if fetch and upstream:
        try:
            _git(root, "-c", "maintenance.auto=false", "-c", "gc.auto=0", "fetch", "--quiet",
                 upstream.split("/")[0])
        except UpdateError as e:
            out["error"] = f"could not check GitHub: {e}"
    out["head"] = _git(root, "rev-parse", "--short", "HEAD").strip()
    behind = ahead = 0
    incoming: list[str] = []
    if upstream:
        counts = _git(root, "rev-list", "--left-right", "--count", f"HEAD...{upstream}", check=False).split()
        if len(counts) == 2:
            ahead, behind = int(counts[0]), int(counts[1])
        incoming = [ln for ln in _git(root, "log", "--format=%h %s", f"HEAD..{upstream}", "-30",
                                      check=False).splitlines() if ln]
    out.update(behind=behind, ahead=ahead, incoming=incoming)
    out["dirty"] = [ln[3:] for ln in _git(root, "status", "--porcelain", "--untracked-files=no").splitlines() if ln]
    return out


def _deps_changed(root: Path, old: str, new: str) -> bool:
    return bool(_git(root, "diff", "--name-only", old, new, "--", "pyproject.toml", check=False).strip())


def _install_deps(root: Path) -> str:
    """Install the (possibly new) third-party dependencies WITHOUT reinstalling SwarmForge itself
    (it's an editable install, so code changes apply already; on Windows the running swarmforge.exe
    can't be replaced)."""
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python < 3.11
        return "skipped (Python too old to read pyproject.toml)"
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    proj = data.get("project", {})
    reqs = list(proj.get("dependencies") or [])
    for extra in ("dashboard", "dev"):
        reqs += list((proj.get("optional-dependencies") or {}).get(extra) or [])
    if not reqs:
        return "no dependencies"
    p = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", *reqs], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise UpdateError(f"installing dependencies failed: {(p.stderr or p.stdout).strip()[-800:]}")
    return "dependencies up to date"


def update(root: Path = ROOT, install=_install_deps) -> dict:
    st = status(root)
    if not st["ok"]:
        raise UpdateError(st["error"])
    if not st["upstream"]:
        raise UpdateError(f"branch '{st['branch']}' doesn't track a GitHub branch; nothing to update from")
    if st["dirty"]:
        raise UpdateError("you have uncommitted changes, so nothing was updated (your work is untouched). "
                          "Commit them first: " + ", ".join(st["dirty"][:10]))
    if st["error"]:
        raise UpdateError(st["error"])
    old = _git(root, "rev-parse", "HEAD").strip()
    result = {"old": old[:8], "behind": st["behind"], "ahead": st["ahead"], "commits": st["incoming"],
              "deps": None, "images_stale": False, "merged": False}
    if st["behind"] == 0:
        result.update(new=old[:8], message="Already up to date.")
        return result
    p = subprocess.run(["git", "merge", "--ff-only", st["upstream"]], cwd=str(root), capture_output=True, text=True)
    if p.returncode != 0:                       # you have local commits: merge like `git pull` would
        p = subprocess.run(["git", "merge", "--no-edit", st["upstream"]], cwd=str(root), capture_output=True,
                           text=True)
        if p.returncode != 0:
            subprocess.run(["git", "merge", "--abort"], cwd=str(root), capture_output=True)
            raise UpdateError("the new code conflicts with your local commits; nothing was changed. "
                              f"Run `git pull` in {root} and resolve the conflicts. ({(p.stdout + p.stderr)[-400:]})")
        result["merged"] = True
    new = _git(root, "rev-parse", "HEAD").strip()
    result["new"] = new[:8]
    if _deps_changed(root, old, new):
        result["deps"] = install(root)
    changed = _git(root, "diff", "--name-only", old, new, "--", *IMAGE_FILES, check=False).strip()
    result["images_stale"] = bool(changed)
    result["message"] = f"Updated {old[:8]} → {new[:8]} ({st['behind']} new commit(s))."
    return result


# ---------------------------------------------------------------- restarts
def dashboard_args() -> dict:
    """The options this dashboard was started with (set by `swarmforge dashboard`)."""
    try:
        return json.loads(os.environ.get("SWARMFORGE_DASH_ARGS") or "{}")
    except json.JSONDecodeError:
        return {}


def restart_dashboard(config_path: str | None, log_dir: Path, pid: int | None = None) -> int:
    """Start a detached helper that stops this dashboard (pid) and launches a fresh one."""
    pid = pid or os.getpid()
    log_dir.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, "-m", "swarmforge.cli"] + (["-c", config_path] if config_path else []) + [
        "restart-dashboard", "--pid", str(pid), "--args", json.dumps(dashboard_args())]
    kw: dict = {"stdin": subprocess.DEVNULL, "cwd": str(Path(config_path).parent) if config_path else None}
    log = open(log_dir / "dashboard-restart.log", "a", encoding="utf-8")
    kw.update(stdout=log, stderr=subprocess.STDOUT)
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen(args, **kw)
    log.close()
    return p.pid


def _kill(pid: int) -> None:
    if sys.platform == "win32":
        # NOT /T: this helper was started by the dashboard process, so killing the dashboard's process
        # tree would kill the helper too, and nothing would start the new dashboard
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _port_free(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def run_restart(config_path: str | None, old_pid: int, opts: dict, *, launch=None, sleep=time.sleep) -> int:
    """Body of `swarmforge restart-dashboard`: wait a moment (so the page can show the message), stop
    the old dashboard, wait for its port, start a new one with the same options (and check it's up)."""
    from .launcher import pid_alive
    print(f"{time.strftime('%H:%M:%S')} restart requested: stopping dashboard pid {old_pid}", flush=True)
    sleep(2.0)
    _kill(old_pid)
    for _ in range(60):
        if not pid_alive(old_pid):
            break
        sleep(0.5)
    port = int(opts.get("port") or 8501)
    for _ in range(60):
        if _port_free(port):
            break
        sleep(0.5)
    cmd = [sys.executable, "-m", "swarmforge.cli"] + (["-c", config_path] if config_path else []) + [
        "dashboard", "--port", str(port), "--no-browser", "--theme", str(opts.get("theme") or "auto")]
    if opts.get("lan"):
        cmd.append("--lan")
    if launch:
        return launch(cmd)
    kw: dict = {"stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kw["start_new_session"] = True
    for attempt in (1, 2):
        print(f"{time.strftime('%H:%M:%S')} starting dashboard (attempt {attempt}): {' '.join(cmd)}", flush=True)
        proc = subprocess.Popen(cmd, **kw)
        for _ in range(90):                      # wait up to ~45 s for it to answer on its port
            sleep(0.5)
            if not _port_free(port):
                print(f"{time.strftime('%H:%M:%S')} dashboard is up on port {port} (pid {proc.pid})", flush=True)
                return proc.pid
            if proc.poll() is not None:
                print(f"{time.strftime('%H:%M:%S')} dashboard exited with code {proc.returncode}", flush=True)
                break
    print(f"{time.strftime('%H:%M:%S')} could not start the dashboard; run `swarmforge dashboard` yourself",
          flush=True)
    return 0


def request_worker_restart(store) -> str:
    """Ask the queue worker to restart with the new code once its running tasks are done."""
    from .runqueue import worker_pid
    if not worker_pid(store):
        return "The queue worker isn't running; it uses the new code the next time it starts."
    store.kv_set(RESTART_KEY, "1")
    running = sum(1 for i in store.queue_items(include_finished=False) if i["status"] == "running")
    return ("The queue worker will restart with the new code right away." if not running else
            f"The queue worker starts nothing new and restarts with the new code when its {running} running "
            "task(s) finish.")


def own_repo(root: Path = ROOT) -> str | None:
    """owner/name of the GitHub repository this SwarmForge checkout comes from."""
    import re
    try:
        url = _git(root, "remote", "get-url", "origin").strip()
    except UpdateError:
        return None
    m = re.search(r"github\.com[/:]([^/\s]+)/([^/\s]+?)(?:\.git)?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None
