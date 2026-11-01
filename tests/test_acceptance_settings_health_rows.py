"""Independent acceptance tests for the Settings & health page.

Focus of this file (the two findings that failed the previous review cycle, plus a
re-verification of every acceptance criterion from a fresh angle):

* AC1  health rows come from preflight.environment_check_results mapped by exact name plus a
  GitHub access row; a missing API key and an unreachable Docker render red rows with fixes;
  checks preflight never performs (docker rows in local mode) are explicitly "skipped" /
  "not checked" and are NEVER fabricated as "ok" (in the data and in the rendered page).
* AC2  an invalid save (reviewer model == developer model) shows the validation error and
  leaves swarmforge.yaml untouched; a valid save writes the file plus a byte-identical .bak.
* AC3  API key values never appear on the rendered page or in written files; only the env-var
  name and a set/MISSING indicator are shown.
* AC4  test_models caps busy_wait_s as min(float(cfg.limits.busy_wait_s), 5.0) — it never
  zeroes it and keeps smaller configured values.
* AC5  settings.CANONICAL_CHECKS equals the set of rows preflight.environment_check_results
  actually emits in docker mode.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from swarmforge import preflight, settings, welcome
from swarmforge.config import parse_config
from tests.conftest import base_raw

DASH = Path(__file__).resolve().parents[1] / "src" / "swarmforge" / "dashboard.py"
SENTINEL = "sentinel-secret-xyz"


def make_cfg(tmp_path, sandbox_mode="local", **limits):
    raw = base_raw(tmp_path / "work", sandbox_mode)
    raw["limits"].update(limits)
    return parse_config(raw)


# --------------------------------------------------------------------- UI helpers
def _write_cfg(tmp_path, monkeypatch) -> Path:
    import streamlit as st
    st.cache_resource.clear()  # the dashboard caches config/store per process
    raw = base_raw(tmp_path / "work")
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FAKE_KEY", SENTINEL)
    monkeypatch.setenv("SWARMFORGE_CONFIG", str(p))
    monkeypatch.setenv("SWARMFORGE_DASH_AUTOREFRESH", "0")
    monkeypatch.delenv("SWARMFORGE_DB", raising=False)
    return p


def _open_settings(tmp_path, monkeypatch, **patches):
    """Write the config, patch every network/subprocess helper, open the Settings page."""
    p = _write_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(welcome, "health_checks", lambda cfg: [])
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


# ------------------------------------------------------------------------ AC1
def test_health_rows_red_for_missing_key_and_dead_docker(tmp_path, monkeypatch):
    """AC1: with the REAL preflight (docker mode, no docker, no keys) the missing-key and
    unreachable-Docker rows render as failures with the fix text; the image row that could
    not be probed is 'skipped', never a fabricated 'ok'."""
    monkeypatch.delenv("FAKE_KEY", raising=False)
    monkeypatch.setattr(preflight.shutil, "which", lambda n: "/usr/bin/git")
    monkeypatch.setattr(preflight, "_docker_problem",
                        lambda c, t: ("Docker is not responding (no answer).",
                                      "Start Docker Desktop and wait for 'Engine running'."))
    monkeypatch.setattr(settings, "github_access",
                        lambda cfg: (False, "set GITHUB_TOKEN in the .env"))
    cfg = make_cfg(tmp_path, "docker")
    rows = {r["name"]: r for r in settings.get_health_checks(cfg)}
    assert rows["Provider API keys"]["status"] == "fail"
    assert "fake" in rows["Provider API keys"]["message"]
    assert "FAKE_KEY" in rows["Provider API keys"]["fix"]
    assert rows["Git"]["status"] == "ok"
    assert rows["Docker running"]["status"] == "fail"
    assert "Docker Desktop" in rows["Docker running"]["fix"]
    # the image probe cannot run while the daemon is down -> not checked, not fake "ok"
    assert rows["Sandbox image built"]["status"] == "skipped"
    assert "not checked" in rows["Sandbox image built"]["message"]
    assert rows["GitHub access"]["status"] == "fail"


def test_github_access_uses_token_and_user_call(tmp_path, monkeypatch):
    """AC1: the GitHub row is built from github.get_token plus a GET {api_url}/user call."""
    from swarmforge import github
    calls: list = []
    monkeypatch.setattr(preflight, "_docker_problem", lambda c, t: None)
    monkeypatch.setattr(preflight, "_image_problems", lambda c, i: [])
    monkeypatch.setattr(preflight, "_sandbox_tool_problems", lambda c, i, t: [])

    class Resp:
        status_code = 200

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers))
        return Resp()

    monkeypatch.setattr(github, "get_token", lambda gh_cfg: "ghp_test_token")
    monkeypatch.setattr(settings, "environment_check_results",
                        lambda cfg: preflight.environment_check_results(cfg))
    monkeypatch.setattr(settings.httpx, "get", fake_get)
    cfg = make_cfg(tmp_path, "docker")
    rows = {r["name"]: r for r in settings.get_health_checks(cfg)}
    assert rows["GitHub access"]["status"] == "ok"
    assert calls and calls[0][0].endswith("/user")
    assert calls[0][1]["Authorization"] == "Bearer ghp_test_token"


def test_local_mode_never_fabricates_ok_rows_for_unperformed_checks(tmp_path, monkeypatch):
    """AC1 (previous defect): checks preflight never performs must be 'skipped' /
    'not checked' — never a fake 'ok'."""
    monkeypatch.setattr(settings, "environment_check_results", lambda cfg: [
        {"name": "Git", "status": "ok", "message": "", "fix": ""}])
    monkeypatch.setattr(settings, "github_access", lambda cfg: (True, ""))
    rows = settings.get_health_checks(make_cfg(tmp_path, "local"))
    by = {r["name"]: r for r in rows}
    for name in ("Docker running", "Sandbox image built", "Sandbox tools"):
        assert by[name]["status"] == "skipped", f"{name} must not be fabricated as ok"
        assert "not checked" in by[name]["message"]
        assert "local" in by[name]["message"]
    # every green row corresponds to a check that was actually performed
    assert {r["name"] for r in rows if r["status"] == "ok"} == {"Git", "GitHub access"}


def test_ui_renders_skipped_rows_with_neutral_icon(tmp_path, monkeypatch):
    """AC1: the rendered page shows skipped rows with a neutral icon — neither ✅ nor ❌."""
    _, at = _open_settings(
        tmp_path, monkeypatch,
        get_health_checks=settings.get_health_checks,  # real mapping
        environment_check_results=lambda cfg: [
            {"name": "Git", "status": "ok", "message": "", "fix": ""}],
        github_access=lambda cfg: (True, ""))
    md = " ".join(m.value for m in at.markdown)
    assert "⏭️ **Docker running** (optional) — not checked (sandbox.mode: local)" in md
    assert "✅ **Docker running**" not in md
    assert "❌ **Docker running**" not in md
    assert "✅ **Git**" in md


# ------------------------------------------------------------------------ AC2
def test_validate_for_save_rejects_shared_and_missing_model(tmp_path):
    """AC2: the reviewer==developer model edit fails validation; a distinct model passes."""
    raw = base_raw(tmp_path / "work")
    bad = settings.apply_edits(raw, {"reviewer": {"model": "deepseek-fake"}}, {})
    errs = settings.validate_for_save(bad, tmp_path / "swarmforge.yaml")
    assert errs and any("same model" in e or "share model" in e for e in errs)
    zero = settings.apply_edits(raw, {}, {"max_concurrent_agents": 0})
    assert any("max_concurrent_agents" in e for e in
               settings.validate_for_save(zero, tmp_path / "swarmforge.yaml"))
    good = settings.apply_edits(raw, {"reviewer": {"model": "glm-fake-2"}}, {})
    assert settings.validate_for_save(good, tmp_path / "swarmforge.yaml") == []


def test_ui_invalid_save_shows_error_and_leaves_file_unchanged(tmp_path, monkeypatch):
    """AC2: saving reviewer == developer model shows the inline error; file and .bak untouched."""
    models = ["deepseek-fake", "glm-fake", "kimi-fake", "minimax-fake", "qwen-fake"]
    p, at = _open_settings(tmp_path, monkeypatch, provider_models=lambda cfg, name: models)
    before = p.read_bytes()
    at.selectbox(key="set_model_reviewer").select("deepseek-fake")
    next(b for b in at.button if b.label == "Save settings").click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("same model" in e.value or "share model" in e.value for e in at.error), \
        [e.value for e in at.error]
    assert p.read_bytes() == before
    assert not Path(str(p) + ".bak").exists()


def test_ui_valid_save_writes_file_and_byte_identical_backup(tmp_path, monkeypatch):
    """AC2: a valid save writes swarmforge.yaml and a byte-identical swarmforge.yaml.bak."""
    p, at = _open_settings(tmp_path, monkeypatch)  # provider_models None -> free-text input
    before = p.read_bytes()
    at.text_input(key="set_model_free_planner").input("qwen-new")
    next(b for b in at.button if b.label == "Save settings").click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    saved = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert saved["roles"]["planner"]["model"] == "qwen-new"
    bak = Path(str(p) + ".bak")
    assert bak.exists()
    assert bak.read_bytes() == before


# ------------------------------------------------------------------------ AC3
def test_page_never_renders_api_key_values(tmp_path, monkeypatch):
    """AC3: with the real health mapping and FAKE_KEY set to a sentinel, the sentinel never
    appears anywhere on the page; only the env var name and set/MISSING status do."""
    _, at = _open_settings(
        tmp_path, monkeypatch,
        get_health_checks=settings.get_health_checks,
        environment_check_results=lambda cfg: preflight.environment_check_results(cfg),
        github_access=lambda cfg: (False, "set GITHUB_TOKEN in the .env next to swarmforge.yaml"))
    text = _page_text(at)
    assert SENTINEL not in text
    assert "FAKE_KEY" in text          # env var name is shown
    assert "set ✅" in text            # only whether it is set


def test_written_files_never_contain_api_key(tmp_path, monkeypatch):
    """AC3: neither the saved swarmforge.yaml nor its .bak ever contains the key value."""
    monkeypatch.setenv("FAKE_KEY", SENTINEL)
    p = tmp_path / "swarmforge.yaml"
    raw = base_raw(tmp_path / "work")
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    new = settings.apply_edits(raw, {"reviewer": {"model": "glm-fake-2"}}, {"budget_usd": 9})
    assert settings.validate_for_save(new, p) == []
    bak = settings.save_config_with_backup(p, new)
    for f in (p, bak):
        assert SENTINEL not in f.read_text(encoding="utf-8")
    assert "FAKE_KEY" in p.read_text(encoding="utf-8")  # only the variable name


# ------------------------------------------------------------------------ AC4
class _FakeUsage:
    total_tokens = 7
    cost_usd = 0.0
    cost_source = "test"


def _fake_llm(captured: dict):
    from types import SimpleNamespace

    class FakeLLM:
        def __init__(self, config, budget=None, max_retries=None, busy_wait_s=None, **kw):
            captured["busy_wait_s"] = busy_wait_s

        async def chat(self, role, messages, tools=None, **kw):
            captured["messages"] = messages
            captured["tools"] = tools
            return SimpleNamespace(
                tool_calls=[{"function": {"name": "report_status",
                                          "arguments": '{"status": "ready"}'}}],
                content="", usage=_FakeUsage())
    return FakeLLM


def test_test_models_caps_busy_wait_s_at_five(tmp_path, monkeypatch):
    """AC4: the default busy_wait_s of 900 is capped to min(900, 5.0) = 5.0 — not zeroed."""
    from swarmforge import llm
    captured: dict = {}
    monkeypatch.setattr(llm, "LLMClient", _fake_llm(captured))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    cfg = make_cfg(tmp_path)
    assert cfg.limits.busy_wait_s == 900  # the default
    rows = settings.test_models(cfg, roles=["planner"])
    assert rows and rows[0]["ok"]
    assert captured["busy_wait_s"] == min(float(cfg.limits.busy_wait_s), 5.0)
    assert captured["busy_wait_s"] == 5.0
    assert captured["busy_wait_s"] != 0.0


def test_test_models_keeps_smaller_busy_wait_s(tmp_path, monkeypatch):
    """AC4: a configured busy_wait_s below the cap is kept as-is (never zeroed)."""
    from swarmforge import llm
    captured: dict = {}
    monkeypatch.setattr(llm, "LLMClient", _fake_llm(captured))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    cfg = make_cfg(tmp_path, busy_wait_s=2)
    settings.test_models(cfg, roles=["planner"])
    assert captured["busy_wait_s"] == 2.0
    assert captured["busy_wait_s"] != 0.0


def test_test_models_sends_tiny_tool_calling_request(tmp_path, monkeypatch):
    """AC4: the request is the same tiny tool-calling request checks.run_checks(live=True) uses."""
    from swarmforge import checks
    captured: dict = {}
    monkeypatch.setattr("swarmforge.llm.LLMClient", _fake_llm(captured))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    cfg = make_cfg(tmp_path)
    settings.test_models(cfg, roles=["planner"])
    src = Path(checks.__file__).read_text(encoding="utf-8")
    # the exact user message and tool name used by the live block of checks.run_checks
    assert "Call report_status with status 'ready'." in src
    assert captured["messages"][-1]["content"] == "Call report_status with status 'ready'."
    assert captured["tools"][0]["function"]["name"] == "report_status"


# ------------------------------------------------------------------------ AC5
def test_canonical_checks_equal_preflight_rows_in_docker_mode(tmp_path, monkeypatch):
    """AC5: every CANONICAL_CHECKS entry is a row preflight actually emits (and vice versa),
    both when Docker answers and when it is down."""
    monkeypatch.setattr(preflight, "_docker_problem", lambda c, t: None)
    monkeypatch.setattr(preflight, "_image_problems", lambda c, i: [])
    monkeypatch.setattr(preflight, "_sandbox_tool_problems", lambda c, i, t: [])
    monkeypatch.setenv("FAKE_KEY", "test-key")
    cfg = make_cfg(tmp_path, "docker")
    emitted = {r["name"] for r in preflight.environment_check_results(cfg)}
    assert set(settings.CANONICAL_CHECKS) == emitted
    # Docker down: same row names (the image row becomes 'skipped', still emitted)
    monkeypatch.setattr(preflight, "_docker_problem",
                        lambda c, t: ("Docker is not responding.", "Start Docker Desktop."))
    emitted_down = {r["name"] for r in preflight.environment_check_results(cfg)}
    assert set(settings.CANONICAL_CHECKS) == emitted_down
