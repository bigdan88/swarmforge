"""Thinking level (reasoning_effort): config default, per-role override, sent to the provider."""
import pytest

from swarmforge.config import ConfigError, parse_config
from swarmforge.llm import Budget, LLMClient
from swarmforge.variants import validate_overlay

from .conftest import FakeProvider, base_raw


async def _sent(cfg, role):
    fake = FakeProvider({m: (lambda body: {"content": "ok"}) for m in
                         ("qwen-fake", "deepseek-fake", "kimi-fake", "glm-fake", "minimax-fake")})
    llm = LLMClient(cfg, None, None, Budget(5), transport=fake.transport)
    await llm.chat(role, [{"role": "user", "content": "hi"}])
    return fake.requests[-1]


async def test_default_and_per_role_effort_are_sent(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path)
    raw["reasoning_effort"] = "medium"
    raw["roles"]["reviewer"]["reasoning_effort"] = "high"
    raw["roles"]["tester"]["extra_body"] = {"reasoning_effort": "low"}
    cfg = parse_config(raw)
    assert (await _sent(cfg, "planner"))["reasoning_effort"] == "medium"
    assert (await _sent(cfg, "reviewer"))["reasoning_effort"] == "high"
    assert (await _sent(cfg, "tester"))["reasoning_effort"] == "low"     # explicit extra_body wins


async def test_no_effort_configured_sends_nothing(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    assert "reasoning_effort" not in await _sent(cfg, "planner")


def test_invalid_effort_rejected_and_variants_can_tune_it(tmp_path, cfg):
    raw = base_raw(tmp_path)
    raw["reasoning_effort"] = "max"
    with pytest.raises(ConfigError, match="reasoning_effort must be one of"):
        parse_config(raw)
    assert validate_overlay({"roles": {"planner": {"reasoning_effort": "low"}}}, cfg) == []
    assert validate_overlay({"roles": {"planner": {"reasoning_effort": "turbo"}}}, cfg)
