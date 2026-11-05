"""Acceptance: Failures page shows heal-chain time and budget usage (AC1/AC2/AC3).

Complements tests/test_failures_chain_page.py (2-run chain + single attempt) with:
- a 3-run chain accumulating elapsed/cost across all attempts, with an explicit
  self_heal.max_total_budget_usd cap, while every pre-existing failures-page section
  (metrics, chart, table, cause, root cause, fix, lesson, evidence, buttons) still renders;
- a no-chain failure showing a single-attempt line built from its own elapsed and caps;
- a unit check that the chain is found from any member and the 3x-first-budget fallback cap.
"""

import time
from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from swarmforge.config import load_config, parse_config  # noqa: E402
from swarmforge.db import Store  # noqa: E402
from swarmforge.postmortem import chain_usage, heal_chain  # noqa: E402

from .conftest import base_raw  # noqa: E402
from .test_dashboard import APP, _go  # noqa: E402

A = "20990101-000000-aaaaaa"
B = "20990101-010000-bbbbbb"
C = "20990101-020000-cccccc"


def _write_cfg(tmp_path, monkeypatch, **self_heal):
    """Like test_dashboard._write_cfg but with self-heal chain settings added."""
    import streamlit as st

    st.cache_resource.clear()  # the dashboard caches config/store per process
    raw = base_raw(tmp_path / "work")
    raw["self_heal"] = {"enabled": True, "max_attempts": 2, **self_heal}
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    monkeypatch.setenv("SWARMFORGE_CONFIG", str(p))
    monkeypatch.setenv("SWARMFORGE_DASH_AUTOREFRESH", "0")
    monkeypatch.setenv("SWARMFORGE_DASH_UPDATE_CHECK", "0")
    monkeypatch.delenv("SWARMFORGE_DB", raising=False)
    return load_config(str(p))


def _fail(store, rid, **over):
    fields = dict(title="Chain task", repo="x/y", state="FAILED", reason="maximum runtime exceeded",
                  category="time_limit", root_cause=f"root {rid}", fix=f"fix {rid}",
                  lesson=f"lesson {rid}", retry=0, diagnosed_by="planner/qwen", evidence=f"ev {rid}")
    fields.update(over)
    store.failure_save(rid, **fields)


def test_failures_page_three_run_chain_with_explicit_cap(tmp_path, monkeypatch):
    """AC1: a healed failure linked to two retries shows accumulated chain elapsed/cost vs the
    chain caps (explicit max_total_budget_usd here), and every pre-existing section still renders."""
    cfg = _write_cfg(tmp_path, monkeypatch, max_total_budget_usd=60.0)
    store = Store(cfg.db_path)
    t = time.time()
    store.create_run(A, state="FAILED", issue_title="Chain task", repo="x/y", budget_usd=20,
                     max_runtime_s=7200, state_reason="maximum runtime exceeded", config_json="{}",
                     created_at=t - 4 * 3600, finished_at=t - 4 * 3600 + 7200)
    store.create_run(B, state="FAILED", issue_title="Chain task", repo="x/y", budget_usd=30,
                     max_runtime_s=10800, state_reason="maximum runtime exceeded", config_json="{}",
                     heal_of=A, heal_attempt=1, created_at=t - 2 * 3600, finished_at=t - 2 * 3600 + 3600)
    store.create_run(C, state="COMPLETED", issue_title="Chain task", repo="x/y", budget_usd=45,
                     config_json="{}", heal_of=B, heal_attempt=2,
                     created_at=t - 1800, finished_at=t)
    store.record_llm_call(C, cost_usd=0.05, cost_source="provider_reported")
    # B's post-mortem first, then A's, so A is the newest failure the page auto-selects.
    _fail(store, B, heal_run_id=C, outcome="fixed itself on attempt 2", retry=1,
          cost_usd=0.10, elapsed_s=3600.0)
    _fail(store, A, heal_run_id=B, outcome="fixed itself on attempt 1", retry=1,
          cost_usd=0.24, elapsed_s=7200.0)

    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    _go(at, "Failures")
    assert not at.exception, [e.value for e in at.exception]

    texts = [m.value for m in at.markdown]
    # chain: 7200 + 3600 (failure rows) + 1800 (C run row) = 12600s = 3.5h;
    # caps: 7200 + 10800 + cfg default (C has no max_runtime_s); cost 0.24 + 0.10 + 0.05 = $0.39;
    # budget cap: explicit self_heal.max_total_budget_usd = $60.
    elapsed_h = (7200 + 3600 + 1800) / 3600
    cap_h = (7200 + 10800 + cfg.limits.max_runtime_s) / 3600
    assert any(
        f"**chain:** {elapsed_h:.1f}h / {cap_h:.0f}h" in txt and "$0.39" in txt and "$60" in txt
        for txt in texts
    ), texts

    # --- pre-existing sections unchanged (AC2) ---
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Failures"] == "2" and metrics["Fixed themselves"] == "2"
    assert at.get("vega_lite_chart")  # cause chart
    assert at.dataframe  # failures table
    assert any("**Cause:**" in txt and "**What happened next:** fixed itself on attempt 1" in txt
               for txt in texts)
    assert any(f"root {A}" in txt for txt in texts)        # root cause
    assert any(f"fix {A}" in txt for txt in texts)         # what to do differently
    assert any(f"lesson {A}" in txt for txt in texts)      # lesson
    assert any(e.label == "Evidence the diagnosis was based on" for e in at.expander)
    labels = [b.label for b in at.button]
    assert "Open this run" in labels and "Open the retry" in labels


def test_failures_page_no_chain_single_attempt_line(tmp_path, monkeypatch):
    """AC2: a failure without a heal chain renders a single-attempt line from its own elapsed
    and caps; no 'chain:' text appears; the old sections are unchanged."""
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    store.create_run(A, state="FAILED", issue_title="Broken build", repo="x/y", budget_usd=10,
                     max_runtime_s=3600, state_reason="tests failed", config_json="{}")
    _fail(store, A, title="Broken build", reason="tests failed", category="test_failure",
          root_cause="A flaky test failed the build.", fix="Re-run the flaky test.",
          lesson="Quarantine flaky tests.", outcome="not retried", cost_usd=1.5, elapsed_s=7200.0)

    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    _go(at, "Failures")
    assert not at.exception, [e.value for e in at.exception]

    texts = [m.value for m in at.markdown]
    # own elapsed 7200s = 2.0h / own max_runtime_s 3600s = 1h; cost $1.50 / 3 x budget_usd 10 = $30.
    assert any("**attempt:** 2.0h / 1h" in txt and "$1.50 / $30" in txt for txt in texts), texts
    assert not any("chain:" in txt for txt in texts)

    # pre-existing sections still render
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Failures"] == "1" and metrics["Need you"] == "1"
    assert at.get("vega_lite_chart") and at.dataframe
    assert any("**Cause:**" in txt for txt in texts)
    assert any("A flaky test failed the build." in txt for txt in texts)
    assert any("Quarantine flaky tests." in txt for txt in texts)
    assert any(e.label == "Evidence the diagnosis was based on" for e in at.expander)
    labels = [b.label for b in at.button]
    assert "Open this run" in labels and "Open the retry" not in labels


def _unit_cfg(tmp_path: Path) -> "object":
    raw = base_raw(tmp_path)
    raw["self_heal"] = {"enabled": True, "max_attempts": 2}
    return parse_config(raw)


def test_chain_found_from_middle_and_3x_head_budget_cap(tmp_path):
    """AC1 accounting: the chain is located from any member; with max_total_budget_usd=0 the cap
    is 3x the first run's budget (not the queried member's)."""
    store = Store(tmp_path / "db.sqlite")
    cfg = _unit_cfg(tmp_path)
    store.create_run(A, state="FAILED", budget_usd=10.0)
    store.create_run(B, state="FAILED", heal_of=A, heal_attempt=1)
    store.create_run(C, state="COMPLETED", heal_of=B, heal_attempt=2)
    store.failure_save(A, heal_run_id=B)
    store.failure_save(B, heal_run_id=C)
    for rid in (A, B, C):
        assert heal_chain(store, rid) == [A, B, C]
        u = chain_usage(store, cfg, rid)
        assert u["attempts"] == 2
        assert u["budget_cap_usd"] == 3 * 10.0  # head's budget, even when queried from C
