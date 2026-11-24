"""Docker Desktop not started after a reboot: SwarmForge starts it, and paused lanes resume by themselves."""
import asyncio

import pytest

from swarmforge import preflight, runqueue
from swarmforge.db import Store


@pytest.fixture
def store(cfg):
    s = Store(cfg.db_path)
    yield s
    s.close()


def test_docker_desktop_is_started_once_and_waited_for(monkeypatch):
    calls, answers = [], iter([False, False, True])
    monkeypatch.setattr(preflight, "_DESKTOP_TRIED_AT", 0.0)
    monkeypatch.setattr(preflight, "docker_desktop_path", lambda: r"C:\Program Files\Docker\Docker\Docker Desktop.exe")
    monkeypatch.setattr(preflight.subprocess, "Popen", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(preflight, "_docker_answers", lambda d, t: (next(answers), []))
    monkeypatch.delenv("SWARMFORGE_NO_DOCKER_AUTOSTART", raising=False)
    assert preflight.start_docker_desktop("docker", sleep=lambda s: None)
    assert len(calls) == 1
    assert not preflight.start_docker_desktop("docker", sleep=lambda s: None)  # not again within ten minutes
    assert len(calls) == 1


def test_no_autostart_without_docker_desktop_or_when_turned_off(monkeypatch):
    monkeypatch.setattr(preflight, "_DESKTOP_TRIED_AT", 0.0)
    monkeypatch.setattr(preflight, "docker_desktop_path", lambda: None)
    assert not preflight.start_docker_desktop("docker", sleep=lambda s: None)
    monkeypatch.setattr(preflight, "docker_desktop_path", lambda: "/Applications/Docker.app")
    monkeypatch.setenv("SWARMFORGE_NO_DOCKER_AUTOSTART", "1")
    assert not preflight.start_docker_desktop("docker", sleep=lambda s: None)


def test_a_lane_paused_by_the_environment_resumes_when_it_is_fixed_but_yours_stays_paused(cfg, store):
    env = store.queue_add("Horse overlay", "body", "o/horse-tinder", queue="project-h1")
    mine = store.queue_add("Other", "body", "o/other", queue="mine")
    store.queue_update(env, note=f"{runqueue.ENV_PAUSE_NOTE}: Docker is not responding")
    runqueue.set_lane_paused(store, "project-h1", True)
    runqueue.set_lane_paused(store, "mine", True)  # paused by you
    assert runqueue.environment_paused_lanes(store) == ["project-h1"]
    assert asyncio.run(runqueue.resume_environment_paused(store, cfg, check=lambda c: ["Docker is not responding"])) == []
    assert runqueue.lane_paused(store, "project-h1")
    assert asyncio.run(runqueue.resume_environment_paused(store, cfg, check=lambda c: [])) == ["project-h1"]
    assert not runqueue.lane_paused(store, "project-h1") and runqueue.lane_paused(store, "mine")
    assert store.queue_item(env)["note"] == "environment fixed; resumed automatically"
    assert store.queue_item(mine)["status"] == "queued"


def test_web_build_check_detects_stale_and_current(tmp_path, monkeypatch):
    from swarmforge.webapp import frontend_build as fb
    front, web = tmp_path / "frontend", tmp_path / "web"
    (front / "src").mkdir(parents=True)
    (front / "package.json").write_text("{}")
    (front / "src" / "App.tsx").write_bytes(b"one\r\n")
    web.mkdir()
    (web / "index.html").write_text("<html>")
    monkeypatch.setattr(fb, "FRONTEND", front)
    monkeypatch.setattr(fb, "WEB", web)
    monkeypatch.setattr(fb, "STAMP", web / ".source-sha256")
    assert fb.status()[0] == "stale"
    (web / ".source-sha256").write_text(fb.source_hash(front))
    assert fb.status()[0] == "current"
    (front / "src" / "App.tsx").write_bytes(b"one\n")  # same content, other line endings: still current
    assert fb.status()[0] == "current"
    (front / "src" / "App.tsx").write_bytes(b"two\n")
    assert fb.status()[0] == "stale" and fb.health_row()["status"] == "fail"
    monkeypatch.setattr(fb, "_npm", lambda: None)
    assert fb.ensure_current(echo=lambda m: None) == "stale"  # says so instead of serving silently


def test_resuming_a_live_worker_clears_the_pause_without_starting_another(cfg, store, monkeypatch):
    store.kv_set(runqueue.PAUSED_KEY, "1")
    monkeypatch.setattr(runqueue, "worker_pid", lambda s: 1234)
    assert runqueue.start_worker(cfg, store) == 1234
    assert store.kv_get(runqueue.PAUSED_KEY) == "0"
    store.kv_set(runqueue.PAUSED_KEY, "1")
    assert runqueue.start_worker(cfg, store, keep_pause=True) == 1234
    assert store.kv_get(runqueue.PAUSED_KEY) == "1"


async def test_preflight_retry_has_no_missing_checkout_seed_and_keeps_its_allowance(cfg, store, monkeypatch):
    from swarmforge import orchestrator, postmortem
    from swarmforge.execution import ensure_allowance

    store.create_run("old-preflight", state="BLOCKED", run_dir=str(cfg.workdir / "missing"))
    store.event("old-preflight", "environment", "Docker is not responding", level="error")
    eid = ensure_allowance(store, "old-preflight", cfg)
    before = store._one("SELECT budget_usd,deadline FROM execution_allowances WHERE id=?", (eid,))
    item = store.queue_add("Retry preflight", "body", "owner/app", seed_run_id="old-preflight", execution_id=eid)
    seen = []

    class FakeOrchestrator:
        def __init__(self, config, s, **kwargs):
            seen.append(kwargs.get("seed_run_id"))
            self.execution_id = kwargs["execution_id"]

        async def execute(self, **kwargs):
            rid = kwargs["run_id"]
            store.create_run(rid, state="BLOCKED", run_dir=str(cfg.workdir / rid), execution_id=self.execution_id)
            store.event(rid, "environment", "Docker is still unavailable", level="error")
            return rid

    async def no_heal(config, s, rid, *args, **kwargs):
        return rid

    monkeypatch.setattr(orchestrator, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(postmortem, "heal", no_heal)
    await runqueue.run_item(cfg, store, store.queue_item(item), echo=None)
    assert seen == [None]
    assert store.queue_item(item)["status"] == "queued"
    assert store.queue_item(item)["seed_run_id"] is None
    assert store.queue_item(item)["execution_id"] == eid
    assert store._one("SELECT budget_usd,deadline FROM execution_allowances WHERE id=?", (eid,)) == before


def test_a_checkout_with_work_remains_available_as_a_seed(cfg, store):
    directory = cfg.workdir / "retained"
    (directory / "repo").mkdir(parents=True)
    assert runqueue.continuation_available({"run_dir": str(directory), "integration_branch": "swarmforge/task-retained"})
    assert not runqueue.continuation_available({"run_dir": str(directory), "integration_branch": None})
