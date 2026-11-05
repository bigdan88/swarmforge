"""Acceptance tests for the self-healing wall-clock cap (self_heal.max_total_runtime_s).

Covers the criteria end to end:
- AC1: heal() sums (finished_at or now) - created_at over every run of the chain and refuses to
  launch another attempt when less than 600 s remain, recording the time-cap outcome.
- AC2: escalate(cfg, attempt, time_cap=...) clamps max_runtime_s and every role timeout_s below
  the time_factor-escalated value, including the model_unavailable early-return path.
- AC3: max_total_runtime_s defaults to 0 = 3x the first run's stored max_runtime_s (mirroring the
  budget-cap semantics); validate_config raises ConfigError for negative values.
- AC4: the clamp is visible in the self-heal echo line and in the 'selfheal' store event.
- AC5: documented in swarmforge.example.yaml and the README 'Failures and self-healing' section;
  the existing budget-cap behaviour still blocks retries with its own outcome message.

Companion unit tests (already covering the same feature from the developer side):
tests/test_selfheal_time.py.
"""

from __future__ import annotations

import time
from pathlib import Path

import yaml

from swarmforge.config import ConfigError, parse_config
from swarmforge.db import Store
from swarmforge.postmortem import escalate, heal

from .conftest import ROOT, base_raw


def _failed_run(
    store: Store,
    run_id: str,
    *,
    started_s_ago: float,
    ended_s_ago: float,
    max_runtime_s: int = 3600,
) -> None:
    """A FAILED time_limit run whose chain elapsed is (started_s_ago - ended_s_ago) seconds."""
    now = time.time()
    store.create_run(
        run_id,
        budget_usd=5,
        max_runtime_s=max_runtime_s,
        state="FAILED",
        state_reason="maximum runtime exceeded",
    )
    store.update_run(
        run_id, created_at=now - started_s_ago, finished_at=now - ended_s_ago
    )
    store.failure_save(run_id, category="time_limit", retry=1, root_cause="x", fix="y")


# ------------------------------------------------------------------ AC1
async def test_ac1_heal_blocks_when_chain_elapsed_leaves_under_600s(cfg):
    """Sum over ALL chain run ids: the second iteration must see r1 + r2 elapsed, not just r2."""
    store = Store(cfg.db_path)
    cfg.self_heal.max_attempts = 2
    cfg.self_heal.max_total_runtime_s = 3600  # tcap 1.0 h
    _failed_run(store, "r-a1-first", started_s_ago=1000, ended_s_ago=100)  # elapsed 900
    launched = []

    async def launch(c, seed, ctx, heal_of, attempt):
        launched.append(attempt)
        # the retried run also failed on time after 2500 s -> chain elapsed 3400, only 200 left
        _failed_run(store, "r-a1-second", started_s_ago=2600, ended_s_ago=100)
        return "r-a1-second"

    last = await heal(cfg, store, "r-a1-first", launch)
    assert last == "r-a1-second"
    assert launched == [
        1
    ]  # exactly one attempt launched, then the time cap blocked the next
    outcome = store.failure("r-a1-second")["outcome"]
    assert "not retried: self-healing time cap 1.0h reached" in outcome


# ------------------------------------------------------------------ AC2
async def test_ac2_escalate_time_cap_clamps_roles_and_model_unavailable_path(cfg):
    cfg.limits.max_runtime_s = 3600
    base_role_timeout = cfg.roles["planner"].timeout_s  # 900 by default
    # escalated (no cap): 3600 * 1.5 = 5400; role 900 * 1.5 = 1350
    assert escalate(cfg, 1).limits.max_runtime_s == 5400
    assert escalate(cfg, 1).roles["planner"].timeout_s == int(base_role_timeout * 1.5)
    # with time_cap below the escalated values, both are clamped below them
    c = escalate(cfg, 1, time_cap=1000)
    assert c.limits.max_runtime_s == 1000
    assert all(r.timeout_s <= 1000 for r in c.roles.values())
    assert c.roles["planner"].timeout_s == 1000  # escalated 1350 clamped
    # model_unavailable early-return path: the max_runtime_s clamp still applies and roles are
    # never given more wall-clock than the base config (that path does not scale them)
    mu = escalate(cfg, 1, category="model_unavailable", time_cap=1000)
    assert mu.limits.max_runtime_s == 1000
    assert all(r.timeout_s <= base_role_timeout for r in mu.roles.values())


# ------------------------------------------------------------------ AC3
async def test_ac3_zero_default_means_3x_first_run_stored_runtime(cfg):
    """tcap = 3 x the FIRST run's stored max_runtime_s (not cfg.limits), mirroring the budget cap."""
    store = Store(cfg.db_path)
    cfg.limits.max_runtime_s = (
        3600  # deliberately different from the first run's stored 7200
    )
    cfg.self_heal.max_attempts = 2
    cfg.self_heal.max_total_runtime_s = 0  # default -> 3 * 7200 = 21600 s = 6.0 h
    _failed_run(
        store, "r-a3", started_s_ago=21100, ended_s_ago=0, max_runtime_s=7200
    )  # elapsed 21100

    async def launch(c, seed, ctx, heal_of, attempt):
        raise AssertionError("must not launch: only 500 s of the 6 h chain cap remain")

    await heal(cfg, store, "r-a3", launch)
    assert (
        "not retried: self-healing time cap 6.0h reached"
        in store.failure("r-a3")["outcome"]
    )


def test_ac3_config_default_positive_accepted_negative_rejected(tmp_path):
    assert (
        parse_config(base_raw(tmp_path / "w1")).self_heal.max_total_runtime_s == 0
    )  # default
    raw = base_raw(tmp_path / "w2")
    raw["self_heal"]["max_total_runtime_s"] = 7200
    assert parse_config(raw).self_heal.max_total_runtime_s == 7200
    raw = base_raw(tmp_path / "w3")
    raw["self_heal"]["max_total_runtime_s"] = -1
    try:
        parse_config(raw)
        raise AssertionError("expected ConfigError")
    except ConfigError as e:
        assert "max_total_runtime_s" in str(e)


# ------------------------------------------------------------------ AC4
async def test_ac4_clamp_visible_in_echo_line_and_selfheal_event(cfg):
    store = Store(cfg.db_path)
    cfg.limits.max_runtime_s = 3600
    cfg.self_heal.max_attempts = 2
    cfg.self_heal.max_total_runtime_s = 0  # tcap 3 * 3600 = 10800 s
    _failed_run(
        store, "r-a4", started_s_ago=10100, ended_s_ago=100
    )  # elapsed 10000 -> 800 left

    async def launch(c, seed, ctx, heal_of, attempt):
        store.create_run(
            "r-a4-next",
            budget_usd=5,
            max_runtime_s=c.limits.max_runtime_s,
            state="COMPLETED",
        )
        return "r-a4-next"

    lines = []
    await heal(cfg, store, "r-a4", launch, echo=lines.append)
    assert len(lines) == 1
    echo = lines[0]
    assert "clamped" in echo and "chain time cap" in echo
    assert "0.2 h" in echo  # the clamped runtime shown in the echo line (800 s)
    assert "3.0 h" in echo  # and the chain cap it was clamped against
    events = [e for e in store.events("r-a4") if e["kind"] == "selfheal"]
    assert events and "clamped" in events[0]["message"]


# ------------------------------------------------------------------ AC5
async def test_ac5_budget_cap_still_blocks_and_records_budget_outcome(cfg):
    """Regression: the pre-existing budget cap keeps its behaviour (checked before the time cap)."""
    store = Store(cfg.db_path)
    cfg.self_heal.max_attempts = 2
    cfg.self_heal.max_total_budget_usd = 1.0
    _failed_run(store, "r-a5", started_s_ago=100, ended_s_ago=50)  # plenty of time left
    store.record_llm_call(
        "r-a5", role="planner", model="qwen-fake", cost_usd=1.0, cost_source="estimated"
    )

    async def launch(c, seed, ctx, heal_of, attempt):
        raise AssertionError("must not launch: budget cap reached")

    await heal(cfg, store, "r-a5", launch)
    assert (
        "not retried: self-healing budget cap $1.00 reached"
        in store.failure("r-a5")["outcome"]
    )


def test_ac5_documented_in_example_yaml_and_readme():
    raw = yaml.safe_load((ROOT / "swarmforge.example.yaml").read_text(encoding="utf-8"))
    assert raw["self_heal"]["max_total_runtime_s"] == 0
    yaml_text = (ROOT / "swarmforge.example.yaml").read_text(encoding="utf-8")
    line = next(
        l
        for l in yaml_text.splitlines()
        if l.strip().startswith("max_total_runtime_s:")
    )
    assert "#" in line and "3x" in line  # documented with the default semantics

    readme = (Path(ROOT) / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Failures and self-healing", 1)[1].split("\n## ", 1)[0]
    assert "self_heal.max_total_runtime_s" in section
    assert "3x" in section  # default explained
    assert (
        "budget" in section and "cap" in section
    )  # budget-cap text unchanged in spirit
