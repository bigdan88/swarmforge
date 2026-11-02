import functools
import inspect
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from swarmforge.config import parse_config

# Signatures of *environmental* transient failures: fork/thread/PID exhaustion under parallel
# load (jemalloc "can't start new thread", EAGAIN on fork, BlockingIOError) and Chromium dying
# mid-test because the box is saturated. The heavy end-to-end/browser tests wrap themselves in
# `retry_transient` below so a saturated worker reruns the whole test instead of failing the
# suite; genuine regressions reproduce on every attempt and still fail. Skips are never retried.
TRANSIENT_FAILURE_SIGNS = (
    "resource temporarily unavailable",
    "can't start new thread",
    "unable to fork",
    "target page, context or browser has been closed",
    "browser has been closed",
    "did not finish within",
)


def _is_transient(exc: BaseException) -> bool:
    texts: list[str] = []

    def walk(e: BaseException | None, seen: set[int]) -> None:
        if e is None or id(e) in seen:
            return
        seen.add(id(e))
        texts.append(str(e))
        walk(e.__cause__, seen)
        walk(e.__context__, seen)
        for sub in getattr(e, "exceptions", ()) or ():  # ExceptionGroup
            walk(sub, seen)

    walk(exc, set())
    blob = " ".join(texts).lower()
    return any(sign in blob for sign in TRANSIENT_FAILURE_SIGNS)


def retry_transient(attempts: int = 3, delay_s: float = 2.0):
    """Decorator that reruns a test from scratch when it fails with a transient
    resource-exhaustion error (see TRANSIENT_FAILURE_SIGNS). Works on sync and async
    test functions; assertions themselves are never weakened — every attempt runs the
    full test body, and a real failure repeats on all attempts and still fails."""

    def deco(fn):
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                for i in range(attempts):
                    try:
                        return await fn(*args, **kwargs)
                    except BaseException as exc:  # noqa: BLE001 - filtered re-raise below
                        if not _is_transient(exc) or i == attempts - 1:
                            raise
                        time.sleep(delay_s)

            return awrapper

        @functools.wraps(fn)
        def swrapper(*args, **kwargs):
            for i in range(attempts):
                try:
                    return fn(*args, **kwargs)
                except BaseException as exc:  # noqa: BLE001 - filtered re-raise below
                    if not _is_transient(exc) or i == attempts - 1:
                        raise
                    time.sleep(delay_s)

        return swrapper

    return deco

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "inventory-app"
ISSUE = ROOT / "samples" / "issues" / "export-csv-json.md"


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
