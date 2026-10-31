"""OpenAI-compatible chat client (DeepInfra by default) with usage/cost telemetry.

Cost accounting distinguishes three sources, recorded per call:
  * provider_reported - the provider returned a cost in the response (DeepInfra returns
    `usage.estimated_cost`, which is its own billing computation for the call).
  * estimated         - computed by SwarmForge from token counts x a price table (fetched
    from the provider's model listing or set manually in the config).
  * unavailable       - neither cost nor prices are known; the call still counts tokens if
    the provider reported them.
"""
from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Config, ProviderConfig, RoleConfig


class LLMError(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    cost_usd: float | None = None
    cost_source: str = "unavailable"


@dataclass
class ChatResult:
    message: dict
    finish_reason: str | None
    usage: Usage
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def content(self) -> str:
        return self.message.get("content") or ""

    @property
    def tool_calls(self) -> list[dict]:
        return self.message.get("tool_calls") or []


class Budget:
    """Thread/async-safe running total of spend for one run."""

    def __init__(self, limit_usd: float, spent_usd: float = 0.0):
        self.limit = limit_usd
        self.spent = spent_usd
        self.unpriced_calls = 0
        self._lock = threading.Lock()

    def check(self) -> None:
        with self._lock:
            if self.spent >= self.limit:
                raise BudgetExceeded(f"budget ${self.limit:.2f} exhausted (spent ${self.spent:.4f})")

    def add(self, usage: Usage) -> None:
        with self._lock:
            if usage.cost_usd is not None:
                self.spent += usage.cost_usd
            else:
                self.unpriced_calls += 1

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit - self.spent)


class PriceTable:
    """USD per 1M tokens for (provider, model). Fetched lazily from the provider."""

    def __init__(self):
        self._prices: dict[tuple[str, str], dict[str, float]] = {}
        self._fetched: set[str] = set()
        self._lock = asyncio.Lock()

    async def get(self, provider: ProviderConfig, role: RoleConfig) -> dict[str, float] | None:
        if role.price_in_per_mtok is not None and role.price_out_per_mtok is not None:
            return {"in": role.price_in_per_mtok, "out": role.price_out_per_mtok, "cached": None}
        async with self._lock:
            if provider.name not in self._fetched and provider.pricing_url:
                self._fetched.add(provider.name)
                try:
                    await self._fetch(provider)
                except Exception:  # pricing is best-effort
                    pass
        return self._prices.get((provider.name, role.model))

    async def _fetch(self, provider: ProviderConfig) -> None:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(provider.pricing_url)
            r.raise_for_status()
            for m in r.json().get("data", []):
                p = (m.get("metadata") or {}).get("pricing") or {}
                if "input_tokens" in p and "output_tokens" in p:
                    self._prices[(provider.name, m["id"])] = {
                        "in": float(p["input_tokens"]),
                        "out": float(p["output_tokens"]),
                        "cached": float(p["cache_read_tokens"]) if p.get("cache_read_tokens") else None,
                    }

    def known(self, provider: str, model: str) -> dict | None:
        return self._prices.get((provider, model))


def parse_usage(raw_usage: dict | None, prices: dict | None) -> Usage:
    u = Usage()
    if not raw_usage:
        return u
    u.prompt_tokens = raw_usage.get("prompt_tokens")
    u.completion_tokens = raw_usage.get("completion_tokens")
    u.total_tokens = raw_usage.get("total_tokens")
    details = raw_usage.get("prompt_tokens_details") or {}
    u.cached_tokens = details.get("cached_tokens")
    if raw_usage.get("estimated_cost") is not None:
        u.cost_usd = float(raw_usage["estimated_cost"])
        u.cost_source = "provider_reported"
    elif prices and u.prompt_tokens is not None and u.completion_tokens is not None:
        cached = u.cached_tokens or 0
        cached_price = prices.get("cached") if prices.get("cached") is not None else prices["in"]
        u.cost_usd = (
            (u.prompt_tokens - cached) * prices["in"]
            + cached * cached_price
            + u.completion_tokens * prices["out"]
        ) / 1_000_000
        u.cost_source = "estimated"
    return u


RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class LLMClient:
    def __init__(self, config: Config, store=None, run_id: str | None = None,
                 budget: Budget | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 max_retries: int = 4):
        self.config = config
        self.store = store
        self.run_id = run_id
        self.budget = budget or Budget(config.limits.budget_usd)
        self.prices = PriceTable()
        self.max_retries = max_retries
        self._transport = transport

    async def chat(self, role: str, messages: list[dict], *, tools: list[dict] | None = None,
                   task_key: str | None = None, tool_choice: Any = None) -> ChatResult:
        rc = self.config.role(role)
        pc = self.config.providers[rc.provider]
        key = pc.api_key()
        if not key:
            raise LLMError(f"environment variable {pc.api_key_env} is not set (provider {pc.name})")
        self.budget.check()

        body: dict[str, Any] = {
            "model": rc.model,
            "messages": messages,
            "temperature": rc.temperature,
            "max_tokens": rc.max_output_tokens,
            **rc.extra_body,
        }
        if tools:
            body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   **pc.extra_headers}
        url = pc.base_url.rstrip("/") + "/chat/completions"

        started = time.time()
        last_err: str | None = None
        data: dict | None = None
        async with httpx.AsyncClient(timeout=httpx.Timeout(rc.timeout_s, connect=30),
                                     transport=self._transport) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    r = await client.post(url, headers=headers, json=body)
                    if r.status_code in RETRY_STATUS and attempt < self.max_retries:
                        last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                        await asyncio.sleep(_backoff(attempt, r))
                        continue
                    if r.status_code >= 400:
                        last_err = f"HTTP {r.status_code}: {r.text[:1000]}"
                        break
                    data = r.json()
                    break
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    last_err = f"{type(e).__name__}: {e}"
                    if attempt < self.max_retries:
                        await asyncio.sleep(_backoff(attempt))
                        continue
                    break

        if data is None or not data.get("choices"):
            if data is not None:
                last_err = f"no choices in response: {json.dumps(data)[:500]}"
            self._record(role, rc, pc, task_key, started, Usage(), "error", last_err)
            raise LLMError(f"{role} ({rc.model}) call failed: {last_err}")

        prices = await self.prices.get(pc, rc)
        usage = parse_usage(data.get("usage"), prices)
        self.budget.add(usage)
        choice = data["choices"][0]
        msg = dict(choice.get("message") or {})
        msg.setdefault("role", "assistant")
        self._record(role, rc, pc, task_key, started, usage, "ok", None)
        return ChatResult(message=msg, finish_reason=choice.get("finish_reason"), usage=usage, raw=data)

    def _record(self, role, rc, pc, task_key, started, usage: Usage, status, error):
        if not self.store or not self.run_id:
            return
        self.store.record_llm_call(
            self.run_id, task_key=task_key, role=role, provider=pc.name, model=rc.model,
            started_at=started, ended_at=time.time(), prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens, total_tokens=usage.total_tokens,
            cached_tokens=usage.cached_tokens, cost_usd=usage.cost_usd,
            cost_source=usage.cost_source if status == "ok" else "unavailable",
            status=status, error=error,
        )


def _backoff(attempt: int, resp: httpx.Response | None = None) -> float:
    if resp is not None:
        ra = resp.headers.get("retry-after")
        if ra:
            try:
                return min(60.0, float(ra))
            except ValueError:
                pass
    return min(60.0, (2 ** attempt) + random.random())
