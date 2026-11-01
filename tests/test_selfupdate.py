"""Update SwarmForge from GitHub and restart: pull, deps, image notice, refusal on local edits,
restart helper, queue worker restart."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from swarmforge import runqueue as rq
from swarmforge import selfupdate as su
from swarmforge.db import Store


def g(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    g(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    g(tmp_path, "clone", "-q", str(origin), str(seed))
    (seed / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = []\n')
    (seed / "app.py").write_text("v = 1\n")
    g(seed, "add", "-A")
    g(seed, "commit", "-qm", "init")
    g(seed, "push", "-q", "origin", "HEAD:main")
    mine = tmp_path / "mine"                      # the user's SwarmForge checkout
    g(tmp_path, "clone", "-q", str(origin), str(mine))
    g(mine, "config", "user.name", "Dan")
    g(mine, "config", "user.email", "dan@example.com")
    return seed, mine


def merged_pr(seed, path, text, msg="PR merged"):
    p = seed / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    g(seed, "add", "-A")
    g(seed, "commit", "-qm", msg)
    g(seed, "push", "-q", "origin", "HEAD:main")


def test_status_and_fast_forward_update(repos):
    seed, mine = repos
    merged_pr(seed, "app.py", "v = 2\n", "Dark mode (#5)")
    s = su.status(mine)
    assert s["ok"] and s["behind"] == 1 and s["ahead"] == 0 and "Dark mode (#5)" in s["incoming"][0]
    res = su.update(mine, install=lambda r: pytest.fail("no dependency change"))
    assert (mine / "app.py").read_text() == "v = 2\n" and res["behind"] == 1 and not res["images_stale"]
    assert su.update(mine)["message"] == "Already up to date."


def test_dependencies_and_images_are_reported(repos):
    seed, mine = repos
    merged_pr(seed, "pyproject.toml", '[project]\nname = "x"\ndependencies = ["rich"]\n')
    merged_pr(seed, "src/swarmforge/resources/Dockerfile.sandbox", "FROM python\n")
    installed = []
    res = su.update(mine, install=lambda r: installed.append(r) or "installed")
    assert installed == [mine] and res["deps"] == "installed" and res["images_stale"]


def test_local_edits_stop_the_update_and_are_untouched(repos):
    seed, mine = repos
    merged_pr(seed, "app.py", "v = 2\n")
    (mine / "app.py").write_text("v = 99  # my edit\n")
    with pytest.raises(su.UpdateError, match="uncommitted changes.*app.py"):
        su.update(mine)
    assert (mine / "app.py").read_text() == "v = 99  # my edit\n"


def test_local_commits_are_merged_and_conflicts_leave_everything_as_it_was(repos):
    seed, mine = repos
    merged_pr(seed, "other.py", "x = 1\n")
    (mine / "notes.txt").write_text("mine\n")
    g(mine, "add", "-A")
    g(mine, "commit", "-qm", "local work")
    res = su.update(mine)
    assert res["merged"] and (mine / "other.py").exists() and (mine / "notes.txt").exists()
    merged_pr(seed, "app.py", "v = 3\n")
    (mine / "app.py").write_text("v = 4\n")
    g(mine, "add", "-A")
    g(mine, "commit", "-qm", "conflicting local work")
    head = g(mine, "rev-parse", "HEAD")
    with pytest.raises(su.UpdateError, match="conflicts with your local commits"):
        su.update(mine)
    assert g(mine, "rev-parse", "HEAD") == head and g(mine, "status", "--porcelain") == ""


def test_restart_stops_the_old_dashboard_and_starts_one_with_the_same_options(tmp_path):
    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    launched = []
    su.run_restart("C:/cfg/swarmforge.yaml", old.pid, {"port": 8601, "theme": "dark", "lan": True},
                   launch=launched.append, sleep=lambda s: time.sleep(min(s, 0.05)))
    assert old.wait(timeout=10) is not None                       # the old dashboard was stopped
    cmd = launched[0]
    assert cmd[cmd.index("--port") + 1] == "8601" and cmd[cmd.index("--theme") + 1] == "dark" and "--lan" in cmd
    assert "-c" in cmd and "dashboard" in cmd and "--no-browser" in cmd


async def test_worker_finishes_then_restarts_with_new_code(cfg, monkeypatch):
    st = Store(cfg.db_path)
    st.queue_add("Next", "b", "repo")
    st.kv_set(su.RESTART_KEY, "1")
    started = []
    monkeypatch.setattr(rq, "start_worker", lambda c, s, keep_pause=False: started.append(keep_pause) or 1)
    monkeypatch.setattr(rq, "run_item", lambda *a, **k: pytest.fail("must not start new work before restarting"))
    assert await rq.work(cfg, st, echo=None, poll_s=0.01) == 0
    assert started == [True] and st.kv_get(su.RESTART_KEY) is None
    assert su.request_worker_restart(st).startswith("The queue worker isn't running")


def test_windows_kill_spares_the_helper(monkeypatch):
    """taskkill /T would also kill the restart helper (a child of the dashboard), so nothing restarts."""
    calls = []
    monkeypatch.setattr(su.sys, "platform", "win32")
    monkeypatch.setattr(su.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    su._kill(1234)
    assert calls == [["taskkill", "/F", "/PID", "1234"]] and "/T" not in calls[0]
