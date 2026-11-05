import time
from pathlib import Path

import yaml

from swarmforge.config import ConfigError, SelfHealConfig, parse_config
from swarmforge.db import Store
from swarmforge.postmortem import escalate, heal

from .conftest import ROOT, base_raw


async def test_escalate_time_cap_clamps_runtime_and_role_timeouts(cfg):
    cfg.limits.max_runtime_s = 3600
    c = escalate(cfg, 2, time_cap=1800)
    assert (
        c.limits.max_runtime_s == 1800
    )  # escalated would be int(3600 * 1.5**2) = 8100
    assert all(r.timeout_s <= 1800 for r in c.roles.values())
    assert (
        escalate(
            cfg, 2, category="model_unavailable", time_cap=1800
        ).limits.max_runtime_s
        == 1800
    )
    assert escalate(cfg, 2).limits.max_runtime_s == 8100


async def test_escalate_model_unavailable_clamps_role_timeouts(cfg):
    """Regression: the model_unavailable early-return path must also clamp role timeouts."""
    mu = escalate(cfg, 1, category="model_unavailable", time_cap=300)
    # 300 is below the base role timeout of 900, so the clamp must actually apply
    assert all(r.timeout_s == 300 for r in mu.roles.values())
    assert mu.limits.max_runtime_s == 300
    # model_unavailable semantics preserved: no steps/time escalation, doubled busy_wait
    assert mu.roles["planner"].max_steps == cfg.roles["planner"].max_steps
    assert mu.limits.busy_wait_s == cfg.limits.busy_wait_s * 2
    assert mu.limits.max_repair_cycles == cfg.limits.max_repair_cycles


async def test_heal_time_cap_blocks_and_records_outcome(cfg):
    store = Store(cfg.db_path)
    now = time.time()
    store.create_run(
        "r-time",
        budget_usd=5,
        max_runtime_s=3600,
        state="FAILED",
        state_reason="maximum runtime exceeded",
    )
    store.update_run("r-time", created_at=now - 40000, finished_at=now - 30000)
    store.failure_save(
        "r-time", category="time_limit", retry=1, root_cause="x", fix="y"
    )
    cfg.self_heal.max_attempts = 2
    cfg.self_heal.max_total_runtime_s = 3600  # tcap 3600, elapsed 10000 -> left < 600
    launched = []

    async def launch(c, seed, ctx, heal_of, attempt):
        launched.append(c)
        return "unused"

    await heal(cfg, store, "r-time", launch)
    assert launched == []
    assert (
        "not retried: self-healing time cap 1.0h reached"
        in store.failure("r-time")["outcome"]
    )


async def test_heal_time_cap_default_is_3x_runtime(cfg):
    store = Store(cfg.db_path)
    now = time.time()
    cfg.self_heal.max_total_runtime_s = 0  # default: 3x the first run's max_runtime_s
    cfg.self_heal.max_attempts = 2
    cfg.limits.max_runtime_s = 3600

    # blocked: elapsed 10300 of tcap 10800 -> 500 left < 600
    store.create_run(
        "r-block",
        budget_usd=5,
        max_runtime_s=3600,
        state="FAILED",
        state_reason="maximum runtime exceeded",
    )
    store.update_run("r-block", created_at=now - 10400, finished_at=now - 100)
    store.failure_save(
        "r-block", category="time_limit", retry=1, root_cause="x", fix="y"
    )

    async def noop(c, seed, ctx, heal_of, attempt):
        raise AssertionError("must not launch")

    await heal(cfg, store, "r-block", noop)
    assert (
        "not retried: self-healing time cap 3.0h reached"
        in store.failure("r-block")["outcome"]
    )

    # allowed: elapsed 10000 -> 800 left >= 600, but the escalated runtime is clamped to 800
    store.create_run(
        "r-ok",
        budget_usd=5,
        max_runtime_s=3600,
        state="FAILED",
        state_reason="maximum runtime exceeded",
    )
    store.update_run("r-ok", created_at=now - 10100, finished_at=now - 100)
    store.failure_save("r-ok", category="time_limit", retry=1, root_cause="x", fix="y")
    seen = []

    async def launch(c, seed, ctx, heal_of, attempt):
        seen.append(c)
        store.create_run(
            "r-next",
            budget_usd=5,
            max_runtime_s=c.limits.max_runtime_s,
            state="COMPLETED",
        )
        return "r-next"

    await heal(cfg, store, "r-ok", launch)
    assert len(seen) == 1
    assert (
        seen[0].limits.max_runtime_s == 800
    )  # escalated 5400 clamped to the 800 s left
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


async def test_heal_echo_reports_time_cap_clamp(cfg):
    store = Store(cfg.db_path)
    now = time.time()
    cfg.limits.max_runtime_s = 3600
    cfg.self_heal.max_total_runtime_s = 0  # default: 3x the first run's max_runtime_s
    cfg.self_heal.max_attempts = 2
    store.create_run(
        "r-echo",
        budget_usd=5,
        max_runtime_s=3600,
        state="FAILED",
        state_reason="maximum runtime exceeded",
    )
    store.update_run(
        "r-echo", created_at=now - 10100, finished_at=now - 100
    )  # elapsed 10000
    store.failure_save(
        "r-echo", category="time_limit", retry=1, root_cause="x", fix="y"
    )

    async def launch(c, seed, ctx, heal_of, attempt):
        store.create_run(
            "r-echo-next",
            budget_usd=5,
            max_runtime_s=c.limits.max_runtime_s,
            state="COMPLETED",
        )
        return "r-echo-next"

    lines = []
    await heal(cfg, store, "r-echo", launch, echo=lines.append)
    assert len(lines) == 1
    echo = lines[0]
    assert "clamped" in echo
    assert "0.2 h" in echo  # the clamped runtime (800 s left)
    assert "3.0 h" in echo  # the chain cap it was clamped against
    events = [e for e in store.events("r-echo") if e["kind"] == "selfheal"]
    assert events and "clamped" in events[0]["message"]


async def test_heal_time_cap_counts_unfinished_chain_run_with_now(cfg):
    """AC1: elapsed uses (finished_at or time.time()) - created_at, so a chain run that never
    recorded finished_at still counts its wall-clock up to now against the cap."""
    store = Store(cfg.db_path)
    now = time.time()
    cfg.limits.max_runtime_s = 3600
    cfg.self_heal.max_attempts = 2
    cfg.self_heal.max_total_runtime_s = 3600  # tcap 1.0 h
    store.create_run(
        "r-alive",
        budget_usd=5,
        max_runtime_s=3600,
        state="FAILED",
        state_reason="maximum runtime exceeded",
    )
    store.update_run(
        "r-alive", created_at=now - 1000, finished_at=now - 900
    )  # elapsed 100
    store.failure_save(
        "r-alive", category="time_limit", retry=1, root_cause="x", fix="y"
    )

    async def launch(c, seed, ctx, heal_of, attempt):
        # the retry FAILED but never recorded finished_at: its elapsed is now - created_at ~ 3000
        store.create_run(
            "r-alive-2",
            budget_usd=5,
            max_runtime_s=c.limits.max_runtime_s,
            state="FAILED",
            state_reason="maximum runtime exceeded",
        )
        store.update_run("r-alive-2", created_at=time.time() - 3000)
        store.failure_save(
            "r-alive-2", category="time_limit", retry=1, root_cause="x", fix="y"
        )
        return "r-alive-2"

    last = await heal(cfg, store, "r-alive", launch)
    assert last == "r-alive-2"
    # chain elapsed ~ 3100 of tcap 3600 -> under 600 left -> the second attempt is blocked
    assert (
        "not retried: self-healing time cap 1.0h reached"
        in store.failure("r-alive-2")["outcome"]
    )
    # the now-based count must not exceed the cap by more than the real seconds that passed
    assert store.failure("r-alive")["outcome"]  # earlier runs get the chain summary


async def test_heal_model_unavailable_attempt_clamps_role_timeouts(cfg):
    """Regression (reviewer finding): heal() must pass time_cap into escalate() for the
    model_unavailable category too, so the launched attempt never gets more wall-clock per role
    than remains in the chain (base role timeout is 900 s)."""
    store = Store(cfg.db_path)
    now = time.time()
    cfg.limits.max_runtime_s = 3600
    cfg.self_heal.max_attempts = 2
    cfg.self_heal.max_total_runtime_s = 3600  # tcap 3600
    store.create_run(
        "r-mu",
        budget_usd=5,
        max_runtime_s=3600,
        state="FAILED",
        state_reason="planner: model busy (HTTP 429)",
    )
    store.update_run(
        "r-mu", created_at=now - 2800, finished_at=now
    )  # elapsed 2800 -> 800 left
    store.failure_save(
        "r-mu", category="model_unavailable", retry=1, root_cause="x", fix="y"
    )
    seen = []

    async def launch(c, seed, ctx, heal_of, attempt):
        seen.append(c)
        store.create_run(
            "r-mu-2",
            budget_usd=5,
            max_runtime_s=c.limits.max_runtime_s,
            state="COMPLETED",
        )
        return "r-mu-2"

    await heal(cfg, store, "r-mu", launch)
    assert len(seen) == 1
    c = seen[0]
    assert (
        c.limits.max_runtime_s == 800
    )  # escalated 5400 clamped to the time left in the chain
    # model_unavailable does not scale roles, but 800 < the base 900 so the clamp must apply
    assert all(r.timeout_s <= 800 for r in c.roles.values())
    assert c.roles["planner"].timeout_s == 800
    # model_unavailable semantics preserved
    assert c.limits.busy_wait_s == cfg.limits.busy_wait_s * 2


async def test_heal_budget_cap_blocks_before_time_cap(cfg):
    """AC4: with BOTH caps exceeded the pre-existing budget-cap behaviour wins (checked first)."""
    store = Store(cfg.db_path)
    now = time.time()
    cfg.self_heal.max_attempts = 2
    cfg.self_heal.max_total_budget_usd = 1.0
    cfg.self_heal.max_total_runtime_s = 3600
    store.create_run(
        "r-both",
        budget_usd=5,
        max_runtime_s=3600,
        state="FAILED",
        state_reason="maximum runtime exceeded",
    )
    store.update_run(
        "r-both", created_at=now - 4000, finished_at=now
    )  # elapsed 4000 > tcap too
    store.failure_save(
        "r-both", category="time_limit", retry=1, root_cause="x", fix="y"
    )
    store.record_llm_call(
        "r-both",
        role="planner",
        model="qwen-fake",
        cost_usd=1.0,
        cost_source="estimated",
    )

    async def launch(c, seed, ctx, heal_of, attempt):
        raise AssertionError("must not launch: budget cap reached first")

    await heal(cfg, store, "r-both", launch)
    outcome = store.failure("r-both")["outcome"]
    assert "not retried: self-healing budget cap $1.00 reached" in outcome
    assert "time cap" not in outcome


def test_example_yaml_documents_max_total_runtime_s():
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
    assert "3x" in section
