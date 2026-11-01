"""Status chatbot: answers questions about runs and swarm (per-task/agent) statuses.

Builds a deterministic plain-text snapshot of the SQLite store (the same data the
dashboard and `swarmforge status` show) and sends it as system context to a
configured role (the optional `chat` role, falling back to the self-heal
diagnosis role or `planner`) through the existing LLMClient.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import httpx

from .config import Config, ConfigError
from .db import Store

TERMINAL = {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "BUDGET_EXCEEDED"}

MAX_SNAPSHOT_CHARS = 14_000


class ChatError(RuntimeError):
    """Friendly, user-presentable chatbot failure."""


SYSTEM_PROMPT = """You are the SwarmForge status chatbot. You answer questions about SwarmForge runs \
and the swarm's per-task/agent statuses.

Rules:
- Answer ONLY from the provided <status> snapshot (plus the conversation history). \
Never invent runs, tasks, states, costs or failures.
- Cite run ids (first 8 characters) and their states when you mention a run.
- Be concise and use Markdown (short bullets, bold states).
- If the snapshot does not contain the answer, say you do not know instead of guessing."""


def resolve_role(cfg: Config) -> str:
    """The role the chatbot talks through: `chat` if configured, else the self-heal
    diagnosis role if it exists, else `planner`."""
    if "chat" in cfg.roles:
        return "chat"
    sh = cfg.self_heal
    if sh.diagnosis_role in cfg.roles:
        return sh.diagnosis_role
    return "planner"


def _trunc(s: str | None, n: int = 200) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _cap(text: str, limit: int = MAX_SNAPSHOT_CHARS) -> str:
    """Cap total length; truncate the middle (like postmortem.evidence)."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n…[snapshot truncated]…\n" + text[-half:]


def status_snapshot(store: Store, max_runs: int = 12) -> str:
    """Deterministic plain-text snapshot of runs, swarm task statuses, failures,
    queue and pending approvals. Never raises on an empty store."""
    gen = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    L = [f"SwarmForge run/swarm state generated {gen}"]
    runs = store.list_runs(limit=max_runs)
    if not runs:
        L.append("No runs yet.")
        return "\n".join(L)

    for run in runs:
        rid = (run["id"] or "")[:8]
        created = run.get("created_at")
        finished = run.get("finished_at")
        elapsed = ((finished or time.time()) - created) / 60 if created else 0
        cost = store.cost_summary(run["id"]).get("total") or 0
        L.append("")
        L.append(f"Run {rid}: {run.get('state') or '-'}"
                 f"{' - ' + run['state_reason'] if run.get('state_reason') else ''}")
        L.append(f"  mode: {run.get('mode') or '-'} | issue: {run.get('issue_title') or '-'} "
                 f"| repo: {run.get('repo') or '-'}")
        L.append(f"  created: {datetime.fromtimestamp(created, UTC).strftime('%Y-%m-%d %H:%M') if created else '-'}"
                 f" | finished: {datetime.fromtimestamp(finished, UTC).strftime('%Y-%m-%d %H:%M') if finished else '-'}"
                 f" | elapsed: {elapsed:.0f} min")
        L.append(f"  cost: ${cost:.3f} of ${run.get('budget_usd') or 0:.2f} "
                 f"| repair cycle: {run.get('repair_cycle') or 0} "
                 f"| pr: {run.get('pr_url') or '-'}")
        tasks = store.tasks(run["id"])
        for t in tasks:
            L.append(f"  [{t.get('state')}] {t.get('task_key')} "
                     f"({t.get('role')}, {t.get('model')}, cycle {t.get('cycle')}): "
                     f"{t.get('title') or ''}")
            if t.get("error"):
                L.append(f"    error: {_trunc(t['error'])}")
            if t.get("summary"):
                L.append(f"    summary: {_trunc(t['summary'])}")

    L.append("")
    L.append("Failures (post-mortems):")
    failures = store.failures()
    if not failures:
        L.append("  none")
    for f in failures:
        L.append(f"  - run {(f.get('run_id') or '')[:8]}: {f.get('category') or '-'} "
                 f"| root cause: {_trunc(f.get('root_cause'))} "
                 f"| outcome: {f.get('outcome') or '-'} | retry: {bool(f.get('retry'))}")

    L.append("Queue:")
    items = store.queue_items()
    if not items:
        L.append("  empty")
    for q in items:
        L.append(f"  - [{q.get('status')}] {q.get('title') or '-'} (repo: {q.get('repo') or '-'})")

    approvals = len(store.pending_approvals())
    L.append(f"Pending approvals: {approvals}")

    return _cap("\n".join(L))


async def answer(cfg: Config, store: Store, question: str,
                 history: list[dict] | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
                 max_runs: int = 12) -> dict:
    """Ask the status chatbot a question; returns {answer, role, model, question}."""
    from .llm import Budget, BudgetExceeded, LLMClient, LLMError

    role = resolve_role(cfg)
    messages = [{"role": "system",
                 "content": SYSTEM_PROMPT + "\n\n<status>\n" + status_snapshot(store, max_runs) + "\n</status>"}]
    for m in (history or []):
        if m.get("role") in ("user", "assistant") and m.get("content"):
            messages.append({"role": m["role"], "content": str(m["content"])})
    messages.append({"role": "user", "content": question})
    try:
        llm = LLMClient(cfg, store=None, run_id=None, budget=Budget(2.0), transport=transport)
        res = await llm.chat(role, messages, task_key="chatbot")
    except (LLMError, BudgetExceeded, ConfigError, httpx.HTTPError) as e:
        raise ChatError(str(e)) from e
    return {"answer": res.content.strip(), "role": role,
            "model": cfg.roles[role].model, "question": question}


def ask(cfg: Config, store: Store, question: str, history: list[dict] | None = None, *,
        transport: httpx.AsyncBaseTransport | None = None, max_runs: int = 12) -> dict:
    """Synchronous wrapper around answer() for CLI and Streamlit callers."""
    return asyncio.run(answer(cfg, store, question, history, transport=transport, max_runs=max_runs))
