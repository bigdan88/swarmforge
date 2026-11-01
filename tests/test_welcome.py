import subprocess
from pathlib import Path

import pytest

from swarmforge import welcome
from swarmforge.config import parse_config
from tests.conftest import base_raw


def make_cfg(tmp_path, sandbox_mode="local"):
    return parse_config(base_raw(tmp_path / "work", sandbox_mode))


def test_health_checks_missing_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("FAKE_KEY", raising=False)
    cfg = make_cfg(tmp_path)
    items = welcome.health_checks(cfg)
    by_name = {i.name: i for i in items}
    assert not by_name["API key (FAKE_KEY)"].ok
    assert "FAKE_KEY" in by_name["API key (FAKE_KEY)"].fix
    assert ".env" in by_name["API key (FAKE_KEY)"].fix
    gh = by_name["GitHub login"]
    assert gh.optional
    assert "gh auth login" in gh.fix


def test_health_checks_all_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    monkeypatch.setattr(welcome, "github_logged_in", lambda cfg: True)
    cfg = make_cfg(tmp_path)
    items = welcome.health_checks(cfg)
    assert all(i.ok for i in items)


def test_health_checks_docker_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    monkeypatch.setattr(welcome, "docker_running", lambda cfg: True)
    monkeypatch.setattr(welcome, "image_built", lambda cfg: False)
    cfg = make_cfg(tmp_path, "docker")
    items = welcome.health_checks(cfg)
    by_name = {i.name: i for i in items}
    assert by_name["Docker running"].ok
    assert not by_name["Sandbox image built"].ok
    assert "swarmforge sandbox build" in by_name["Sandbox image built"].fix


def test_health_checks_uses_module_attributes(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    monkeypatch.setattr(welcome, "docker_running", lambda cfg: False)
    cfg = make_cfg(tmp_path, "docker")
    items = welcome.health_checks(cfg)
    assert not next(i for i in items if i.name == "Docker running").ok


def test_health_checks_broken_helper_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    def boom(cfg):
        raise RuntimeError("boom")
    monkeypatch.setattr(welcome, "docker_running", boom)
    cfg = make_cfg(tmp_path, "docker")
    items = welcome.health_checks(cfg)
    item = next(i for i in items if i.name == "Docker running")
    assert not item.ok
    assert "docker info" in item.fix


def test_prepare_sample_repo(tmp_path):
    dest = tmp_path / "sample"
    welcome.prepare_sample_repo(dest)
    assert (dest / ".git").is_dir()
    # idempotent: second call is a no-op
    welcome.prepare_sample_repo(dest)
    assert (dest / ".git").is_dir()


def test_prepare_sample_repo_failure(tmp_path, monkeypatch):
    dest = tmp_path / "sample"
    def fake_run(*a, **kw):
        return subprocess.CompletedProcess(a, 1, "", "boom")
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="boom"):
        welcome.prepare_sample_repo(dest)


def test_sample_issue_text():
    title, body = welcome.sample_issue_text()
    assert title
    assert body
    assert "export" in title.lower()


def test_start_sample_run(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    cfg = make_cfg(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setattr(welcome, "prepare_sample_repo", lambda dest=None: repo)

    recorded = {}

    def fake_start_run(cfg_, **kwargs):
        recorded.update(kwargs)
        return "run-123", Path("/tmp/x")

    from swarmforge import launcher
    monkeypatch.setattr(launcher, "start_run", fake_start_run)

    remembered = []
    store = type("Store", (), {"remember_repo": lambda self, r, limit=15: remembered.append(r)})()

    run_id = welcome.start_sample_run(cfg, store)
    assert run_id == "run-123"
    assert recorded["budget"] == 3.0
    assert recorded["max_cycles"] == 1
    assert recorded["repo"] == str(repo)
    assert recorded["mode"] == "swarm"
    assert recorded["approval"] == "dashboard"
    assert recorded["issue_title"]
    assert recorded["issue_body"]
    assert remembered == [str(repo)]
