"""Settings & health page tests."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from swarmforge import settings, welcome  # noqa: E402
from swarmforge.config import parse_config  # noqa: E402
from tests.conftest import base_raw  # noqa: E402


def make_cfg(tmp_path, sandbox_mode="local"):
    return parse_config(base_raw(tmp_path / "work", sandbox_mode))


def test_get_health_checks_missing_api_key_and_dead_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "environment_check_results", lambda cfg: [
        {"name": "Provider API keys", "status": "fail",
         "message": "The API key for provider 'fake' is missing",
         "fix": "add FAKE_KEY=... to the .env file next to the config and restart the dashboard"},
        {"name": "Docker running", "status": "fail", "message": "Docker is not responding.",
         "fix": "Start Docker Desktop and wait until `docker info` succeeds."}])
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
    monkeypatch.setattr(settings, "environment_check_results", lambda cfg: [])
    monkeypatch.setattr(settings, "github_access", lambda cfg: (True, ""))
    rows = settings.get_health_checks(make_cfg(tmp_path))
    assert rows and all(r["status"] == "ok" for r in rows)


def test_get_health_checks_local_mode_marks_docker_optional(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "environment_check_results", lambda cfg: [])
    monkeypatch.setattr(settings, "github_access", lambda cfg: (True, ""))
    rows = settings.get_health_checks(make_cfg(tmp_path, "local"))
    by_name = {r["name"]: r for r in rows}
    assert by_name["Docker running"]["optional"] is True
    assert by_name["Docker running"]["status"] == "ok"
    assert "local" in by_name["Docker running"]["message"]


def test_get_health_checks_unmatched_row_becomes_extra_row(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "environment_check_results", lambda cfg: [
        {"name": "Something else is wrong", "status": "fail",
         "message": "Something else is wrong.", "fix": "do the thing."}])
    monkeypatch.setattr(settings, "github_access", lambda cfg: (True, ""))
    rows = settings.get_health_checks(make_cfg(tmp_path))
    extra = [r for r in rows if r["name"] == "Something else is wrong"]
    assert len(extra) == 1
    assert extra[0]["status"] == "fail"
    assert extra[0]["fix"] == "do the thing."


def test_get_health_checks_git_problem_matches_canonical_row(tmp_path, monkeypatch):
    """A structured 'Git' fail row maps to the canonical 'Git' row by name."""
    monkeypatch.setattr(settings, "environment_check_results", lambda cfg: [
        {"name": "Git", "status": "fail",
         "message": "git is not installed or not on PATH.",
         "fix": "Install Git for Windows and reopen the terminal."}])
    monkeypatch.setattr(settings, "github_access", lambda cfg: (True, ""))
    rows = settings.get_health_checks(make_cfg(tmp_path))
    by_name = {r["name"]: r for r in rows}
    git_row = by_name["Git"]
    assert git_row["status"] == "fail"
    assert "git is not installed" in git_row["message"].lower()
    assert "Install Git" in git_row["fix"]
    # no duplicate/unmatched row carrying the git prose
    assert "git is not installed or not on PATH" not in by_name
    git_rows = [r for r in rows if "git" in r["message"].lower()]
    assert len(git_rows) == 1
    # other canonical rows unchanged
    for name in ("Provider API keys", "Docker running", "Sandbox image built"):
        assert name in by_name


def test_get_health_checks_maps_rows_purely_by_name(tmp_path, monkeypatch):
    """Rows are mapped by exact name only: an unknown name passes through verbatim as an extra
    row — no substring matching, no 'Fix:' prose parsing."""
    monkeypatch.setattr(settings, "environment_check_results", lambda cfg: [
        {"name": "Whatever", "status": "fail", "message": "GIT IS NOT INSTALLED or not on PATH.",
         "fix": "install Git."}])
    monkeypatch.setattr(settings, "github_access", lambda cfg: (True, ""))
    rows = settings.get_health_checks(make_cfg(tmp_path))
    by_name = {r["name"]: r for r in rows}
    assert by_name["Git"]["status"] == "ok"  # no substring match on the message
    extra = by_name["Whatever"]
    assert extra["status"] == "fail"
    assert extra["fix"] == "install Git."  # verbatim, no 'Fix:' splitting
    assert "GIT IS NOT INSTALLED" not in by_name


# ------------------------------------------------- preflight structured results
def test_environment_check_results_git_fail_and_docker_rows(tmp_path, monkeypatch):
    from swarmforge import preflight
    monkeypatch.setattr(preflight.shutil, "which", lambda n: None if n == "git" else "/bin/docker")
    monkeypatch.delenv("FAKE_KEY", raising=False)
    monkeypatch.setenv("FAKE_KEY", "test-key")

    # local mode: no docker/image rows
    cfg = make_cfg(tmp_path, "local")
    names = [r["name"] for r in preflight.environment_check_results(cfg)]
    assert names == ["Provider API keys", "Git"]

    # docker mode with failing probes: both docker rows present and failing
    cfg = make_cfg(tmp_path, "docker")
    monkeypatch.setattr(preflight, "_docker_problem",
                        lambda c, t: ("Docker is not responding.", "Start Docker Desktop."))
    monkeypatch.setattr(preflight, "_image_problems", lambda c, i: [])
    rows = preflight.environment_check_results(cfg)
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"Provider API keys", "Git", "Docker running", "Sandbox image built"}
    assert by_name["Git"]["status"] == "fail"
    assert "git is not installed" in by_name["Git"]["message"]
    assert by_name["Docker running"]["status"] == "fail"
    assert by_name["Sandbox image built"]["status"] == "ok"  # not probed when docker is down

    # docker ok but image missing
    monkeypatch.setattr(preflight, "_docker_problem", lambda c, t: None)
    monkeypatch.setattr(preflight, "_image_problems",
                        lambda c, i: [("The sandbox image 'x' is not built yet.",
                                       "swarmforge sandbox build")])
    rows = preflight.environment_check_results(cfg)
    by_name = {r["name"]: r for r in rows}
    assert by_name["Docker running"]["status"] == "ok"
    assert by_name["Sandbox image built"]["status"] == "fail"
    assert by_name["Sandbox image built"]["fix"] == "swarmforge sandbox build"


def test_environment_check_results_missing_key_row(tmp_path, monkeypatch):
    from swarmforge import preflight
    monkeypatch.delenv("FAKE_KEY", raising=False)
    cfg = make_cfg(tmp_path)
    rows = preflight.environment_check_results(cfg)
    key_row = next(r for r in rows if r["name"] == "Provider API keys")
    assert key_row["status"] == "fail"
    assert "FAKE_KEY" in key_row["fix"]
    assert "fake" in key_row["message"]
    assert all(set(r) == {"name", "status", "message", "fix"} for r in rows)
    assert all(r["status"] in ("ok", "fail") for r in rows)


def test_environment_problems_prose_unchanged(tmp_path, monkeypatch):
    """Regression: environment_problems keeps the exact prose substrings other code relies on."""
    from swarmforge import preflight
    monkeypatch.delenv("FAKE_KEY", raising=False)
    monkeypatch.setattr(preflight.shutil, "which", lambda n: None if n == "git" else "/bin/docker")
    cfg = make_cfg(tmp_path, "docker")
    monkeypatch.setattr(preflight, "_docker_problem",
                        lambda c, t: ("Docker is not responding (no answer).", "Start Docker Desktop."))
    problems = preflight.environment_problems(cfg)
    text = " ".join(problems)
    assert "The API key for provider" in text
    assert "git is not installed or not on PATH" in text
    assert "Docker is not responding" in text
    # image problem prose (docker ok, image missing)
    monkeypatch.setattr(preflight, "_docker_problem", lambda c, t: None)
    monkeypatch.setattr(preflight, "_image_problems",
                        lambda c, i: [("The sandbox image 'dev-sandbox' is not built yet.",
                                       "swarmforge sandbox build --dev")])
    text = " ".join(preflight.environment_problems(cfg))
    assert "is not built yet. Run" in text
    assert "swarmforge sandbox build --dev" in text


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


# ============================================================ UI (AppTest)
DASH = Path(__file__).resolve().parents[1] / "src" / "swarmforge" / "dashboard.py"


def _write_cfg(tmp_path, monkeypatch) -> Path:
    import streamlit as st
    st.cache_resource.clear()  # the dashboard caches config/store per process
    raw = base_raw(tmp_path / "work")
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FAKE_KEY", "sentinel-secret-xyz")
    monkeypatch.setenv("SWARMFORGE_CONFIG", str(p))
    monkeypatch.setenv("SWARMFORGE_DASH_AUTOREFRESH", "0")
    monkeypatch.delenv("SWARMFORGE_DB", raising=False)
    return p


def _open_settings(tmp_path, monkeypatch, **patches):
    """Write the config, patch every network/subprocess helper, open the Settings page."""
    p = _write_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(welcome, "health_checks", lambda cfg: [])  # no docker/gh on the Work page
    defaults = {
        "get_health_checks": lambda cfg: [],
        "provider_models": lambda cfg, name: None,
        "test_models": lambda cfg, roles=None: [],
        "build_sandbox_image": lambda cfg, on_line=None: 0,
    }
    for name, fn in {**defaults, **patches}.items():
        monkeypatch.setattr(settings, name, fn)
    at = AppTest.from_file(str(DASH), default_timeout=90)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    next(b for b in at.sidebar.button if b.label == "Settings").click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return p, at


def _page_text(at) -> str:
    """Everything the page renders (main + sidebar), as one string."""
    parts = [str(at), repr(at)]
    for kind in ("title", "header", "subheader", "markdown", "caption", "code", "error",
                 "info", "success", "warning", "exception", "button", "selectbox",
                 "text_input", "number_input", "metric"):
        for e in getattr(at, kind, ()) or ():
            parts.append(str(getattr(e, "label", "")))
            parts.append(str(getattr(e, "value", "")))
    return " ".join(parts)


def test_settings_nav_exists(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(welcome, "health_checks", lambda cfg: [])
    at = AppTest.from_file(str(DASH), default_timeout=90)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Settings" in [b.label for b in at.sidebar.button]


def test_health_rows_render_status(tmp_path, monkeypatch):
    """AC1: monkeypatched missing-key and unreachable-Docker checks render red rows with fixes."""
    rows = [
        {"name": "Provider API keys", "status": "fail", "message": "FAKE_KEY is not set",
         "fix": "put FAKE_KEY=... in the .env next to swarmforge.yaml", "optional": False},
        {"name": "Docker running", "status": "fail", "message": "Docker is not reachable",
         "fix": "start Docker Desktop until `docker info` succeeds", "optional": False},
        {"name": "Git", "status": "ok", "message": "", "fix": "", "optional": True},
        {"name": "GitHub access", "status": "fail", "message": "GitHub token missing or invalid",
         "fix": "set GITHUB_TOKEN in .env", "optional": True},
    ]
    _, at = _open_settings(tmp_path, monkeypatch, get_health_checks=lambda cfg: rows)
    text = _page_text(at)
    assert "❌ **Provider API keys**" in text
    assert "❌ **Docker running**" in text
    assert "✅ **Git**" in text
    assert "❌ **GitHub access** (optional)" in text
    fixes = " ".join(c.value for c in at.code)
    assert "FAKE_KEY=..." in fixes
    assert "docker info" in fixes


def test_health_rows_render_via_preflight_mapping(tmp_path, monkeypatch):
    """AC1 variant: the real get_health_checks maps structured preflight rows + github_access."""
    _, at = _open_settings(
        tmp_path, monkeypatch,
        get_health_checks=settings.get_health_checks,  # keep the real mapping
        environment_check_results=lambda cfg: [
            {"name": "Provider API keys", "status": "fail",
             "message": "The API key for provider 'fake' is missing",
             "fix": "add FAKE_KEY=... to the .env file next to the config and restart the dashboard"},
            {"name": "Docker running", "status": "fail", "message": "Docker is not responding.",
             "fix": "start Docker Desktop and wait until `docker info` succeeds."}],
        github_access=lambda cfg: (False, "set GITHUB_TOKEN in .env"),
    )
    text = _page_text(at)
    assert "❌ **Provider API keys**" in text
    assert "❌ **Docker running**" in text
    assert "✅ **Git**" in text
    assert "❌ **GitHub access** (optional)" in text
    fixes = " ".join(c.value for c in at.code)
    assert "FAKE_KEY" in fixes
    assert "docker info" in fixes


def test_save_invalid_leaves_file_unchanged(tmp_path, monkeypatch):
    """AC2: reviewer == developer model shows the validation error; the file is untouched."""
    models = ["qwen-fake", "deepseek-fake", "kimi-fake", "glm-fake", "minimax-fake"]
    p, at = _open_settings(tmp_path, monkeypatch, provider_models=lambda cfg, name: models)
    before = p.read_bytes()
    at.selectbox(key="set_model_reviewer").select("deepseek-fake")
    next(b for b in at.button if b.label == "Save settings").click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    errs = [e.value for e in at.error]
    assert any("same model" in e or "share model" in e for e in errs), errs
    assert p.read_bytes() == before
    assert not p.with_name(p.name + ".bak").exists()


def test_save_valid_writes_file_and_backup(tmp_path, monkeypatch):
    """AC2+AC6: a valid save writes swarmforge.yaml plus a .bak of the old content, clears the
    cached config and shows a confirmation that survives the rerun."""
    import streamlit as st
    p, at = _open_settings(tmp_path, monkeypatch)  # provider_models -> None => free-text input
    before = p.read_bytes()
    clears: list[int] = []
    orig_clear = st.cache_resource.clear
    monkeypatch.setattr(st.cache_resource, "clear",
                        lambda *a, **k: (clears.append(1), orig_clear(*a, **k))[1])
    at.run()  # a plain run must not clear the cache
    assert not at.exception, [e.value for e in at.exception]
    assert not clears
    at.text_input(key="set_model_free_planner").input("qwen-new")
    next(b for b in at.button if b.label == "Save settings").click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    saved = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert saved["roles"]["planner"]["model"] == "qwen-new"
    bak = p.with_name(p.name + ".bak")
    assert bak.exists()
    assert bak.read_bytes() == before
    assert yaml.safe_load(bak.read_text(encoding="utf-8"))["roles"]["planner"]["model"] == "qwen-fake"
    assert any("Saved" in s.value for s in at.success), [s.value for s in at.success]
    assert clears, "st.cache_resource.clear() must run after a valid save"


def test_api_keys_never_rendered(tmp_path, monkeypatch):
    """AC3: the sentinel key value never appears in any rendered element or in the written file."""
    p, at = _open_settings(tmp_path, monkeypatch)
    text = _page_text(at)
    assert "sentinel-secret-xyz" not in text
    assert "FAKE_KEY" in text  # only the variable name
    next(b for b in at.button if b.label == "Save settings").click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "sentinel-secret-xyz" not in _page_text(at)
    assert "sentinel-secret-xyz" not in p.read_text(encoding="utf-8")
    bak = p.with_name(p.name + ".bak")
    assert bak.exists()
    assert "sentinel-secret-xyz" not in bak.read_text(encoding="utf-8")
    assert "FAKE_KEY" in p.read_text(encoding="utf-8")


def test_buttons_exist_and_test_models(tmp_path, monkeypatch):
    """AC4: Re-check / Build sandbox image / Test models / Save settings are wired up."""
    def fake_test_models(cfg, roles=None):
        return [{"role": "planner", "model": "qwen-fake", "ok": True,
                 "detail": "report_status({'status': 'ready'})", "total_tokens": 42,
                 "cost_usd": 0.001234, "cost_source": "pricing table"}]

    _, at = _open_settings(tmp_path, monkeypatch, test_models=fake_test_models)
    labels = [b.label for b in at.button]
    for want in ("Re-check", "Build sandbox image", "Test models", "Save settings"):
        assert want in labels
    next(b for b in at.button if b.label == "Re-check").click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    next(b for b in at.button if b.label == "Test models").click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    text = _page_text(at)
    assert "✅ planner (qwen-fake): report_status" in text
    assert "tokens=42" in text
    assert "$0.001234" in text
    assert "pricing table" in text


def test_build_sandbox_streams_output(tmp_path, monkeypatch):
    """AC4: the build button streams the helper's output into an expanded expander."""
    def fake_build(cfg, on_line=None):
        if on_line:
            on_line("Step 1/5 : FROM python:3.12-slim")
            on_line("Successfully built deadbeef")
        return 0

    _, at = _open_settings(tmp_path, monkeypatch, build_sandbox_image=fake_build)
    next(b for b in at.button if b.label == "Build sandbox image").click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Sandbox build output" in e.label for e in at.expander)
    assert "Successfully built deadbeef" in " ".join(c.value for c in at.code)
    assert any("Sandbox image built" in s.value for s in at.success)
