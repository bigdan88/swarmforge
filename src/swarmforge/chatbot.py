"""Status chatbot: answers questions about runs and swarm (per-task/agent) statuses.

Builds a deterministic plain-text snapshot of the SQLite store (the same data the
dashboard and `swarmforge status` show) and sends it as system context to a
configured role (the optional `chat` role, falling back to the self-heal
diagnosis role or `planner`) through the existing LLMClient.
"""
from __future__ import annotations

import asyncio
import time
import uuid
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

IMPROVE_PROMPT = """
You are also the team lead who helps Dan improve SwarmForge itself (the platform) and get failed tasks
back on track. Use <improvement_context> (features, modules, statistics, failures, lessons learned,
work already queued or planned) as your evidence.

When Dan asks what to improve, for ideas, or about failed tasks, answer in Markdown and then END your
reply with exactly one fenced block of machine-readable actions:

```actions
{"tasks": [{"title": "...", "description": "what to build, with an **Acceptance criteria** bullet list",
            "why": "the evidence: run ids, failure categories, lessons", "size": "S|M|L", "repo": "owner/name"}],
 "fixes": [{"run_id": "<full run id from the failures list>", "summary": "what went wrong, one line",
            "options": [{"label": "short button text", "instructions": "what the next attempt must do differently",
                         "time_limit_h": null, "budget_usd": null, "max_cycles": null, "continue_code": true}]}]}
```

Rules for tasks: 2-5 tasks, each small and independent (one pull request, about a day of work), concrete
and testable, never a duplicate of something already queued, in the backlog or recently done. Prefer
changes that remove the causes of the failures and slowness you can see. Use the platform repository
unless Dan asks about another project.
Rules for fixes: only for runs in the failures list; 2-4 distinct options per run (e.g. more time,
smaller scope, specific technical fix, continue from the code already written); set the numeric fields
only when that option changes them; continue_code=true only if the run is marked resumable.
Leave "tasks" or "fixes" as an empty list when not relevant. Omit the block entirely for plain status
questions."""



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
                 "content": SYSTEM_PROMPT + "\n" + IMPROVE_PROMPT + "\n\n<status>\n" + status_snapshot(store, max_runs)
                 + "\n</status>\n\n<improvement_context>\n" + improvement_context(cfg, store)
                 + "\n</improvement_context>"}]
    for m in (history or []):
        if m.get("role") in ("user", "assistant") and m.get("content"):
            messages.append({"role": m["role"], "content": str(m["content"])})
    messages.append({"role": "user", "content": question})
    try:
        llm = LLMClient(cfg, store=None, run_id=None, budget=Budget(2.0), transport=transport)
        res = await llm.chat(role, messages, task_key="chatbot")
    except (LLMError, BudgetExceeded, ConfigError, httpx.HTTPError) as e:
        raise ChatError(str(e)) from e
    text, actions = parse_actions(res.content or "", store)
    return {"answer": text, "role": role, "model": cfg.roles[role].model, "question": question,
            "actions": actions}


def ask(cfg: Config, store: Store, question: str, history: list[dict] | None = None, *,
        transport: httpx.AsyncBaseTransport | None = None, max_runs: int = 12) -> dict:
    """Synchronous wrapper around answer() for CLI and Streamlit callers."""
    return asyncio.run(answer(cfg, store, question, history, transport=transport, max_runs=max_runs))


# ======================================================== conversation persistence
DEFAULT_CONVERSATION_TITLE = "General"


def create_conversation(store: Store, title: str = DEFAULT_CONVERSATION_TITLE,
                        last_run_id: str | None = None) -> dict:
    """Create a new conversation and return it as a dict."""
    conv_id = uuid.uuid4().hex
    now = time.time()
    store._exec(
        "INSERT INTO conversations (id, title, created_at, updated_at, last_run_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (conv_id, title, now, now, last_run_id),
    )
    conv = get_conversation(store, conv_id)
    assert conv is not None
    return conv


def get_conversation(store: Store, conversation_id: str) -> dict | None:
    return store._one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))


def list_conversations(store: Store, limit: int = 50) -> list[dict]:
    """Conversations most-recent first (by updated_at, then id)."""
    return store._all(
        "SELECT * FROM conversations ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,))


def add_message(store: Store, conversation_id: str, role: str, content: str) -> dict:
    """Append a message to a conversation; bumps the conversation's updated_at."""
    if get_conversation(store, conversation_id) is None:
        raise ChatError(f"unknown conversation {conversation_id}")
    now = time.time()
    with store._lock:
        cur = store.conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (conversation_id, role, content, now),
        )
        store.conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        store.conn.commit()
        msg_id = cur.lastrowid
    msg = store._one("SELECT * FROM chat_messages WHERE id = ?", (msg_id,))
    assert msg is not None
    return msg


def get_messages(store: Store, conversation_id: str, limit: int | None = None) -> list[dict]:
    """Messages of a conversation, oldest first. With limit, the last `limit` messages."""
    if limit is None:
        return store._all(
            "SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,))
    rows = store._all(
        "SELECT * FROM chat_messages WHERE conversation_id = ? "
        "ORDER BY id DESC LIMIT ?", (conversation_id, limit))
    return list(reversed(rows))


def clear_messages(store: Store, conversation_id: str) -> int:
    """Delete every message of a conversation, keeping the conversation row itself.

    Returns the number of deleted messages. Raises ChatError for an unknown id.
    Does NOT bump conversations.updated_at (sidebar order must not change).
    """
    if get_conversation(store, conversation_id) is None:
        raise ChatError(f"unknown conversation {conversation_id}")
    with store._lock:
        cur = store.conn.execute(
            "DELETE FROM chat_messages WHERE conversation_id = ?", (conversation_id,))
        store.conn.commit()
        return cur.rowcount


def rename_conversation(store: Store, conversation_id: str, title: str) -> dict:
    """Rename a conversation; bumps updated_at."""
    if get_conversation(store, conversation_id) is None:
        raise ChatError(f"unknown conversation {conversation_id}")
    store._exec(
        "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
        (title, time.time(), conversation_id),
    )
    conv = get_conversation(store, conversation_id)
    assert conv is not None
    return conv


def delete_conversation(store: Store, conversation_id: str) -> bool:
    """Delete a conversation and all its messages. Returns True if it existed."""
    with store._lock:
        store.conn.execute("DELETE FROM chat_messages WHERE conversation_id = ?",
                           (conversation_id,))
        cur = store.conn.execute("DELETE FROM conversations WHERE id = ?",
                                 (conversation_id,))
        store.conn.commit()
        return cur.rowcount > 0


def ensure_default_conversation(store: Store) -> dict:
    """Return the default conversation, seeding it on first use."""
    conv = store._one(
        "SELECT * FROM conversations WHERE title = ? ORDER BY id LIMIT ?",
        (DEFAULT_CONVERSATION_TITLE, 1),
    )
    if conv is not None:
        return conv
    return create_conversation(store, DEFAULT_CONVERSATION_TITLE)


# ====================================================================== self-improvement
NEEDS_YOU = ("needs you", "still failing", "not retried")
SIZES = ("S", "M", "L")


def _platform_root():
    from .selfupdate import ROOT
    return ROOT


def failures_needing_attention(store: Store, limit: int = 10) -> list[dict]:
    out = []
    for f in store.failures(limit=200):
        outcome = (f.get("outcome") or "")
        if outcome.startswith(NEEDS_YOU) or (not outcome and not f.get("heal_run_id")):
            if "dismissed" in outcome or "retried from chat" in outcome:
                continue
            out.append(f)
        if len(out) >= limit:
            break
    return out


def improvement_context(cfg: Config, store: Store) -> str:
    """Evidence for suggesting platform improvements and failure fixes. Never raises."""
    import re
    from pathlib import Path
    L: list[str] = []
    try:
        from .selfupdate import own_repo
        L.append(f"Platform repository (SwarmForge itself): {own_repo() or 'unknown'}")
    except Exception:  # noqa: BLE001
        pass
    root = _platform_root()
    try:
        heads = [h[3:].strip() for h in (root / "README.md").read_text(encoding="utf-8").splitlines()
                 if h.startswith("## ")]
        L.append("Features (README sections): " + "; ".join(heads[:40]))
    except OSError:
        pass
    mods = []
    for f in sorted((root / "src" / "swarmforge").glob("*.py")):
        try:
            m = re.match(r'\s*(?:from __future__[^\n]*\n)?\s*"""(.*?)(?:\n|""")', f.read_text(encoding="utf-8"), re.S)
            mods.append(f"{f.stem}: {_trunc(m.group(1), 110) if m else ''}")
        except OSError:
            continue
    if mods:
        L.append("Modules:\n  " + "\n  ".join(mods[:45]))
    # statistics
    runs = store.list_runs(40)
    done = [r for r in runs if r["state"] in TERMINAL]
    if done:
        ok = sum(1 for r in done if r["state"] == "COMPLETED")
        durs = sorted(((r.get("finished_at") or r["created_at"]) - r["created_at"]) / 60 for r in done)
        costs = [store.cost_summary(r["id"]).get("total") or 0 for r in done]
        cats: dict[str, int] = {}
        for f in store.failures(limit=100):
            cats[f.get("category") or "?"] = cats.get(f.get("category") or "?", 0) + 1
        L.append(f"Last {len(done)} finished runs: {ok} completed, {len(done) - ok} not; median "
                 f"{durs[len(durs) // 2]:.0f} min; average ${sum(costs) / len(costs):.2f}. Failure categories: "
                 + ", ".join(f"{k}={v}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1])))
    # failures that need a decision
    fails = failures_needing_attention(store)
    if fails:
        from .postmortem import resumable
        L.append("Failures needing Dan's decision (use these full run ids in fixes):")
        for f in fails:
            run = store.get_run(f["run_id"]) or {}
            L.append(f"- run {f['run_id']} | task: {_trunc(f.get('title') or run.get('issue_title'), 120)} | "
                     f"repo: {f.get('repo') or run.get('repo')} | category: {f.get('category')} | "
                     f"time limit {((run.get('max_runtime_s') or 0) / 3600):.1f} h, budget ${run.get('budget_usd') or 0:.0f}, "
                     f"repair cycles {run.get('max_repair_cycles')} | resumable: {resumable(store, f['run_id'])}\n"
                     f"  root cause: {_trunc(f.get('root_cause'), 400)}\n  suggested fix: {_trunc(f.get('fix'), 300)}")
    # lessons from the knowledge base
    try:
        from .knowledge import Vault
        notes = [n for n in Vault(cfg.vault_path).notes() if n.type in ("pitfall", "review-lesson", "failure",
                                                                         "user-note", "model-behavior")]
        notes.sort(key=lambda n: n.created or "", reverse=True)
        if notes:
            L.append("Lessons learned (newest first):")
            L += [f"- [{n.type}] {n.title}: {_trunc(n.body.replace(chr(10), ' '), 160)}" for n in notes[:15]]
    except Exception:  # noqa: BLE001
        pass
    # don't suggest what's already planned
    planned = [q["title"] for q in store.queue_items() if q["status"] in ("queued", "running")]
    try:
        from .runqueue import parse_backlog
        bl = root / "docs" / "SELF_IMPROVEMENT_BACKLOG.md"
        if bl.is_file():
            planned += [b.title for b in parse_backlog(bl.read_text(encoding="utf-8"))]
    except Exception:  # noqa: BLE001
        pass
    recent = [r.get("issue_title") for r in runs if r.get("issue_title")]
    if planned:
        L.append("Already queued or in the backlog (don't duplicate): " + "; ".join(_trunc(t, 80) for t in planned[:30]))
    if recent:
        L.append("Recently worked on: " + "; ".join(_trunc(t, 80) for t in dict.fromkeys(recent).keys()))
    return _cap("\n".join(L), 12_000)


def _num(v, lo, hi):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if lo <= x <= hi else None


def parse_actions(text: str, store: Store | None = None) -> tuple[str, dict]:
    """Split the reply into (markdown shown to Dan, validated actions)."""
    import json
    import re
    actions: dict = {"tasks": [], "fixes": []}
    m = re.search(r"```actions\s*(\{.*?\})\s*```", text or "", re.S)
    if not m:
        return (text or "").strip(), actions
    shown = ((text[:m.start()] + text[m.end():]) if text else "").strip()
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return shown, actions
    for t in (data.get("tasks") or [])[:8]:
        if isinstance(t, dict) and str(t.get("title") or "").strip() and str(t.get("description") or "").strip():
            actions["tasks"].append({"title": str(t["title"]).strip()[:150],
                                     "description": str(t["description"]).strip()[:6000],
                                     "why": str(t.get("why") or "").strip()[:800],
                                     "size": str(t.get("size") or "M").upper()[:1] if str(t.get("size") or "M").upper()[:1] in SIZES else "M",
                                     "repo": str(t.get("repo") or "").strip()[:200]})
    for f in (data.get("fixes") or [])[:10]:
        if not isinstance(f, dict):
            continue
        run = store.get_run(str(f.get("run_id") or "")) if store is not None else {"id": f.get("run_id")}
        if not run:
            continue
        opts = []
        for o in (f.get("options") or [])[:5]:
            if isinstance(o, dict) and str(o.get("instructions") or "").strip():
                opts.append({"label": str(o.get("label") or "Retry")[:80], "instructions": str(o["instructions"])[:3000],
                             "time_limit_h": _num(o.get("time_limit_h"), 0.1, 72),
                             "budget_usd": _num(o.get("budget_usd"), 0.5, 500),
                             "max_cycles": int(_num(o.get("max_cycles"), 0, 10)) if _num(o.get("max_cycles"), 0, 10) is not None else None,
                             "continue_code": bool(o.get("continue_code"))})
        if opts:
            actions["fixes"].append({"run_id": run["id"], "summary": str(f.get("summary") or "")[:300], "options": opts})
    return shown, actions
