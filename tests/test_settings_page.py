"""Settings & health page tests."""
from __future__ import annotations

import yaml

from swarmforge import settings
from swarmforge.config import parse_config
from tests.conftest import base_raw


def make_cfg(tmp_path, sandbox_mode="local"):
    return parse_config(base_raw(tmp_path / "work", sandbox_mode))


def test_get_health_checks_missing_api_key_and_dead_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "environment_problems", lambda cfg: [
        ("You are missing the API key for provider 'fake' (environment variable FAKE_KEY is not set). "
         "Fix: put FAKE_KEY=... in the .env file next to the config."),
        ("Docker is not reachable (is Docker Desktop running?). Fix: start Docker Desktop and wait "
         "until `docker info` succeeds.")])
    monkeypatch.setattr(settings, "github_access", lambda cfg: (False, "set GITHUB_TOKEN"))
    cfg = make_cfg(tmp_path)
    rows = settings.get_health_checks(cfg)
    by_name = {r["name"]: r for r in rows}
    assert by_name["Provider API keys"]["status"] == "fail"
    assert "FAKE_KEY" in by_name["Provider API keys"]["fix"]
    assert by_name["Docker running"]["status"] == "fail"
    assert "docker info" in by_name["Docker running"]["fix"]
    assert by_name["Git"]["status"] == "ok"
    gh = [r for r in rows if r["name"] == "GitHub access"]
    assert len(gh) == 1 and gh[0]["status"] == "fail"
    # no API key value ever appears
    assert not any("test-key" in str(r) for r in rows)


def test_get_health_checks_all_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "environment_problems", lambda cfg: [])
    monkeypatch.setattr(settings, "github_access", lambda cfg: (True, ""))
    rows = settings.get_health_checks(make_cfg(tmp_path))
    assert rows and all(r["status"] == "ok" for r in rows)


def test_get_health_checks_local_mode_marks_docker_optional(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "environment_problems", lambda cfg: [])
    monkeypatch.setattr(settings, "github_access", lambda cfg: (True, ""))
    rows = settings.get_health_checks(make_cfg(tmp_path, "local"))
    by_name = {r["name"]: r for r in rows}
    assert by_name["Docker running"]["optional"] is True
    assert by_name["Docker running"]["status"] == "ok"
    assert "local" in by_name["Docker running"]["message"]


def test_get_health_checks_unmatched_problem_becomes_extra_row(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "environment_problems", lambda cfg: [
        "Something else is wrong. Fix: do the thing."])
    monkeypatch.setattr(settings, "github_access", lambda cfg: (True, ""))
    rows = settings.get_health_checks(make_cfg(tmp_path))
    extra = [r for r in rows if r["name"] == "Something else is wrong"]
    assert len(extra) == 1
    assert extra[0]["status"] == "fail"
    assert extra[0]["fix"] == "do the thing."


def test_github_access_no_token(tmp_path, monkeypatch):
    from swarmforge import github
    monkeypatch.setattr(github, "get_token", lambda gh_cfg: None)
    monkeypatch.setattr(settings.httpx, "get", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("network")))
    ok, fix = settings.github_access(make_cfg(tmp_path))
    assert ok is False
    assert "GITHUB_TOKEN" in fix


def test_github_access_offline_never_raises(tmp_path, monkeypatch):
    from swarmforge import github
    monkeypatch.setattr(github, "get_token", lambda gh_cfg: "tok")
    monkeypatch.setattr(settings.httpx, "get", lambda *a, **kw: (_ for _ in ()).throw(OSError("down")))
    ok, fix = settings.github_access(make_cfg(tmp_path))
    assert ok is False
    assert "not reachable" in fix


def test_apply_edits_and_validate(tmp_path):
    raw = base_raw(tmp_path / "work")
    edited = settings.apply_edits(
        raw, {"reviewer": {"model": "glm-fake-2", "temperature": 0.3, "max_steps": 12}},
        {"budget_usd": 7, "max_runtime_s": 600})
    # original untouched
    assert raw["roles"]["reviewer"]["model"] == "glm-fake"
    assert raw["limits"]["budget_usd"] == 5
    assert edited["roles"]["reviewer"]["model"] == "glm-fake-2"
    assert edited["roles"]["reviewer"]["temperature"] == 0.3
    assert edited["roles"]["reviewer"]["max_steps"] == 12
    assert edited["limits"]["budget_usd"] == 7
    assert edited["limits"]["max_runtime_s"] == 600
    assert edited["providers"] == raw["providers"]
    assert settings.validate_for_save(edited, tmp_path / "swarmforge.yaml") == []
    # invalid: reviewer model equals a developer model
    bad = settings.apply_edits(raw, {"reviewer": {"model": "deepseek-fake"}}, {})
    errs = settings.validate_for_save(bad, tmp_path / "swarmforge.yaml")
    assert errs and any("same model" in e or "share model" in e for e in errs)


def test_save_config_with_backup(tmp_path):
    p = tmp_path / "swarmforge.yaml"
    raw = base_raw(tmp_path / "work")
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    old_bytes = p.read_bytes()
    new_raw = settings.apply_edits(raw, {"reviewer": {"model": "glm-fake-2"}}, {"budget_usd": 9})
    backup = settings.save_config_with_backup(p, new_raw)
    assert backup == tmp_path / "swarmforge.yaml.bak"
    assert backup.read_bytes() == old_bytes
    saved = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert saved["roles"]["reviewer"]["model"] == "glm-fake-2"
    assert saved["limits"]["budget_usd"] == 9


def test_save_never_writes_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "sentinel-secret-xyz")
    p = tmp_path / "swarmforge.yaml"
    raw = base_raw(tmp_path / "work")
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    backup = settings.save_config_with_backup(p, raw)
    assert "sentinel-secret-xyz" not in p.read_text(encoding="utf-8")
    assert "sentinel-secret-xyz" not in backup.read_text(encoding="utf-8")
    assert "FAKE_KEY" in p.read_text(encoding="utf-8")  # only the env var name


def test_read_raw_empty(tmp_path):
    p = tmp_path / "swarmforge.yaml"
    p.write_text("", encoding="utf-8")
    assert settings.read_raw(p) == {}
    p.write_text(yaml.safe_dump({"limits": {"budget_usd": 1}}, sort_keys=False), encoding="utf-8")
    assert settings.read_raw(p)["limits"]["budget_usd"] == 1
