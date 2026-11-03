# Connecting Chinese model providers

SwarmForge talks to any **OpenAI-compatible chat-completions endpoint that supports tool
(function) calling**. The default setup uses **DeepInfra**, which hosts all five model families
behind one key and one API, and returns a per-call cost (`usage.estimated_cost`).

## DeepInfra (default)

1. Create a key at <https://deepinfra.com/dash/api_keys> and add credit/billing.
2. Put it in `.env` next to your `swarmforge.yaml`:
   ```
   DEEPINFRA_API_KEY=...
   ```
3. Verify: `swarmforge check --live` lists each role's model, its published price, and sends a
   tiny tool-calling request to every model. Every role must show `tool call report_status(...)`.

Provider block:

```yaml
providers:
  deepinfra:
    base_url: https://api.deepinfra.com/v1/openai
    api_key_env: DEEPINFRA_API_KEY
    pricing_url: https://api.deepinfra.com/v1/openai/models
```

### Choosing models

Model ids must match DeepInfra's catalogue exactly. List what is available today, with prices:

```
python -c "import httpx;[print(m['id'], (m.get('metadata') or {}).get('pricing')) for m in httpx.get('https://api.deepinfra.com/v1/openai/models').json()['data'] if any(k in m['id'] for k in ('Qwen','deepseek','moonshot','zai-org','MiniMax'))]"
```

Historical example-team measurements (verified with `check --live` on 2026-10-30; prices are USD
per 1M input/output tokens at that date, not current price guarantees). These models remain in
`swarmforge.example.yaml`; the dated local team is listed in the [README](../README.md):

| Role | Example model | Historical price in/out | Alternative | Alternative |
|---|---|---|---|---|
| planner | `Qwen/Qwen3.5-397B-A17B` | 0.45 / 3.00 | `Qwen/Qwen3.8-Flash` | `Qwen/Qwen3.8-Max` |
| developer_backend | `deepseek-ai/DeepSeek-V4-Pro` | 1.30 / 2.60 | `deepseek-ai/DeepSeek-V4-Flash` | — |
| developer_frontend | `moonshotai/Kimi-K2.6` | 0.75 / 3.50 | — | `moonshotai/Kimi-K3` |
| reviewer | `zai-org/GLM-5.3` | 0.90 / 4.00 | `zai-org/GLM-5.3-Flash` | — |
| tester | `MiniMaxAI/MiniMax-M3` | 0.28 / 1.10 | — | — |

The same model may serve several roles, except that the reviewer must differ from every
developer model unless you set `review.allow_same_model_review: true`.

Provider-specific request options (e.g. reasoning effort) can be passed per role:

```yaml
roles:
  planner:
    provider: deepinfra
    model: Qwen/Qwen3.8-Max
    extra_body: {reasoning_effort: low}
```

## Using the vendors' own APIs instead

Add more providers and point roles at them. All of these expose OpenAI-compatible endpoints; check
each vendor's current documentation for the exact base URL and model names, then run
`swarmforge check --live`.

```yaml
providers:
  deepseek:
    base_url: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY
  moonshot:
    base_url: https://api.moonshot.ai/v1
    api_key_env: MOONSHOT_API_KEY
  dashscope:   # Alibaba Qwen (international endpoint)
    base_url: https://dashscope-intl.aliyuncs.com/compatible-mode/v1
    api_key_env: DASHSCOPE_API_KEY
  zhipu:       # GLM
    base_url: https://open.bigmodel.cn/api/paas/v4
    api_key_env: ZHIPU_API_KEY
```

Without a `pricing_url`, costs are recorded as provider-reported only if the vendor returns a cost,
otherwise set manual prices per role so the budget can be enforced:

```yaml
roles:
  developer_backend:
    provider: deepseek
    model: deepseek-chat
    price_in_per_mtok: 0.27
    price_out_per_mtok: 1.10
```

Calls with neither are reported as "cost unavailable" and do not count toward the budget.

## Multimodal (vision) messages

A chat message's `content` may be a list of blocks instead of a plain string, following the
OpenAI format: `{type: 'text', text: ...}` and `{type: 'image_url', image_url: {url: ...}}`.
Base64 `data:` URLs are supported and forwarded to the provider unchanged (no re-encoding).

Whether a model may receive image blocks is decided at call time:

- The top-level `vision_models` config key lists model ids (or substrings, matched
  case-insensitively) that are vision-capable:
  ```yaml
  vision_models: [Qwen/Qwen2.5-VL-72B-Instruct, openai/gpt-4o]
  ```
- Models not listed are treated as vision-capable only if their name hints at it
  (contains `vision`, `-vl`, `vl-`, `omni`, `4o`, `4v`, `image` or `multimodal`).

If images are passed to a model that is not vision-capable, the call does not fail: the image
blocks are stripped from the message and replaced with a short text note, and the request is
sent as text-only. Plain string content is unaffected.
