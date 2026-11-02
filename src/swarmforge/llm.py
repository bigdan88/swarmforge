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
import inspect
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

    async def get(self, provider: ProviderConfig, role: RoleConfig, model: str | None = None) -> dict[str, float] | None:
        model = model or role.model
        if model == role.model and role.price_in_per_mtok is not None and role.price_out_per_mtok is not None:
            return {"in": role.price_in_per_mtok, "out": role.price_out_per_mtok, "cached": None}
        async with self._lock:
            if provider.name not in self._fetched and provider.pricing_url:
                self._fetched.add(provider.name)
                try:
                    await self._fetch(provider)
                except Exception:  # pricing is best-effort
                    pass
        return self._prices.get((provider.name, model))

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
BUSY_STATUS = RETRY_STATUS  # "model busy"/overloaded/rate limited/gateway errors: wait patiently


class _ModelUnavailable(Exception):
    """This model can't serve the request right now; try the role's next fallback model."""


# Model-name fragments that usually indicate vision-capable models (used when the
# config's explicit `vision_models` list doesn't match).
VISION_MODEL_HINTS: tuple[str, ...] = ("vision", "-vl", "vl-", "omni", "4o", "4v", "image", "multimodal")


def model_supports_vision(model: str, vision_models: list[str] | None = None) -> bool:
    """True if `model` is vision-capable, via the explicit config list (exact or
    substring, case-insensitive) or the VISION_MODEL_HINTS name heuristic."""
    low = model.lower()
    for entry in vision_models or []:
        e = entry.lower()
        if e == low or e in low:
            return True
    return any(h in low for h in VISION_MODEL_HINTS)


def parse_tool_args(raw) -> dict:
    """Parse a tool call's `function.arguments` JSON string into a dict.

    Tolerates double-encoded JSON strings (a dict encoded twice). Empty/None
    yields {}. Invalid JSON raises ValueError (callers catch it)."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    args = json.loads(raw)
    if isinstance(args, str):
        args = json.loads(args)
    return args if isinstance(args, dict) else {}


def messages_contain_images(messages: list[dict]) -> bool:
    """True if any message carries list content with an image_url block."""
    for m in messages:
        content = m.get("content")
        if isinstance(content, list) and any(b.get("type") == "image_url" for b in content if isinstance(b, dict)):
            return True
    return False


def normalize_messages(messages: list[dict], vision: bool) -> tuple[list[dict], int]:
    """Prepare messages for a model with or without vision support.

    Returns a NEW outer list (the input is never mutated). With vision, messages
    pass through unchanged (image blocks included). Without vision, image_url
    blocks are stripped and a short text note is prepended to each affected
    message; other block types are kept. Returns (messages, images_stripped)."""
    out: list[dict] = []
    stripped = 0
    for m in messages:
        content = m.get("content")
        if vision or not isinstance(content, list) or not any(
                isinstance(b, dict) and b.get("type") == "image_url" for b in content):
            out.append(m)
            continue
        n = sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image_url")
        stripped += n
        note = {"type": "text",
                "text": f"[note: {n} image attachment(s) were removed because this model does not support vision]"}
        new_m = dict(m)
        new_m["content"] = [note, *[b for b in content
                                     if not (isinstance(b, dict) and b.get("type") == "image_url")]]
        out.append(new_m)
    return out, stripped


class LLMClient:
    def __init__(self, config: Config, store=None, run_id: str | None = None,
                 budget: Budget | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 max_retries: int = 4, busy_wait_s: float | None = None,
                 record_errors: bool = True):
        self.config = config
        self.busy_wait_s = busy_wait_s
        self.store = store
        self.run_id = run_id
        self.budget = budget or Budget(config.limits.budget_usd)
        self.prices = PriceTable()
        self.max_retries = max_retries
        self.record_errors = record_errors
        self._transport = transport

    async def chat(self, role: str, messages: list[dict], *, tools: list[dict] | None = None,
                   task_key: str | None = None, tool_choice: Any = None) -> ChatResult:
        """Call the role's model. Busy/overloaded responses (429/503/529…) are retried patiently
        for up to `limits.busy_wait_s` per model; then the role's `fallback_models` are tried in
        order. Every attempt that fails is recorded; the successful call records the model used.
        Message `content` may be a string or an OpenAI-style list of blocks (text / image_url,
        base64 data URLs supported); image blocks are forwarded as-is for vision-capable models
        and replaced with a text note otherwise."""
        rc = self.config.role(role)
        pc = self.config.providers[rc.provider]
        key = pc.api_key()
        if not key:
            raise LLMError(f"environment variable {pc.api_key_env} is not set (provider {pc.name})")
        self.budget.check()
        models = [rc.model, *[m for m in rc.fallback_models if m != rc.model]]
        errors: list[str] = []
        for idx, model in enumerate(models):
            if idx > 0:
                self._event(f"{role}: {models[idx - 1]} unavailable; falling back to {model}", task_key, role)
            try:
                return await self._chat_model(role, rc, pc, key, model, messages, tools, tool_choice, task_key)
            except _ModelUnavailable as e:
                errors.append(f"{model}: {e}")
                continue
        raise LLMError(f"{role} call failed on every configured model: " + " | ".join(errors))

    async def _chat_model(self, role, rc, pc, key, model, messages, tools, tool_choice, task_key) -> ChatResult:
        vision = model_supports_vision(model, self.config.vision_models)
        msgs, stripped = normalize_messages(messages, vision)
        if stripped:
            self._event(f"{role}: {model} is not vision-capable; {stripped} image block(s) "
                        "replaced with a text note", task_key, role, level="warn")
        body: dict[str, Any] = {
            "model": model,
            "messages": msgs,
            "temperature": rc.temperature,
            "max_tokens": rc.max_output_tokens,
            **rc.extra_body,
        }
        effort = rc.reasoning_effort or self.config.reasoning_effort
        if effort and "reasoning_effort" not in rc.extra_body:
            body["reasoning_effort"] = effort      # DeepInfra: none|low|medium|high (default for reasoning models: high)
        if tools:
            body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   **pc.extra_headers}
        url = pc.base_url.rstrip("/") + "/chat/completions"
        busy_wait = self.busy_wait_s if self.busy_wait_s is not None else self.config.limits.busy_wait_s

        started = time.time()
        last_err: str | None = None
        data: dict | None = None
        transient = 0
        busy_attempt = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(rc.timeout_s, connect=30),
                                     transport=self._transport) as client:
            while True:
                try:
                    r = await client.post(url, headers=headers, json=body)
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    last_err = f"{type(e).__name__}: {e}"
                    transient += 1
                    if transient <= self.max_retries:
                        await asyncio.sleep(_backoff(transient - 1))
                        continue
                    self._record(role, model, pc, task_key, started, Usage(), "error", last_err)
                    raise _ModelUnavailable(last_err)
                if r.status_code in BUSY_STATUS:
                    last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                    waited = time.time() - started
                    if waited < busy_wait:
                        delay = min(_busy_backoff(busy_attempt, r), max(1.0, busy_wait - waited))
                        if busy_attempt in (0, 3, 8):
                            self._event(f"{role}: {model} is busy ({r.status_code}); retrying for up to "
                                        f"{int(busy_wait // 60)} min", task_key, role, level="warn")
                        busy_attempt += 1
                        await asyncio.sleep(delay)
                        continue
                    self._record(role, model, pc, task_key, started, Usage(), "error", last_err)
                    raise _ModelUnavailable(f"busy for {int(waited)}s: {last_err}")
                if r.status_code in (404,) or (r.status_code == 400 and "model" in r.text.lower()
                                               and ("not found" in r.text.lower() or "does not exist" in r.text.lower())):
                    last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                    self._record(role, model, pc, task_key, started, Usage(), "error", last_err)
                    raise _ModelUnavailable(last_err)
                if r.status_code >= 400:
                    last_err = f"HTTP {r.status_code}: {r.text[:1000]}"
                    self._record(role, model, pc, task_key, started, Usage(), "error", last_err)
                    raise LLMError(f"{role} ({model}) call failed: {last_err}")
                try:
                    data = r.json()
                except ValueError:
                    data = None
                if not data or not data.get("choices"):
                    last_err = f"no choices in response: {r.text[:300]}"
                    transient += 1
                    if transient <= self.max_retries:
                        await asyncio.sleep(_backoff(transient - 1))
                        continue
                    self._record(role, model, pc, task_key, started, Usage(), "error", last_err)
                    raise _ModelUnavailable(last_err)
                break

        prices = await self.prices.get(pc, rc, model)
        usage = parse_usage(data.get("usage"), prices)
        self.budget.add(usage)
        choice = data["choices"][0]
        msg = dict(choice.get("message") or {})
        msg.setdefault("role", "assistant")
        self._record(role, model, pc, task_key, started, usage, "ok", None)
        return ChatResult(message=msg, finish_reason=choice.get("finish_reason"), usage=usage, raw=data)

    async def chat_with_tools(self, role: str, messages: list[dict], tools: list[dict],
                              execute, *, max_rounds: int = 5, task_key: str | None = None,
                              on_tool=None) -> ChatResult:
        """Function-calling loop: call the model, execute requested tools, feed
        results back as role='tool' messages, and repeat until the model returns
        plain text or `max_rounds` LLM calls have been made.

        `messages` is mutated in place (assistant and tool messages are appended).
        `execute(name, args)` may be sync or async; exceptions are converted to
        `"ERROR: ..."` tool messages and never raised out of the loop. `on_tool(name,
        args)` fires before each execution (sync or async) and its errors are
        swallowed."""
        if max_rounds <= 0:
            raise ValueError("max_rounds must be >= 1")
        res: ChatResult | None = None
        for _ in range(max_rounds):
            res = await self.chat(role, messages, tools=tools, task_key=task_key)
            messages.append(res.message)
            if not res.tool_calls:
                return res
            for tc in res.tool_calls:
                name = tc["function"]["name"]
                try:
                    args = parse_tool_args(tc["function"].get("arguments"))
                except ValueError as e:
                    args = {}
                    out = f"ERROR: ValueError: invalid tool arguments: {e}"
                else:
                    if on_tool is not None:
                        try:
                            cb = on_tool(name, args)
                            if inspect.isawaitable(cb):
                                await cb
                        except Exception:
                            pass
                    try:
                        out = execute(name, args)
                        if inspect.isawaitable(out):
                            out = await out
                    except Exception as e:
                        out = f"ERROR: {type(e).__name__}: {e}"
                messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": str(out)})
        assert res is not None  # loop ran at least once when max_rounds >= 1
        return res

    def _event(self, message: str, task_key, role, level: str = "info") -> None:
        if self.store and self.run_id:
            try:
                self.store.event(self.run_id, "model", message, level=level, task_key=task_key, role=role)
            except Exception:
                pass

    def _record(self, role, model, pc, task_key, started, usage: Usage, status, error):
        if not self.store or not self.run_id:
            return
        if status != "ok" and not self.record_errors:
            # Failed calls are still visible as events, but don't count toward
            # cost summaries (used by per-conversation chat attribution).
            self._event(f"llm call failed ({status}): {error}", task_key, role, level="error")
            return
        self.store.record_llm_call(
            self.run_id, task_key=task_key, role=role, provider=pc.name, model=model,
            started_at=started, ended_at=time.time(), prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens, total_tokens=usage.total_tokens,
            cached_tokens=usage.cached_tokens, cost_usd=usage.cost_usd,
            cost_source=usage.cost_source if status == "ok" else "unavailable",
            status=status, error=error,
        )


def _busy_backoff(attempt: int, resp: httpx.Response | None = None) -> float:
    """5s, 10s, 20s, 40s, then every 60s (honours Retry-After up to 2 min), with jitter."""
    if resp is not None:
        ra = resp.headers.get("retry-after")
        if ra:
            try:
                return min(120.0, float(ra)) + random.random()
            except ValueError:
                pass
    return min(60.0, 5.0 * (2 ** attempt)) + random.random() * 2


def _backoff(attempt: int, resp: httpx.Response | None = None) -> float:
    if resp is not None:
        ra = resp.headers.get("retry-after")
        if ra:
            try:
                return min(60.0, float(ra))
            except ValueError:
                pass
    return min(60.0, (2 ** attempt) + random.random())
