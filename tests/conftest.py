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


_STALE_LOCK_S = 2 * 60 * 60  # locks older than 2h are considered breakable


def _break_stale_lock(lock_path: Path) -> bool:
    """Best-effort removal of a dead/stale lock file. Race-tolerant."""
    try:
        age_s = time.time() - lock_path.stat().st_mtime
    except OSError:
        return False  # gone already
    if age_s < _STALE_LOCK_S:
        return False
    try:
        lock_path.unlink()
    except OSError:
        return False
    return True


@contextmanager
def _file_mutex_slot(path, timeout_s=_BROWSER_LOCK_TIMEOUT_S):
    """Cross-platform lockfile mutex (Windows fallback for the flock slot).

    Uses an O_EXCL lock file with polling + timeout. The owning PID is written
    into the lock file; locks older than 2h are treated as stale and broken.
    Only guards a test resource, so this is deliberately simple and
    race-tolerant rather than bulletproof.
    """
    lock_path = Path(str(path) + ".mutex")
    deadline = time.monotonic() + timeout_s
    fd = None
    try:
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                break
            except FileExistsError:
                if _break_stale_lock(lock_path):
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out waiting for the browser slot lock at {lock_path}"
                    )
                time.sleep(0.2)
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            lock_path.unlink()
        except OSError:
            pass


def _browser_slot_lock(path, timeout_s=_BROWSER_LOCK_TIMEOUT_S):
    """Select the platform lock: flock on POSIX, file mutex otherwise."""
    if fcntl is None:
        return _file_mutex_slot(path, timeout_s)
    return _flock_slot()


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
    On platforms without fcntl (Windows) this falls back to a stdlib file
    mutex (O_EXCL lock file with polling + timeout).
    """
    return lambda: _browser_slot_lock(_BROWSER_LOCK_PATH, _BROWSER_LOCK_TIMEOUT_S)


# ------------------------------------------- cross-process AppTest slot cap
_APPTEST_STALE_LOCK_S = 900.0  # slot files older than 15min are stale


@contextmanager
def _counting_slot_lock(root: Path, name: str, slots: int, timeout_s: float):
    """Cross-process counting lock: at most `slots` concurrent holders.

    Mirrors `_browser_lock`'s file-based approach but with N slots instead of
    one: the directory `root/name` holds `slot-0.lock` ... `slot-(slots-1).lock`
    and a holder owns exactly one of them. Used to cap concurrent Streamlit
    AppTest renders across pytest-xdist workers (empirically flaky above 4).

    FAIL-OPEN: if no slot frees up before `timeout_s`, the context is entered
    WITHOUT holding any lock — the cap must never itself fail a test.
    """
    lock_dir: Path | None
    try:
        lock_dir = Path(root) / name
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        lock_dir = None  # unusable lock dir: fail open below
    held: Path | None = None
    if lock_dir is not None:
        deadline = time.monotonic() + timeout_s
        while held is None:
            for i in range(slots):
                slot_path = lock_dir / f"slot-{i}.lock"
                try:
                    if (
                        time.time() - slot_path.stat().st_mtime
                        > _APPTEST_STALE_LOCK_S
                    ):
                        slot_path.unlink()  # stale lock from a crashed worker
                except OSError:
                    pass
                try:
                    fd = os.open(
                        str(slot_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                    )
                except FileExistsError:
                    continue
                except OSError:
                    # another holder may have just rmdir'd the lock dir while
                    # it drained; recreate it and keep trying until the deadline
                    try:
                        lock_dir.mkdir(parents=True, exist_ok=True)
                    except OSError:
                        pass
                    continue
                try:
                    os.write(fd, str(os.getpid()).encode())
                finally:
                    os.close(fd)
                held = slot_path
                break
            if held is None:
                if time.monotonic() >= deadline:
                    break  # fail open: enter without a slot
                time.sleep(0.25)
    try:
        yield
    finally:
        if held is not None:
            try:
                held.unlink()
            except OSError:
                pass
            if lock_dir is not None:
                try:
                    lock_dir.rmdir()  # best-effort cleanup when empty
                except OSError:
                    pass


def _slot_lock(root, name=None, slots=None, timeout_s=240.0):
    """Dispatch between the two slot-lock helpers.

    New callers use the counting form: `_slot_lock(root, name, slots, timeout_s)`.
    Legacy callers (the browser_slot fixture, tests/test_browser_slot.py) use
    the selector form `_slot_lock(path, timeout_s=...)`, kept for backwards
    compatibility.
    """
    if name is None:
        return _browser_slot_lock(root, timeout_s)
    return _counting_slot_lock(root, name, slots or 1, timeout_s)


def pytest_configure(config):
    """Cap concurrent Streamlit AppTest renders across pytest-xdist workers.

    Streamlit AppTest renders of the ~2500-line dashboard flake under high
    pytest-xdist parallelism (CPU contention). Patching AppTest.run with a
    4-slot cross-process lock keeps concurrent renders at the empirically
    known-good level even when the suite runs with -n 6. Real-browser tests
    use a different, single-slot lock directory, so no deadlock is possible.
    """
    try:
        from streamlit.testing.v1 import AppTest

        _orig_run = AppTest.run

        def _capped_run(self, *args, **kwargs):
            with _slot_lock(
                Path(tempfile.gettempdir()), "sf-apptest-slots", 4, timeout_s=240.0
            ):
                return _orig_run(self, *args, **kwargs)

        AppTest.run = _capped_run
    except Exception:  # noqa: BLE001, S110 - streamlit absent: suite must collect
        pass


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
