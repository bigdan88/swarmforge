import time

from swarmforge.config import ConfigError, SelfHealConfig, parse_config
from swarmforge.db import Store
from swarmforge.postmortem import escalate, heal

from .conftest import base_raw


async def test_escalate_time_cap_clamps(cfg):
    cfg.limits.max_runtime_s = 3600
    c = escalate(cfg, 2, time_cap=1800)
    assert c.limits.max_runtime_s == 1800  # escalated would be int(3600 * 1.5**2) = 8100
    assert all(r.timeout_s <= 1800 for r in c.roles.values())
    assert escalate(cfg, 2, category="model_unavailable", time_cap=1800).limits.max_runtime_s == 1800
    assert escalate(cfg, 2).limits.max_runtime_s == 8100


async def test_heal_time_cap_blocks_attempt(cfg):
    store = Store(cfg.db_path)
    now = time.time()
    store.create_run("r-time", budget_usd=5, max_runtime_s=3600, state="FAILED",
                     state_reason="maximum runtime exceeded")
    store.update_run("r-time", created_at=now - 40000, finished_at=now - 30000)
    store.failure_save("r-time", category="time_limit", retry=1, root_cause="x", fix="y")
    cfg.self_heal.max_attempts = 2
    cfg.self_heal.max_total_runtime_s = 3600  # tcap 3600, elapsed 10000 -> left < 600
    launched = []

    async def launch(c, seed, ctx, heal_of, attempt):
        launched.append(c)
        return "unused"

    await heal(cfg, store, "r-time", launch)
    assert launched == []
    assert "not retried: self-healing time cap 1.0h reached" in store.failure("r-time")["outcome"]


async def test_heal_time_cap_default_is_3x_first_run(cfg):
    store = Store(cfg.db_path)
    now = time.time()
    cfg.self_heal.max_total_runtime_s = 0  # default: 3x the first run's max_runtime_s
    cfg.self_heal.max_attempts = 2
    cfg.limits.max_runtime_s = 3600

    # blocked: elapsed 10300 of tcap 10800 -> 500 left < 600
    store.create_run("r-block", budget_usd=5, max_runtime_s=3600, state="FAILED",
                     state_reason="maximum runtime exceeded")
    store.update_run("r-block", created_at=now - 10400, finished_at=now - 100)
    store.failure_save("r-block", category="time_limit", retry=1, root_cause="x", fix="y")

    async def noop(c, seed, ctx, heal_of, attempt):
        raise AssertionError("must not launch")

    await heal(cfg, store, "r-block", noop)
    assert "not retried: self-healing time cap 3.0h reached" in store.failure("r-block")["outcome"]

    # allowed: elapsed 10000 -> 800 left >= 600, but the escalated runtime is clamped to 800
    store.create_run("r-ok", budget_usd=5, max_runtime_s=3600, state="FAILED",
                     state_reason="maximum runtime exceeded")
    store.update_run("r-ok", created_at=now - 10100, finished_at=now - 100)
    store.failure_save("r-ok", category="time_limit", retry=1, root_cause="x", fix="y")
    seen = []

    async def launch(c, seed, ctx, heal_of, attempt):
        seen.append(c)
        store.create_run("r-next", budget_usd=5, max_runtime_s=c.limits.max_runtime_s, state="COMPLETED")
        return "r-next"

    await heal(cfg, store, "r-ok", launch)
    assert len(seen) == 1
    assert seen[0].limits.max_runtime_s == 800  # escalated 5400 clamped to the 800 s left
    events = [e for e in store.events("r-ok") if e["kind"] == "selfheal"]
    assert events and "clamped" in events[0]["message"]


def test_config_validation_max_total_runtime_s(tmp_path):
    raw = base_raw(tmp_path / "work")
    raw["self_heal"]["max_total_runtime_s"] = -1
    try:
        parse_config(raw)
        raise AssertionError("expected ConfigError")
    except ConfigError as e:
        assert "max_total_runtime_s" in str(e)
    assert SelfHealConfig().max_total_runtime_s == 0
