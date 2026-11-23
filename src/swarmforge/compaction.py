"""Context compaction: keep long conversations and agent sessions within the model's context.

Instead of silently forgetting what scrolled out of the window, older material is folded into a
short model-written summary:

* Conversations (chat and the project lead's kickoff conversation) keep a rolling summary per
  conversation. Only messages that newly fell out of the recent window are summarized, together with
  the previous summary, so each update is small. The summary is stored, shown to the model as context
  and never replaces the retained messages in the database.
* Agent sessions summarize their oldest turns once trimming old tool output is not enough. The cut
  is always at the start of an assistant turn, so a tool result is never separated from its call.

Summaries are paid from the same allowance as the conversation or task. If summarizing fails, the
caller falls back to the previous summary (conversations) or to dropping the oldest turns with a
visible marker (agents); the original work always continues.
"""
from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable

ChatFn = Callable[[list[dict]], Awaitable[str]]

SUMMARY_WORDS = 250
MAX_SOURCE_CHARS = 60_000          # bound on the text sent to the summarizer per update
SUMMARY_TOKENS_RESERVED = 500      # room kept in a conversation's token budget for the summary

CONVERSATION_PROMPT = (
    "You maintain the running summary of an earlier part of a conversation between a user and an assistant "
    "for a software-engineering team tool. Merge the previous summary with the newly provided messages into "
    f"one updated summary of at most {SUMMARY_WORDS} words. Keep decisions, requirements, names, repository "
    "and task identifiers, numbers, open questions and the user's stated preferences. Drop pleasantries. "
    "Write plain sentences or short bullets, no preamble.")

AGENT_PROMPT = (
    "You are compacting the working memory of a coding agent. Summarize the earlier part of its session in at "
    f"most {SUMMARY_WORDS} words: what it learned about the code (files, functions, commands), what it changed, "
    "test results, errors still open, and what it was about to do next. Plain bullets, no preamble.")


def _render(messages: list[dict], limit: int = MAX_SOURCE_CHARS) -> str:
    """Readable transcript text, keeping the most recent part when it is too long."""
    lines = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):  # multimodal blocks: keep the text
            content = " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
        text = str(content or "")
        for call in m.get("tool_calls") or []:
            fn = call.get("function", {})
            text += f"\n[tool call] {fn.get('name')}({str(fn.get('arguments', ''))[:400]})"
        lines.append(f"{m.get('role', '?')}: {text}")
    out = "\n\n".join(lines)
    return out if len(out) <= limit else "[...earliest part omitted...]\n" + out[-limit:]


# ---------------------------------------------------------------- conversations

def ensure_tables(store) -> None:
    store._exec("""CREATE TABLE IF NOT EXISTS conversation_summaries (
        conversation_id TEXT PRIMARY KEY, summary TEXT NOT NULL, through_message_id INTEGER NOT NULL,
        updated_at REAL NOT NULL)""")


def stored_summary(store, conversation: str) -> dict | None:
    ensure_tables(store)
    return store._one("SELECT summary, through_message_id FROM conversation_summaries WHERE conversation_id=?", (conversation,))


async def compact_conversation(store, conversation: str, all_messages: list[dict], kept: list[dict],
                               chat: ChatFn) -> str | None:
    """Return the summary covering every message older than `kept`, updating it if needed.

    `all_messages` is the conversation in order (with ids); `kept` is the recent window that will be sent
    verbatim. Returns None when nothing has dropped out of the window."""
    if not kept or len(kept) >= len(all_messages):
        return None
    first_kept = kept[0]["id"]
    dropped = [m for m in all_messages if m["id"] < first_kept]
    if not dropped:
        return None
    previous = stored_summary(store, conversation)
    through = previous["through_message_id"] if previous else 0
    new = [m for m in dropped if m["id"] > through]
    if not new:
        return previous["summary"] if previous else None
    source = (f"Previous summary:\n{previous['summary']}\n\n" if previous else "") + "New messages:\n" + _render(new)
    try:
        summary = (await chat([{"role": "system", "content": CONVERSATION_PROMPT},
                               {"role": "user", "content": source}])).strip()
    except Exception:  # noqa: BLE001 - never block the reply; keep the last good summary
        return previous["summary"] if previous else None
    if not summary:
        return previous["summary"] if previous else None
    store._exec("INSERT INTO conversation_summaries(conversation_id,summary,through_message_id,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET summary=excluded.summary, "
                "through_message_id=excluded.through_message_id, updated_at=excluded.updated_at",
                (conversation, summary[:8000], dropped[-1]["id"], time.time()))
    return summary


def summary_message(summary: str) -> dict:
    return {"role": "system", "content": "Summary of the earlier part of this conversation (older messages are not "
                                         "shown verbatim):\n" + summary}


# ---------------------------------------------------------------- agent sessions

def _turn_starts(messages: list[dict]) -> list[int]:
    """Indexes where a new assistant turn begins (safe cut points: no tool result is orphaned)."""
    return [i for i, m in enumerate(messages) if i >= 2 and m.get("role") == "assistant"]


async def compact_agent_history(messages: list[dict], chat: ChatFn, *, keep_turns: int = 6) -> bool:
    """Fold the oldest agent turns into a summary, in place. Returns True if anything was compacted.

    messages[0] (system) and messages[1] (the task) are always kept, as are the last `keep_turns`
    assistant turns with their tool results."""
    starts = _turn_starts(messages)
    if len(starts) <= keep_turns:
        return False
    cut = starts[-keep_turns]
    old = messages[2:cut]
    if not old:
        return False
    try:
        summary = (await chat([{"role": "system", "content": AGENT_PROMPT},
                               {"role": "user", "content": _render(old)}])).strip()
    except Exception as exc:  # noqa: BLE001 - budget/cancellation errors are re-raised by the caller's own call
        from .execution import ExecutionStopped
        from .llm import BudgetExceeded
        if isinstance(exc, (BudgetExceeded, ExecutionStopped)):
            raise
        summary = ""
    note = (f"[Context compacted: {len(old)} earlier messages were summarized to stay within the model's context.]\n"
            + (summary or f"(No summary available; {len(old)} earlier messages were dropped. Re-read files you need.)"))
    messages[2:cut] = [{"role": "user", "content": note}]
    return True


def transcript_chars(messages: list[dict]) -> int:
    return sum(len(json.dumps(m, default=str)) for m in messages)
