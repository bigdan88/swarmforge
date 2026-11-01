"""Settings & health page tests."""
from __future__ import annotations

import copy
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
         "fix": "Start Docker Desktop and wait until `docker info` succeeds."},
        {"name": "Git", "status": "ok", "message": "", "fix": ""}])
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
    assert rows
    # local mode: unperformed docker checks are 'skipped', everything actually checked is 'ok'
    assert all(r["status"] in ("ok", "skipped") for r in rows)
    assert any(r["name"] == "GitHub access" and r["status"] == "ok" for r in rows)


def test_get_health_checks_local_mode_marks_docker_optional(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "environment_check_results", lambda cfg: [])
    monkeypatch.setattr(settings, "github_access", lambda cfg: (True, ""))
    rows = settings.get_health_checks(make_cfg(tmp_path, "local"))
    by_name = {r["name"]: r for r in rows}
    assert by_name["Docker running"]["optional"] is True
    # AC1: never fabricate an 'ok' for a check preflight does not perform -> explicit 'skipped'
    assert by_name["Docker running"]["status"] == "skipped"
    assert "not checked" in by_name["Docker running"]["message"]
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
        {"name": "Provider API keys", "status": "ok", "message": "", "fix": ""},
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
    for name in ("Provider API keys", "Docker running", "Sandbox image built", "Sandbox tools"):
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
    assert "Git" not in by_name  # no fabricated row when preflight didn't report Git
    extra = by_name["Whatever"]
    assert extra["status"] == "fail"
    assert extra["fix"] == "install Git."  # verbatim, no 'Fix:' splitting
    assert "GIT IS NOT INSTALLED" not in by_name


def test_canonical_checks_are_all_performed_by_preflight(tmp_path, monkeypatch):
    """CANONICAL_CHECKS lists exactly the checks preflight.environment_check_results emits."""
    from swarmforge import preflight
    monkeypatch.setattr(preflight, "_docker_problem", lambda c, t: None)
    monkeypatch.setattr(preflight, "_image_problems", lambda c, i: [])
    monkeypatch.setattr(preflight, "_sandbox_tool_problems", lambda c, i, t: [])
    monkeypatch.setenv("FAKE_KEY", "test-key")
    cfg = make_cfg(tmp_path, "docker")
    names = {r["name"] for r in preflight.environment_check_results(cfg)}
    assert names == set(settings.CANONICAL_CHECKS)
    assert "Provider base_url" not in settings.CANONICAL_CHECKS
    assert "GitHub CLI" not in settings.CANONICAL_CHECKS


def test_get_health_checks_never_invents_rows_for_unperformed_checks(tmp_path, monkeypatch):
    """A canonical check absent from the preflight results is never reported as 'ok'."""
    monkeypatch.setattr(settings, "environment_check_results", lambda cfg: [
        {"name": "Git", "status": "fail", "message": "git is not installed or not on PATH.",
         "fix": "Install Git for Windows and reopen the terminal."}])
    monkeypatch.setattr(settings, "github_access", lambda cfg: (True, ""))
    rows = settings.get_health_checks(make_cfg(tmp_path))
    names = {r["name"] for r in rows}
    assert "Provider API keys" not in names  # not checked -> no fabricated 'ok' row
    by_name = {r["name"]: r for r in rows}
    assert by_name["Docker running"]["status"] == "skipped"
    assert "not checked" in by_name["Docker running"]["message"]
    assert "local" in by_name["Docker running"]["message"]
    assert by_name["Sandbox image built"]["status"] == "skipped"
    assert "local" in by_name["Sandbox image built"]["message"]
    assert by_name["Sandbox tools"]["status"] == "skipped"
    assert "local" in by_name["Sandbox tools"]["message"]
    # the only green 'ok' row is the one actually checked (GitHub access)
    assert {r["name"] for r in rows if r["status"] == "ok"} == {"GitHub access"}


def test_test_models_caps_busy_wait_s(tmp_path, monkeypatch):
    """test_models caps busy_wait_s at 5s instead of zeroing it."""
    import types

    from swarmforge import llm

    captured: list[float] = []

    class FakeLLM:
        def __init__(self, config, budget=None, max_retries=None, busy_wait_s=None, **kw):
            captured.append(busy_wait_s)

        async def chat(self, role, messages, **kw):
            return types.SimpleNamespace(
                tool_calls=[{"function": {"name": "report_status",
                                          "arguments": '{"status": "ready"}'}}],
                content="", usage=types.SimpleNamespace(total_tokens=10, cost_usd=0.001,
                                                        cost_source=None))

    monkeypatch.setattr(llm, "LLMClient", FakeLLM)
    cfg = parse_config(base_raw(tmp_path / "work"))
    settings.test_models(cfg, roles=["planner"])
    assert captured == [5.0] and captured[0] > 0

    raw = base_raw(tmp_path / "work")
    raw["limits"]["busy_wait_s"] = 2
    cfg = parse_config(raw)
    settings.test_models(cfg, roles=["planner"])
    assert captured[-1] == 2.0


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

    # docker mode with failing probes: Docker row fails; the image row is 'skipped',
    # never fabricated as 'ok' (AC1) since the probe cannot run while the daemon is down
    cfg = make_cfg(tmp_path, "docker")
    monkeypatch.setattr(preflight, "_docker_problem",
                        lambda c, t: ("Docker is not responding.", "Start Docker Desktop."))
    monkeypatch.setattr(preflight, "_image_problems", lambda c, i: [])
    monkeypatch.setattr(preflight, "_sandbox_tool_problems", lambda c, i, t: [])
    rows = preflight.environment_check_results(cfg)
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"Provider API keys", "Git", "Docker running",
                           "Sandbox image built", "Sandbox tools"}
    assert by_name["Git"]["status"] == "fail"
    assert "git is not installed" in by_name["Git"]["message"]
    assert by_name["Docker running"]["status"] == "fail"
    assert by_name["Sandbox image built"]["status"] == "skipped"  # not ok! check not performed
    assert "not checked" in by_name["Sandbox image built"]["message"]

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
    assert by_name["Sandbox tools"]["status"] == "skipped"  # image missing -> probe not run
    assert "not checked" in by_name["Sandbox tools"]["message"]


def test_get_health_checks_surfaces_sandbox_tools_fail_with_fix(tmp_path, monkeypatch):
    """A 'Sandbox tools' fail row from preflight keeps its fix hint (e.g. missing ruff)."""
    monkeypatch.setattr(settings, "environment_check_results", lambda cfg: [
        {"name": "Provider API keys", "status": "ok", "message": "", "fix": ""},
        {"name": "Git", "status": "ok", "message": "", "fix": ""},
        {"name": "Docker running", "status": "ok", "message": "", "fix": ""},
        {"name": "Sandbox image built", "status": "ok", "message": "", "fix": ""},
        {"name": "Sandbox tools", "status": "fail",
         "message": "The sandbox image 'dev-sandbox' is missing 'ruff'.",
         "fix": "install the tool in the image Dockerfile (`pip install ruff`) and rebuild: "
                "swarmforge sandbox build"}])
    monkeypatch.setattr(settings, "github_access", lambda cfg: (True, ""))
    rows = settings.get_health_checks(make_cfg(tmp_path, "docker"))
    row = next(r for r in rows if r["name"] == "Sandbox tools")
    assert row["status"] == "fail"
    assert "missing 'ruff'" in row["message"]
    assert "pip install ruff" in row["fix"]
    assert row["optional"] is False


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
             "fix": "start Docker Desktop and wait until `docker info` succeeds."},
            {"name": "Git", "status": "ok", "message": "", "fix": ""}],
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


# ============================================== comment-preserving save (F3)

COMMENTED_YAML = """\
# SwarmForge configuration
# Edit roles and limits; comments survive dashboard saves.

providers:
  fake:
    base_url: https://fake.test/v1
    api_key_env: FAKE_KEY  # never put the key itself here

roles:
  planner:
    provider: fake
    model: qwen-fake
    max_steps: 15
  developer_backend:
    provider: fake
    model: deepseek-fake
    max_steps: 15
  developer_frontend:
    provider: fake
    model: kimi-fake
    max_steps: 15
  reviewer:
    provider: fake
    model: glm-fake
    max_steps: 10
  tester:
    provider: fake
    model: minimax-fake
    max_steps: 15

developer_roles: [developer_backend, developer_frontend]

limits:
  budget_usd: 5  # monthly cap
  max_runtime_s: 300
  max_concurrent_agents: 2
  max_repair_cycles: 2

sandbox:
  mode: local
  command_timeout_s: 120
project:
  test_command: python -m pytest -q
workdir: .
knowledge:
  vault: ./knowledge
self_heal:
  max_attempts: 0
"""


def test_save_preserves_comments_and_layout(tmp_path):
    p = tmp_path / "swarmforge.yaml"
    p.write_text(COMMENTED_YAML, encoding="utf-8")
    original_bytes = p.read_bytes()
    raw = yaml.safe_load(COMMENTED_YAML)
    raw["workdir"] = str(tmp_path / "work")
    raw["knowledge"]["vault"] = str(tmp_path / "work" / "knowledge")
    raw["project"]["test_command"] = "python -m pytest -q -p no:cacheprovider"
    edited = settings.apply_edits(
        raw, {"reviewer": {"model": "glm-fake-2", "temperature": 0.2, "max_steps": 12}},
        {"budget_usd": 9})
    backup = settings.save_config_with_backup(p, edited)
    assert backup.read_bytes() == original_bytes  # .bak is a byte-identical copy
    saved_text = p.read_text(encoding="utf-8")
    # comments, blank line and key order survive
    assert "# SwarmForge configuration" in saved_text
    assert "# Edit roles and limits; comments survive dashboard saves." in saved_text
    assert "api_key_env: FAKE_KEY  # never put the key itself here" in saved_text
    assert "budget_usd: 9  # monthly cap" in saved_text
    assert "roles:" in saved_text and "limits:" in saved_text
    assert saved_text.index("providers:") < saved_text.index("roles:")
    assert saved_text.index("roles:") < saved_text.index("limits:")
    assert saved_text.index("limits:") < saved_text.index("sandbox:")
    assert "\n\nlimits:" in saved_text
    # changed values updated
    assert "glm-fake-2" in saved_text
    assert "budget_usd: 9" in saved_text
    saved = yaml.safe_load(saved_text)
    assert saved["roles"]["reviewer"]["model"] == "glm-fake-2"
    assert saved["roles"]["reviewer"]["temperature"] == 0.2
    assert saved["roles"]["reviewer"]["max_steps"] == 12
    assert saved["limits"]["budget_usd"] == 9
    assert saved["limits"]["max_runtime_s"] == 300
    assert saved["providers"] == edited["providers"]


def test_save_inserts_missing_key(tmp_path):
    text = COMMENTED_YAML.replace("  max_runtime_s: 300\n", "")
    p = tmp_path / "swarmforge.yaml"
    p.write_text(text, encoding="utf-8")
    raw = yaml.safe_load(text)
    raw["workdir"] = str(tmp_path / "work")
    raw["knowledge"]["vault"] = str(tmp_path / "work" / "knowledge")
    raw["project"]["test_command"] = "python -m pytest -q -p no:cacheprovider"
    edited = settings.apply_edits(raw, {}, {"max_runtime_s": 600})
    settings.save_config_with_backup(p, edited)
    saved_text = p.read_text(encoding="utf-8")
    # the new key lands inside the limits block, after `limits:` and before `sandbox:`
    assert "max_runtime_s: 600" in saved_text
    assert saved_text.index("limits:") < saved_text.index("max_runtime_s: 600")
    assert saved_text.index("max_runtime_s: 600") < saved_text.index("sandbox:")
    assert "# monthly cap" in saved_text  # comments still survive
    saved = yaml.safe_load(saved_text)
    assert saved["limits"]["max_runtime_s"] == 600
    assert saved["limits"]["budget_usd"] == 5


def test_save_fallback_on_unparseable_original(tmp_path):
    p = tmp_path / "swarmforge.yaml"
    p.write_text("providers: [unclosed\n  bad yaml: {{{\n", encoding="utf-8")
    original_bytes = p.read_bytes()
    raw = base_raw(tmp_path / "work")
    backup = settings.save_config_with_backup(p, raw)
    assert backup.read_bytes() == original_bytes
    saved = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert saved == raw


def test_save_fallback_on_unexpected_layout(tmp_path):
    # a change involving a container (list) falls back to a full dump without raising
    p = tmp_path / "swarmforge.yaml"
    raw = base_raw(tmp_path / "work")
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    changed = copy.deepcopy(raw)
    changed["developer_roles"] = ["developer_backend"]
    settings.save_config_with_backup(p, changed)
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["developer_roles"] == ["developer_backend"]


def test_patch_config_text_quoting():
    text = "a: old\nb: 1\nc: yes\nd: something\n"
    old = {"a": "old", "b": 1, "c": True, "d": "something"}
    new = {"a": "new value", "b": 2, "c": False, "d": None}
    patched = settings.patch_config_text(text, old, new)
    assert patched is not None
    lines = patched.splitlines()
    assert lines[0] == "a: new value"  # plain strings stay single-line scalars
    assert lines[1] == "b: 2"  # ints stay bare
    assert lines[2] == "c: false"  # bools become true/false
    assert lines[3] == "d: null"  # None becomes null
    assert yaml.safe_load(patched) == new
    # values that would be misread as another type stay quoted
    patched = settings.patch_config_text("a: old\n", {"a": "old"}, {"a": "yes"})
    assert patched is not None
    assert patched.splitlines()[0] != "a: yes"
    assert yaml.safe_load(patched)["a"] == "yes"


def test_patch_config_text_inserts_missing_section():
    text = "providers:\n  fake:\n    base_url: https://x.test\n"
    old = yaml.safe_load(text)
    new = {"providers": old["providers"], "limits": {"budget_usd": 3}}
    patched = settings.patch_config_text(text, old, new)
    assert patched is not None
    assert "limits:" in patched
    assert "budget_usd: 3" in patched
    assert yaml.safe_load(patched) == new
