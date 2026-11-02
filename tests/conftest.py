import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from swarmforge.config import parse_config

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "inventory-app"
ISSUE = ROOT / "samples" / "issues" / "export-csv-json.md"

# ------------------------------------------------- one real browser at a time
# Chromium instances do not cope with starting side by side in the small sandbox the suite runs in
# (/dev/shm is 64 MB here): with `pytest -n 6` several workers launch a browser at once and Chromium
# dies mid-test with "BrowserType.launch: Target page, context or browser has been closed" or
# "Target crashed" - failures that have nothing to do with the code under test (this bit the theme
# E2E test and the visual-review tests). Tests tagged `@pytest.mark.browser` therefore hold an
# exclusive *cross-process* lock, so xdist workers take turns instead of racing.
BROWSER_LOCK = Path(os.environ.get("SWARMFORGE_BROWSER_LOCK") or
                    Path(tempfile.gettempdir()) / "swarmforge-tests" / "browser.lock")

try:
    import fcntl
except ImportError:  # pragma: no cover - platforms without flock (Windows dev boxes have no such limit)
    fcntl = None  # type: ignore[assignment]

_HELD: set[str] = set()  # lock paths this process already holds (browser_lock is re-entrant)


@contextlib.contextmanager
def browser_lock(timeout_s: float = 600.0):
    """Wait for the shared browser lock, run the body, release it.

    Never breaks the suite: it is re-entrant within one process (a marked test may call a helper that
    locks again), it gives up waiting after `timeout_s` and runs the body unlocked, and if the lock
    file cannot be created at all (read-only temp dir) it just runs unlocked. All three are strictly
    better than the alternative - Chromium instances started side by side."""
    path = str(BROWSER_LOCK)
    if fcntl is None or path in _HELD:
        yield
        return
    try:
        BROWSER_LOCK.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        yield
        return
    _HELD.add(path)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - we never got the lock
            pass
        os.close(fd)
        _HELD.discard(path)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Serialise real-browser tests across pytest/xdist worker processes."""
    if item.get_closest_marker("browser") is None:
        return (yield)
    with browser_lock():
        return (yield)


def base_raw(workdir: Path, sandbox_mode: str = "local") -> dict:
    return {
        "providers": {"fake": {"base_url": "https://fake.test/v1", "api_key_env": "FAKE_KEY"}},
        "roles": {
            "planner": {"provider": "fake", "model": "qwen-fake", "max_steps": 15},
            "developer_backend": {"provider": "fake", "model": "deepseek-fake", "max_steps": 15},
            "developer_frontend": {"provider": "fake", "model": "kimi-fake", "max_steps": 15},
            "reviewer": {"provider": "fake", "model": "glm-fake", "max_steps": 10},
            "tester": {"provider": "fake", "model": "minimax-fake", "max_steps": 15},
        },
        "developer_roles": ["developer_backend", "developer_frontend"],
        "limits": {"budget_usd": 5, "max_runtime_s": 300, "max_concurrent_agents": 2,
                   "max_repair_cycles": 2},
        "sandbox": {"mode": sandbox_mode, "command_timeout_s": 120},
        "project": {"test_command": f"{Path(sys.executable).as_posix()} -m pytest -q -p no:cacheprovider"},
        "workdir": str(workdir),
        "knowledge": {"vault": str(workdir / "knowledge")},
        "self_heal": {"max_attempts": 0},  # tests that need it turn it on
    }


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    return parse_config(base_raw(tmp_path / "work"))


@pytest.fixture
def sample_repo(tmp_path) -> Path:
    dest = tmp_path / "sample"
    shutil.copytree(SAMPLE, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    for a in (["init", "-q", "-b", "main"], ["add", "-A"],
              ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", *a], cwd=dest, check=True, capture_output=True)
    return dest


# ------------------------------------------------------------- fake model API
def tool_call(name: str, args: dict, cid: str = "c1") -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def completion(message: dict, prompt_tokens=100, completion_tokens=20, cost=0.0001) -> dict:
    return {"choices": [{"message": {"role": "assistant", **message}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "total_tokens": prompt_tokens + completion_tokens, "estimated_cost": cost}}


class FakeProvider:
    """Routes chat requests to per-role scripted handlers: handler(body) -> message dict."""

    def __init__(self, handlers):
        self.handlers = handlers
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        msg = self.handlers[body["model"]](body)
        return httpx.Response(200, json=completion(msg))

    @property
    def transport(self):
        return httpx.MockTransport(self)


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch):
    """Dashboards rendered in tests must not contact GitHub to look for SwarmForge updates."""
    monkeypatch.setenv("SWARMFORGE_DASH_UPDATE_CHECK", "0")


@pytest.fixture(autouse=True)
def _clean_git_env(monkeypatch):
    """Never let a host run's GIT_* environment leak into test git calls."""
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
              "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        monkeypatch.delenv(k, raising=False)
