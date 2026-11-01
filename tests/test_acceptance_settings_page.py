"""Acceptance tests for the Settings & health page.

Complements tests/test_settings_page.py with coverage of the remaining acceptance-criteria
details: full canonical health-row set + GitHub /user validity (AC1), the Test models helper
capping busy_wait_s, rendering fail/cost-unavailable rows, sending the identical tiny request
as checks.run_checks(live=True) and the Re-check button re-running the checks (AC4), the
provider name + env var + set/MISSING indicator (AC3), and the Models/Limits editing
behaviour: selectbox populated from provider_models when reachable with accept_new_options
(free text), free text when unreachable, and temperature/max_steps/limits edits actually
persisted on save (AC5)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from swarmforge import settings, welcome
from swarmforge.config import parse_config
from tests.conftest import base_raw

DASH = Path(__file__).resolve().parents[1] / "src" / "swarmforge" / "dashboard.py"

MODELS = ["deepseek-fake", "glm-fake", "kimi-fake", "minimax-fake", "qwen-fake"]


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


def _open_settings(tmp_path, monkeypatch, *, unset_key: bool = False, **patches):
    """Write the config, patch every network/subprocess helper, open the Settings page."""
    p = _write_cfg(tmp_path, monkeypatch)
    if unset_key:
        monkeypatch.delenv("FAKE_KEY", raising=False)
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


# ------------------------------------------------------------------ AC1
def test_health_rows_cover_every_check_plus_github(tmp_path, monkeypatch):
    """AC1: get_health_checks returns exactly one row per preflight check plus GitHub access."""
    monkeypatch.setattr(settings, "environment_check_results", lambda cfg: [])
    monkeypatch.setattr(settings, "github_access", lambda cfg: (True, ""))
    cfg = parse_config(base_raw(tmp_path / "work"))
    rows = settings.get_health_checks(cfg)
    names = [r["name"] for r in rows]
    for want in ("Provider API keys", "Provider base_url", "GitHub CLI", "Git",
                 "Docker running", "Sandbox image built", "GitHub access"):
        assert names.count(want) == 1, names
    assert all(r["status"] == "ok" for r in rows)


def test_github_access_uses_token_and_user_call(tmp_path, monkeypatch):
    """AC1: github_access = get_token + GET /user: 200 -> ok, 401 -> fail with the fix,
    and the token value never leaks into the returned strings."""
    from swarmforge import github

    class Resp:
        def __init__(self, status_code):
            self.status_code = status_code

    cfg = parse_config(base_raw(tmp_path / "work"))
    monkeypatch.setattr(github, "get_token", lambda gh_cfg: "ghp_sentinel_token")
    seen: dict = {}

    def fake_get(url, headers=None, timeout=None):
        seen["url"] = url
        seen["auth"] = (headers or {}).get("Authorization", "")
        return Resp(200)

    monkeypatch.setattr(settings.httpx, "get", fake_get)
    ok, fix = settings.github_access(cfg)
    assert ok is True and fix == ""
    assert seen["url"].endswith("/user") and "ghp_sentinel_token" in seen["auth"]

    monkeypatch.setattr(settings.httpx, "get", lambda *a, **kw: Resp(401))
    ok, fix = settings.github_access(cfg)
    assert ok is False
    assert "401" in fix
    assert "ghp_sentinel_token" not in fix


def test_health_section_renders_one_row_per_check(tmp_path, monkeypatch):
    """AC1: the rendered page shows every canonical row (green when clean)."""
    _, at = _open_settings(
        tmp_path, monkeypatch,
        get_health_checks=settings.get_health_checks,
        environment_check_results=lambda cfg: [],
        github_access=lambda cfg: (True, ""),
    )
    md = " ".join(m.value for m in at.markdown)
    for want in ("Provider API keys", "Provider base_url", "GitHub CLI", "Git",
                 "Docker running", "Sandbox image built", "GitHub access"):
        assert f"✅ **{want}**" in md, (want, md)


# ------------------------------------------------------------------ AC4
def test_test_models_caps_busy_wait_and_reports_usage(monkeypatch, tmp_path):
    """AC4: settings.test_models sends the tiny tool-calling request per role with
    busy_wait_s capped, returning ok/tokens/cost rows."""
    from types import SimpleNamespace

    from swarmforge import llm

    captured: dict = {}

    class FakeLLM:
        def __init__(self, config, budget=None, max_retries=None, busy_wait_s=None, **kw):
            captured["busy_wait_s"] = busy_wait_s
            captured["max_retries"] = max_retries

        async def chat(self, role, messages, tools=None, **kw):
            captured.setdefault("calls", []).append((role, tools))
            return SimpleNamespace(
                tool_calls=[{"function": {"name": "report_status",
                                          "arguments": '{"status": "ready"}'}}],
                content="",
                usage=SimpleNamespace(total_tokens=120, cost_usd=0.0005,
                                      cost_source="pricing table"))

    monkeypatch.setattr(llm, "LLMClient", FakeLLM)
    cfg = parse_config(base_raw(tmp_path / "work"))
    rows = settings.test_models(cfg, roles=["planner", "reviewer"])
    assert [r["role"] for r in rows] == ["planner", "reviewer"]
    assert all(r["ok"] for r in rows)
    assert rows[0]["total_tokens"] == 120 and rows[0]["cost_usd"] == 0.0005
    assert rows[0]["cost_source"] == "pricing table"
    assert captured["busy_wait_s"] is not None and captured["busy_wait_s"] <= 5.0
    # same tiny tool-calling request as checks.run_checks(live=True)
    role, tools = captured["calls"][0]
    assert role == "planner" and tools[0]["function"]["name"] == "report_status"


def test_test_models_button_shows_fail_and_cost_unavailable(tmp_path, monkeypatch):
    """AC4: the Test models button renders OK and fail rows with tokens/cost per role."""
    def fake_test_models(cfg, roles=None):
        return [
            {"role": "planner", "model": "qwen-fake", "ok": True, "detail": "ready",
             "total_tokens": 42, "cost_usd": 0.001234, "cost_source": "pricing table"},
            {"role": "reviewer", "model": "glm-fake", "ok": False,
             "detail": "HTTP 500 boom", "total_tokens": None, "cost_usd": None,
             "cost_source": "unavailable"},
        ]

    _, at = _open_settings(tmp_path, monkeypatch, test_models=fake_test_models)
    next(b for b in at.button if b.label == "Test models").click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    md = " ".join(m.value for m in at.markdown)
    assert "✅ planner (qwen-fake)" in md and "tokens=42" in md and "$0.001234" in md
    assert "❌ reviewer (glm-fake): HTTP 500 boom" in md
    assert "cost unavailable" in md


# ------------------------------------------------------------------ AC5
def test_model_selectbox_populated_when_provider_reachable(tmp_path, monkeypatch):
    """AC5: when provider_models is reachable the role rows use a selectbox with that model
    list (no free-text box); when unreachable a free-text entry is offered instead."""
    _, at = _open_settings(tmp_path, monkeypatch, provider_models=lambda cfg, name: MODELS)
    cfg_models = {"planner": "qwen-fake", "developer_backend": "deepseek-fake",
                  "developer_frontend": "kimi-fake", "reviewer": "glm-fake",
                  "tester": "minimax-fake"}
    for role, current in cfg_models.items():
        sb = at.selectbox(key=f"set_model_{role}")
        assert list(sb.options) == MODELS
        assert sb.value == current  # current model preselected
    free_keys = [e.key for e in at.text_input]
    assert not any(k.startswith("set_model_free_") for k in free_keys), free_keys

    _, at2 = _open_settings(tmp_path, monkeypatch, provider_models=lambda cfg, name: None)
    assert at2.text_input(key="set_model_free_planner") is not None
    assert list(at2.selectbox(key="set_model_planner").options) == ["qwen-fake"]


def test_save_persists_model_temp_steps_and_limits(tmp_path, monkeypatch):
    """AC5+AC2: editing model/temperature/max_steps per role and all four limits writes the
    new values into swarmforge.yaml (after a byte-identical .bak) — keys never appear."""
    p, at = _open_settings(tmp_path, monkeypatch, provider_models=lambda cfg, name: MODELS)
    before = p.read_bytes()

    at.selectbox(key="set_model_reviewer").select("minimax-fake")
    at.number_input(key="set_temp_planner").set_value(0.7)
    at.number_input(key="set_steps_planner").set_value(22)
    at.number_input(key="set_budget").set_value(8.0)
    at.number_input(key="set_runtime").set_value(7200)
    at.number_input(key="set_agents").set_value(4)
    at.number_input(key="set_cycles").set_value(5)
    next(b for b in at.button if b.label == "Save settings").click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert not any("same model" in e.value or "share model" in e.value for e in at.error), \
        [e.value for e in at.error]

    saved = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert saved["roles"]["reviewer"]["model"] == "minimax-fake"
    assert saved["roles"]["planner"]["temperature"] == pytest.approx(0.7)
    assert saved["roles"]["planner"]["max_steps"] == 22
    assert saved["limits"]["budget_usd"] == pytest.approx(8.0)
    assert saved["limits"]["max_runtime_s"] == 7200
    assert saved["limits"]["max_concurrent_agents"] == 4
    assert saved["limits"]["max_repair_cycles"] == 5
    # untouched roles keep their models; providers section preserved
    assert saved["roles"]["developer_backend"]["model"] == "deepseek-fake"
    assert saved["providers"] == yaml.safe_load(before.decode())["providers"]
    bak = p.with_name(p.name + ".bak")
    assert bak.read_bytes() == before
    # AC3: no key value in the rendered page or the saved file
    text = " ".join(str(m.value) for m in at.markdown)
    assert "sentinel-secret-xyz" not in text
    assert "sentinel-secret-xyz" not in p.read_text(encoding="utf-8")


# ------------------------------------------------------------------ AC3
def test_api_key_row_shows_set_indicator_not_the_value(tmp_path, monkeypatch):
    """AC3: a provider row shows provider name + env var name + 'set' — never the value."""
    _, at = _open_settings(tmp_path, monkeypatch)
    md = " ".join(m.value for m in at.markdown)
    assert "Provider **fake**" in md
    assert "FAKE_KEY" in md
    assert "set ✅" in md
    assert "MISSING" not in md  # the key IS set in this scenario
    assert "sentinel-secret-xyz" not in md


def test_api_key_row_shows_missing_when_env_unset(tmp_path, monkeypatch):
    """AC3: with the env var unset the row flips to the not-set indicator."""
    _, at = _open_settings(tmp_path, monkeypatch, unset_key=True)
    md = " ".join(m.value for m in at.markdown)
    assert "FAKE_KEY" in md
    assert "MISSING ❌" in md


# ------------------------------------------------------------------ AC4
def test_recheck_button_reruns_health_checks(tmp_path, monkeypatch):
    """AC4: 'Re-check' re-runs the health checks (get_health_checks called again)."""
    calls: list[int] = []

    def counting(cfg):
        calls.append(1)
        return []

    _, at = _open_settings(tmp_path, monkeypatch, get_health_checks=counting)
    before = len(calls)
    assert before >= 1
    next(b for b in at.button if b.label == "Re-check").click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(calls) > before, "Re-check must re-run the environment/GitHub checks"


def test_test_models_sends_the_same_request_as_checks_live(tmp_path, monkeypatch):
    """AC4: settings.test_models sends the identical tiny tool-calling request per role as
    checks.run_checks(live=True): same system/user messages and same tool definition."""
    import asyncio
    import copy as _copy
    from types import SimpleNamespace

    from swarmforge import checks, llm

    captured: dict[str, list] = {"settings": [], "checks": []}

    def make_fake(bucket: list):
        class FakeLLM:
            def __init__(self, config, budget=None, max_retries=None, busy_wait_s=None, **kw):
                bucket.append({"init_busy_wait_s": busy_wait_s})

            async def chat(self, role, messages, tools=None, **kw):
                bucket.append({"role": role, "messages": _copy.deepcopy(messages),
                               "tools": _copy.deepcopy(tools)})
                return SimpleNamespace(
                    tool_calls=[{"function": {"name": "report_status",
                                              "arguments": '{"status": "ready"}'}}],
                    content="", usage=SimpleNamespace(total_tokens=7, cost_usd=0.0,
                                                      cost_source="test"))
        return FakeLLM

    # settings.test_models imports LLMClient from swarmforge.llm at call time;
    # checks.py holds a module-level reference — patch both namespaces.
    monkeypatch.setattr(llm, "LLMClient", make_fake(captured["settings"]))
    monkeypatch.setattr(checks, "LLMClient", make_fake(captured["checks"]))
    monkeypatch.setattr(checks, "get_token", lambda gh_cfg: None)  # no network in run_checks
    monkeypatch.setenv("FAKE_KEY", "test-key")
    cfg = parse_config(base_raw(tmp_path / "work"))

    rows = settings.test_models(cfg, roles=["planner", "reviewer"])
    assert [r["role"] for r in rows] == ["planner", "reviewer"]
    assert all(r["ok"] for r in rows)

    asyncio.run(checks.run_checks(cfg, live=True, roles=["planner", "reviewer"]))

    req_settings = [c for c in captured["settings"] if "messages" in c]
    req_checks = [c for c in captured["checks"] if "messages" in c]
    assert req_settings and len(req_settings) == len(req_checks) == 2
    assert req_settings == req_checks, (
        "test_models must send the same tiny tool-calling request as run_checks(live=True)")
    # busy_wait_s capped in the live test (checks.run_checks uses the client default)
    init = next(c for c in captured["settings"] if "init_busy_wait_s" in c)
    assert init["init_busy_wait_s"] is not None and init["init_busy_wait_s"] <= 5.0


# ------------------------------------------------------------------ AC5
def test_model_selectbox_accepts_new_options_when_reachable(tmp_path, monkeypatch):
    """AC5: with a reachable provider the model selectbox is populated from provider_models
    AND accepts free text (accept_new_options=True)."""
    _, at = _open_settings(tmp_path, monkeypatch, provider_models=lambda cfg, name: MODELS)
    for role in ("planner", "developer_backend", "developer_frontend", "reviewer", "tester"):
        sb = at.selectbox(key=f"set_model_{role}")
        assert list(sb.options) == MODELS
        assert sb.proto.accept_new_options is True, role
