"""Unit tests for postmortem.heal_chain / chain_usage (chain time and budget accounting)."""

from pathlib import Path

from swarmforge.config import parse_config
from swarmforge.db import Store
from swarmforge.postmortem import chain_usage, heal_chain

from .conftest import base_raw


def _cfg(tmp_path: Path, **self_heal):
    raw = base_raw(tmp_path)
    raw["self_heal"] = {"enabled": True, **self_heal}
    return parse_config(raw)


def test_single_run_no_links(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    cfg = _cfg(tmp_path)
    store.create_run("A", state="FAILED", budget_usd=10.0)
    assert heal_chain(store, "A") == ["A"]
    u = chain_usage(store, cfg, "A")
    assert u["chain"] == ["A"]
    assert u["attempts"] == 0
    assert u["runtime_cap_s"] == cfg.limits.max_runtime_s
    assert u["budget_cap_usd"] == 3 * 10.0
    assert u["cost_usd"] == 0.0
    assert u["elapsed_s"] == 0.0  # no failure row, no finished_at


def test_two_run_chain_mixed_sources(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    cfg = _cfg(tmp_path)
    t = 1000.0
    store.create_run(
        "A",
        state="FAILED",
        budget_usd=10.0,
        max_runtime_s=7200,
        created_at=t,
        updated_at=t,
        finished_at=t + 60,
    )
    store.create_run(
        "B",
        state="COMPLETED",
        heal_of="A",
        heal_attempt=1,
        created_at=t,
        updated_at=t,
        finished_at=t + 3600,
    )
    store.failure_save("A", heal_run_id="B", cost_usd=0.25, elapsed_s=120.0)
    store.record_llm_call("B", cost_usd=0.09, cost_source="provider_reported")

    assert heal_chain(store, "B") == ["A", "B"]
    u = chain_usage(store, cfg, "B")
    assert u["chain"] == ["A", "B"]
    assert u["attempts"] == 1
    assert (
        u["elapsed_s"] == 120.0 + 3600.0
    )  # failure elapsed_s + B finished_at - created_at
    assert u["runtime_cap_s"] == 7200 + cfg.limits.max_runtime_s
    assert u["cost_usd"] == 0.25 + 0.09
    assert u["budget_cap_usd"] == 3 * 10.0


def test_explicit_total_budget_cap(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    cfg = _cfg(tmp_path, max_total_budget_usd=60.0)
    store.create_run("A", state="FAILED", budget_usd=10.0)
    u = chain_usage(store, cfg, "A")
    assert u["budget_cap_usd"] == 60.0


def test_forward_only_chain(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    cfg = _cfg(tmp_path)
    store.create_run("A", state="FAILED")
    store.create_run("B", state="FAILED")
    store.create_run("C", state="COMPLETED")
    store.failure_save("A", heal_run_id="B")
    store.failure_save("B", heal_run_id="C")
    assert heal_chain(store, "A") == ["A", "B", "C"]
    assert chain_usage(store, cfg, "A")["attempts"] == 2


def test_missing_rows_do_not_raise(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    cfg = _cfg(tmp_path)
    store.create_run("A", state="FAILED")
    store.failure_save("A", heal_run_id="ghost")  # heal target has no run row
    assert heal_chain(store, "A") == ["A", "ghost"]
    u = chain_usage(store, cfg, "A")
    assert u["attempts"] == 1
    assert u["runtime_cap_s"] == 2 * cfg.limits.max_runtime_s
    assert u["budget_cap_usd"] == 3 * cfg.limits.budget_usd  # head budget_usd is NULL


def test_cycle_safe(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    cfg = _cfg(tmp_path)
    store.create_run("A", state="FAILED", heal_of="B")
    store.create_run("B", state="FAILED", heal_of="A")
    store.failure_save("A", heal_run_id="B")
    store.failure_save("B", heal_run_id="A")
    chain = heal_chain(store, "A")
    assert len(chain) <= 12 and set(chain) == {"A", "B"}
    chain_usage(store, cfg, "A")  # must not raise or loop forever
