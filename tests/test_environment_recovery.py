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
