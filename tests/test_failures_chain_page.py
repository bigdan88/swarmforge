"""Failures page: heal-chain time and budget usage line."""

import time

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from swarmforge.db import Store

from .test_dashboard import APP, _go, _write_cfg


def test_failures_page_shows_chain_usage(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    store.create_run(
        "20990101-000000-aaaaaa",
        state="FAILED",
        issue_title="Flaky task",
        repo="x/y",
        budget_usd=20,
        max_runtime_s=64800,
        state_reason="maximum runtime exceeded",
        config_json="{}",
    )
    store.create_run(
        "20990101-010000-bbbbbb",
        state="COMPLETED",
        issue_title="Flaky task",
        repo="x/y",
        budget_usd=30,
        config_json="{}",
        heal_of="20990101-000000-aaaaaa",
        heal_attempt=1,
        created_at=time.time() - 3600,
        finished_at=time.time(),
    )
    store.failure_save(
        "20990101-000000-aaaaaa",
        title="Flaky task",
        repo="x/y",
        state="FAILED",
        reason="maximum runtime exceeded",
        category="time_limit",
        root_cause="The tester ran the whole suite repeatedly.",
        fix="Run only the new tests.",
        retry=1,
        diagnosed_by="planner/qwen",
        evidence="...",
        heal_run_id="20990101-010000-bbbbbb",
        outcome="fixed itself on attempt 1",
        cost_usd=0.34,
        elapsed_s=54720,
    )
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    _go(at, "Failures")
    assert not at.exception, [e.value for e in at.exception]
    # the only diagnosed failure is auto-selected; the chain line accumulates both attempts
    cap_h = (64800 + cfg.limits.max_runtime_s) / 3600
    elapsed_h = (54720 + 3600) / 3600
    budget_cap = cfg.self_heal.max_total_budget_usd or 3 * 20
    texts = [m.value for m in at.markdown]
    assert any(
        f"**chain:** {elapsed_h:.1f}h / {cap_h:.0f}h" in t
        and "$0.34" in t
        and f"${budget_cap:.0f}" in t
        for t in texts
    ), texts
    # the rest of the detail section is unchanged
    assert any("The tester ran the whole suite repeatedly." in t for t in texts)
    assert any(b.label == "Open this run" for b in at.button)
    assert any(b.label == "Open the retry" for b in at.button)


def test_failures_page_single_attempt_usage(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    store.create_run(
        "20990101-000000-aaaaaa",
        state="FAILED",
        issue_title="Broken build",
        repo="x/y",
        budget_usd=20,
        state_reason="tests failed",
        config_json="{}",
    )
    store.failure_save(
        "20990101-000000-aaaaaa",
        title="Broken build",
        repo="x/y",
        state="FAILED",
        reason="tests failed",
        category="test_failure",
        root_cause="A flaky test failed the build.",
        fix="Re-run the flaky test.",
        retry=0,
        diagnosed_by="planner/qwen",
        evidence="...",
        outcome="not retried",
        cost_usd=0.12,
        elapsed_s=7200,
    )
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    _go(at, "Failures")
    assert not at.exception, [e.value for e in at.exception]
    texts = [m.value for m in at.markdown]
    cap_h = cfg.limits.max_runtime_s / 3600
    budget_cap = cfg.self_heal.max_total_budget_usd or 3 * 20
    assert any(
        f"**attempt:** 2.0h / {cap_h:.0f}h" in t
        and "$0.12" in t
        and f"${budget_cap:.0f}" in t
        for t in texts
    ), texts
    assert not any("chain:" in t for t in texts)
    # old content still renders
    assert any("A flaky test failed the build." in t for t in texts)
    assert any(b.label == "Open this run" for b in at.button)
