import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from swarmforge.config import parse_config

# Keep the suite off the real user database. `from swarmforge import dashboard`
# opens its SQLite store at import time (module-level `store = get_store(db_path)`),
# and with no explicit path it falls back to ~/.swarmforge/swarmforge.db. Under
# pytest-xdist several worker processes then create and migrate that same fresh
# file concurrently and sqlite races ("duplicate column name: ...", "database is
# locked") surface as unrelated failures in test_theme/test_chat_sidebar/...
# Point every test process at its own throwaway database; this must happen at
# conftest import time, before test modules are imported during collection.
# Tests that want a specific database (dashboard subprocesses, AppTest runs)
# keep setting or clearing SWARMFORGE_DB themselves, which still works.
os.environ["SWARMFORGE_DB"] = str(
    Path(tempfile.mkdtemp(prefix=f"swarmforge-test-db-{os.getpid()}-"))
    / "swarmforge.db"
)

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "inventory-app"
ISSUE = ROOT / "samples" / "issues" / "export-csv-json.md"


def base_raw(workdir: Path, sandbox_mode: str = "local") -> dict:
    return {
        "providers": {
            "fake": {"base_url": "https://fake.test/v1", "api_key_env": "FAKE_KEY"}
        },
        "roles": {
            "planner": {"provider": "fake", "model": "qwen-fake", "max_steps": 15},
            "developer_backend": {
                "provider": "fake",
                "model": "deepseek-fake",
                "max_steps": 15,
            },
            "developer_frontend": {
                "provider": "fake",
                "model": "kimi-fake",
                "max_steps": 15,
            },
            "reviewer": {"provider": "fake", "model": "glm-fake", "max_steps": 10},
            "tester": {"provider": "fake", "model": "minimax-fake", "max_steps": 15},
        },
        "developer_roles": ["developer_backend", "developer_frontend"],
        "limits": {
            "budget_usd": 5,
            "max_runtime_s": 300,
            "max_concurrent_agents": 2,
            "max_repair_cycles": 2,
        },
        "sandbox": {"mode": sandbox_mode, "command_timeout_s": 120},
        "project": {
            "test_command": f"{Path(sys.executable).as_posix()} -m pytest -q -p no:cacheprovider"
        },
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
    shutil.copytree(
        SAMPLE, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache")
    )
    for a in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", *a], cwd=dest, check=True, capture_output=True)
    return dest


# ------------------------------------------------- cross-process browser slot
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

_BROWSER_LOCK_PATH = Path(tempfile.gettempdir()) / "swarmforge-browser.lock"
_BROWSER_LOCK_TIMEOUT_S = 600.0


@contextmanager
def _noop_slot():
    yield


@contextmanager
def _flock_slot():
    """Hold an exclusive cross-process advisory lock on a shared lockfile.

    This replaces the removed xdist_group workaround: it serialises real
    Chromium/dashboard launches across xdist workers (and any other pytest
    processes on the box) without pinning tests to one worker.
    """
    with open(_BROWSER_LOCK_PATH, "a+") as fh:
        try:
            deadline = time.monotonic() + _BROWSER_LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"timed out waiting for the browser slot lock at "
                            f"{_BROWSER_LOCK_PATH}"
                        )
                    time.sleep(0.5)
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@pytest.fixture
def browser_slot():
    """Factory returning a context manager that serialises browser launches.

    Real-browser tests wrap their launch window in `with browser_slot():` so at
    most one Chromium/dashboard runs at any moment, even under pytest-xdist.
    On platforms without fcntl (Windows) this is a no-op.
    """
    if fcntl is None:
        return _noop_slot
    return _flock_slot


# ------------------------------------------------------------- fake model API
def tool_call(name: str, args: dict, cid: str = "c1") -> dict:
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def completion(
    message: dict, prompt_tokens=100, completion_tokens=20, cost=0.0001
) -> dict:
    return {
        "choices": [
            {"message": {"role": "assistant", **message}, "finish_reason": "tool_calls"}
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "estimated_cost": cost,
        },
    }


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
    for k in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        monkeypatch.delenv(k, raising=False)
