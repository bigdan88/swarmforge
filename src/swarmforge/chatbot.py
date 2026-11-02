"""Status chatbot: answers questions about runs and swarm (per-task/agent) statuses.

Builds a deterministic plain-text snapshot of the SQLite store (the same data the
dashboard and `swarmforge status` show) and sends it as system context to a
configured role (the optional `chat` role, falling back to the self-heal
diagnosis role or `planner`) through the existing LLMClient.

Each conversation carries its OWN context window: when a conversation id is
given, the prompt is built from the latest <status> snapshot plus only that
conversation's stored messages (never another thread's), deterministically
truncated to the newest `chat.context_max_messages` messages and then to the
`chat.context_max_tokens` estimated-token budget (the newest message is always
kept). LLM calls are recorded with run_id "chat:<conversation_id>" so
Store.cost_summary attributes usage and cost per conversation.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .chat_tools import ToolRegistry, chat_with_tools
from .config import Config, ConfigError
from .db import Store
from .tools import github_repo as _ghrepo
from .tools import web as _web

TERMINAL = {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "BUDGET_EXCEEDED"}

MAX_SNAPSHOT_CHARS = 14_000

MAX_INLINE_ATTACHMENT_BYTES = 8192
"""Maximum bytes of a text attachment inlined into the chat prompt."""
MAX_CHAT_IMAGES = 4
"""Maximum image attachments forwarded to the model per chat request."""
MAX_IMAGE_BYTES = 4_000_000
"""Maximum size of a single image attachment (bytes)."""

CHAT_RUN_PREFIX = "chat:"
"""llm_calls for a conversation are recorded under this prefix + conversation id,
so Store.cost_summary attributes usage and cost per conversation."""


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: ~4 characters per token, at least 1 per message."""
    return sum(max(1, len(str(m.get("content") or "")) // 4) for m in messages)


def truncate_context(
    messages: list[dict],
    max_messages: int,
    max_tokens: int | None = None,
    *,
    reserved_tokens: int = 0,
) -> list[dict]:
    """Deterministically keep the newest messages: drop from the front until
    len(kept) <= max_messages, then (while over the token budget and more than
    one message remains) drop the oldest. The budget covers the ENTIRE request:
    `reserved_tokens` (the fixed system context) plus the thread. When the
    combined estimate exceeds `max_tokens`, the oldest thread messages are
    dropped until it fits or only the newest remains. The newest message is
    ALWAYS kept, even if it alone exceeds the budget. Never mutates the input;
    preserves chronological order."""
    kept = list(messages)
    while len(kept) > max_messages:
        kept.pop(0)
    if max_tokens is not None:
        while len(kept) > 1 and reserved_tokens + estimate_tokens(kept) > max_tokens:
            kept.pop(0)
    return kept


class ChatError(RuntimeError):
    """Friendly, user-presentable chatbot failure."""


SYSTEM_PROMPT = """You are the SwarmForge status chatbot. You answer questions about SwarmForge runs \
and the swarm's per-task/agent statuses.

Rules:
- Answer ONLY from the provided <status> snapshot (plus the conversation history). \
Never invent runs, tasks, states, costs or failures.
- Cite run ids (first 8 characters) and their states when you mention a run.
- Be concise and use Markdown (short bullets, bold states).
- If the snapshot does not contain the answer, say you do not know instead of guessing.

Web access:
- When the user shares a link (GitHub repository, documentation, article), call the `fetch_url` tool
and answer from the Markdown it returns, citing the URL.
- The tool fetches any URL as clean Markdown and resolves GitHub repo links to their README;
it respects robots.txt and has a 10 s timeout.
- If the tool returns an ERROR string, explain the failure briefly instead of inventing content.

GitHub repo analysis:
- When the user shares a GitHub link like github.com/<owner>/<repo>, you can browse that repository
with the `repo_tree`, `repo_file`, `repo_search` and `repo_issues` tools (owner + repo come from the link).
- `repo_tree` lists the files/folders of a repo path (at an optional git ref); `repo_file` returns the raw
text of one file (up to 50 KB); `repo_search` searches code inside the repo (needs a GitHub token for
private repos); `repo_issues` summarizes open/closed/all issues (pull requests excluded).
- Use `fetch_url` for READMEs and HTML pages, and the repo_* tools to dig into the repository's code
and issues. If a tool returns an ERROR string, explain the failure briefly instead of inventing content."""

WEB_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "The URL to fetch (http/https)"},
        "max_chars": {
            "type": "integer",
            "description": "Maximum characters to return (default 8000)",
        },
        "extract": {
            "type": "string",
            "description": "Extraction mode: markdown (default), text or html",
        },
    },
    "required": ["url"],
}


SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}
"""Directory names never listed by the list_files tool (pruned from the walk)."""


def list_files(path: str | Path = ".", max_entries: int = 200) -> str:
    """List a directory as newline-joined sorted relative paths ('/' separators,
    directories with a trailing '/'), skipping .git/__pycache__/.pytest_cache.
    Truncates to `max_entries` entries and appends a final '… (N more)' line.
    Raises FileNotFoundError for a missing path and NotADirectoryError for a
    non-directory (the tool registry turns these into ERROR strings)."""
    root = Path(path)
    if not root.exists():
        raise FileNotFoundError(f"no such directory: {path}")
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {path}")
    entries: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_dir = Path(dirpath).relative_to(root)
        prefix = "" if rel_dir == Path(".") else rel_dir.as_posix() + "/"
        for d in dirnames:
            entries.append(prefix + d + "/")
        for f in filenames:
            entries.append(prefix + f)
    entries.sort()
    if len(entries) > max_entries:
        omitted = len(entries) - max_entries
        return "\n".join(entries[:max_entries] + [f"… ({omitted} more)"])
    return "\n".join(entries)

REPO_TREE_SCHEMA = {
    "type": "object",
    "properties": {
        "owner": {
            "type": "string",
            "description": "Repository owner (GitHub user or organization)",
        },
        "repo": {"type": "string", "description": "Repository name"},
        "path": {
            "type": "string",
            "description": "Directory path inside the repo (default: root)",
            "default": "",
        },
        "ref": {
            "type": "string",
            "description": "Optional git ref (branch, tag or commit SHA)",
        },
    },
    "required": ["owner", "repo"],
}

REPO_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "owner": {
            "type": "string",
            "description": "Repository owner (GitHub user or organization)",
        },
        "repo": {"type": "string", "description": "Repository name"},
        "path": {"type": "string", "description": "Path of the file inside the repo"},
        "ref": {
            "type": "string",
            "description": "Optional git ref (branch, tag or commit SHA)",
        },
    },
    "required": ["owner", "repo", "path"],
}

REPO_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "owner": {
            "type": "string",
            "description": "Repository owner (GitHub user or organization)",
        },
        "repo": {"type": "string", "description": "Repository name"},
        "query": {"type": "string", "description": "Code search query"},
    },
    "required": ["owner", "repo", "query"],
}

REPO_ISSUES_SCHEMA = {
    "type": "object",
    "properties": {
        "owner": {
            "type": "string",
            "description": "Repository owner (GitHub user or organization)",
        },
        "repo": {"type": "string", "description": "Repository name"},
        "state": {
            "type": "string",
            "enum": ["open", "closed", "all"],
            "description": "Issue state filter (default: open)",
        },
    },
    "required": ["owner", "repo"],
}



def build_chat_registry(cfg: Config) -> ToolRegistry:
    """Tool registry for the chatbot: web fetch plus directory listing."""

    async def _fetch_url(
        url: str, max_chars: int = 8000, extract: str = "markdown"
    ) -> str:
        return await _web.fetch_url(url, max_chars=max_chars, extract=extract)

    registry = ToolRegistry()
    registry.register(
        "fetch_url",
        "Fetch any URL and return its content as clean Markdown. GitHub repository "
        "links (github.com/<owner>/<repo>) resolve to the repository's README. "
        "Respects robots.txt; 10 s timeout.",
        WEB_TOOL_SCHEMA,
        _fetch_url,
    )
    registry.register(
        "list_files",
        "List the files and subdirectories of a directory as sorted relative paths "
        "(directories end in '/'). Skips .git, __pycache__ and .pytest_cache.",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to list, relative to the current working directory",
                },
                "max_entries": {
                    "type": "integer",
                    "description": "Maximum number of entries to return (default 200)",
                },
            },
            "required": [],
        },
        list_files,
    )

    async def _repo_tree(
        owner: str, repo: str, path: str = "", ref: str | None = None
    ) -> str:
        return await _ghrepo.repo_tree(owner, repo, path, ref)

    async def _repo_file(
        owner: str, repo: str, path: str, ref: str | None = None
    ) -> str:
        return await _ghrepo.repo_file(owner, repo, path, ref)

    async def _repo_search(owner: str, repo: str, query: str) -> str:
        return await _ghrepo.repo_search(owner, repo, query)

    async def _repo_issues(owner: str, repo: str, state: str = "open") -> str:
        return await _ghrepo.repo_issues(owner, repo, state)

    registry.register(
        "repo_tree",
        "List the files and folders of a GitHub repository path at an optional git ref.",
        REPO_TREE_SCHEMA,
        _repo_tree,
    )
    registry.register(
        "repo_file",
        "Return the raw text content of one file in a GitHub repository (capped at 50 KB).",
        REPO_FILE_SCHEMA,
        _repo_file,
    )
    registry.register(
        "repo_search",
        "Search for code in one GitHub repository (needs a GitHub token for private repos).",
        REPO_SEARCH_SCHEMA,
        _repo_search,
    )
    registry.register(
        "repo_issues",
        "Summarize open/closed/all issues of a GitHub repository (pull requests excluded).",
        REPO_ISSUES_SCHEMA,
        _repo_issues,
    )
    return registry


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
    """Cap total length at `limit` characters (result is ALWAYS <= limit);
    truncate the middle (like postmortem.evidence)."""
    if len(text) <= limit:
        return text
    marker = "\n…[snapshot truncated]…\n"
    if limit <= len(marker):
        return text[:limit]
    keep = max(0, (limit - len(marker)) // 2)
    return (text[:keep] + marker + text[-keep:])[:limit]


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
        L.append(
            f"Run {rid}: {run.get('state') or '-'}"
            f"{' - ' + run['state_reason'] if run.get('state_reason') else ''}"
        )
        L.append(
            f"  mode: {run.get('mode') or '-'} | issue: {run.get('issue_title') or '-'} "
            f"| repo: {run.get('repo') or '-'}"
        )
        L.append(
            f"  created: {datetime.fromtimestamp(created, UTC).strftime('%Y-%m-%d %H:%M') if created else '-'}"
            f" | finished: {datetime.fromtimestamp(finished, UTC).strftime('%Y-%m-%d %H:%M') if finished else '-'}"
            f" | elapsed: {elapsed:.0f} min"
        )
        L.append(
            f"  cost: ${cost:.3f} of ${run.get('budget_usd') or 0:.2f} "
            f"| repair cycle: {run.get('repair_cycle') or 0} "
            f"| pr: {run.get('pr_url') or '-'}"
        )
        tasks = store.tasks(run["id"])
        for t in tasks:
            L.append(
                f"  [{t.get('state')}] {t.get('task_key')} "
                f"({t.get('role')}, {t.get('model')}, cycle {t.get('cycle')}): "
                f"{t.get('title') or ''}"
            )
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
        L.append(
            f"  - run {(f.get('run_id') or '')[:8]}: {f.get('category') or '-'} "
            f"| root cause: {_trunc(f.get('root_cause'))} "
            f"| outcome: {f.get('outcome') or '-'} | retry: {bool(f.get('retry'))}"
        )

    L.append("Queue:")
    items = store.queue_items()
    if not items:
        L.append("  empty")
    for q in items:
        L.append(
            f"  - [{q.get('status')}] {q.get('title') or '-'} (repo: {q.get('repo') or '-'})"
        )

    approvals = len(store.pending_approvals())
    L.append(f"Pending approvals: {approvals}")

    return _cap("\n".join(L))


def build_user_content(question: str, attachments) -> str | list[dict]:
    """Build the user message content for PATH attachments (dashboard uploads):
    the plain question when there are no attachments, otherwise an OpenAI-style
    block list (text block with the question plus an 'Attached files:' listing,
    then one image_url data-URL block per image attachment). Unreadable files
    are skipped, never raised."""
    from . import attachments as att

    if not attachments:
        return question
    lines = [question, "", "Attached files:"]
    image_blocks: list[dict] = []
    for path in attachments:
        try:
            kind = att.classify(path)
            text = att.inline_text(path)
            if text is not None:
                lines.append(f"**{path.name}**")
                lines.append("```")
                lines.append(text)
                lines.append("```")
            else:
                lines.append(f"- {path.name} ({kind})")
            if kind == "image":
                image_blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": att.image_data_url(path)},
                    }
                )
        except OSError:
            continue
    blocks: list[dict] = [{"type": "text", "text": "\n".join(lines)}]
    blocks.extend(image_blocks)
    return blocks


def build_attachment_message(
    cfg: Config, attachments: list[dict] | None, conversation: str | None
) -> dict | None:
    """Build an extra user message carrying the request's DICT attachments
    ({"name": str, "data": bytes}).

    Returns None when there are no attachments (zero behavior change).
    Files are persisted via swarmforge.attachments.save_attachments under
    `<workdir>/chat/attachments/<conversation-or-uuid>/`. Text files are
    inlined (capped at MAX_INLINE_ATTACHMENT_BYTES); images become OpenAI-style
    image_url data-URL blocks (up to MAX_CHAT_IMAGES, each <= MAX_IMAGE_BYTES);
    other/oversized/over-quota files get a short stored-path note. Never raises
    on undecodable content.
    """
    from .attachments import classify, data_url, save_attachments

    if not attachments:
        return None
    target = cfg.workdir / "chat" / "attachments" / (conversation or uuid.uuid4().hex)
    saved = save_attachments(target, attachments)
    texts: list[str] = []
    images: list[dict] = []
    for attachment, path in zip(attachments[: len(saved)], saved):
        name = str(attachment.get("name") or "file")
        data = attachment.get("data", b"")
        if not isinstance(data, bytes):
            data = str(data).encode("utf-8")
        kind = classify(name)
        if kind == "image":
            if len(images) >= MAX_CHAT_IMAGES:
                texts.append(
                    f'Image attachment "{name}" skipped (more than {MAX_CHAT_IMAGES} images); '
                    f"stored at {path}."
                )
            elif len(data) > MAX_IMAGE_BYTES:
                texts.append(
                    f'Image attachment "{name}" is too large to forward '
                    f"({len(data)} bytes > {MAX_IMAGE_BYTES}); stored at {path}."
                )
            else:
                images.append(
                    {"type": "image_url", "image_url": {"url": data_url(name, data)}}
                )
            continue
        if kind == "text":
            content = _cap(
                data.decode("utf-8", errors="replace"), MAX_INLINE_ATTACHMENT_BYTES
            )
            texts.append(
                f'Attached file "{name}" (first {MAX_INLINE_ATTACHMENT_BYTES} bytes):\n{content}'
            )
        else:
            texts.append(
                f'Attachment "{name}" stored at {path} '
                "(not inlined: binary or oversized)."
            )
    if not images:
        return {"role": "user", "content": "\n\n".join(texts)}
    text = "\n\n".join(texts + ["See the image attachments below."])
    return {"role": "user", "content": [{"type": "text", "text": text}, *images]}


def _dict_attachments(attachments) -> bool:
    """True when attachments are chatbot-style {"name", "data"} dicts (as
    opposed to dashboard-style saved Paths)."""
    return bool(attachments) and isinstance(attachments[0], dict)


async def _run_chat(cfg: Config, llm, role: str, messages: list[dict], on_tool=None) -> str:
    """One chat turn: the tool loop when chat tools are enabled, a plain
    llm.chat call otherwise (no tools in the request at all)."""
    if cfg.chat.tools_enabled:
        return await chat_with_tools(llm, role, messages, build_chat_registry(cfg),
                                     max_rounds=cfg.chat.max_tool_rounds,
                                     task_key="chatbot", on_tool=on_tool)
    resp = await llm.chat(role, messages, task_key="chatbot")
    return resp.message.get("content") or ""


async def answer(
    cfg: Config,
    store: Store,
    question: str,
    history: list[dict] | None = None,
    *,
    conversation: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    max_runs: int = 12,
    budget=None,
    attachments: list | None = None,
    on_tool=None,
) -> dict:
    """Ask the status chatbot a question; returns {answer, role, model, question}.

    With `conversation` set, the question and reply are persisted to that
    conversation, the prompt context comes only from that conversation's stored
    messages (truncated to the configured window; `history` is ignored), and the
    result additionally carries "conversation" and "usage" (per-conversation
    cost summary).

    `attachments` is an optional keyword-only list, in one of two shapes:
    - dashboard-style saved ``Path`` objects: the last user message becomes a
      multimodal block list (text + image_url data-URL blocks);
    - chatbot-style ``{"name": str, "data": bytes}`` dicts: text files are
      inlined into an extra user message (size-capped); images are forwarded
      as image_url blocks — vision models receive them unchanged, non-vision
      models get a placeholder note via llm.py message normalization.
    Attachments are per-request and are NOT stored in the conversation DB;
    dict attachments are saved to disk under <workdir>/chat/attachments/."""
    from .llm import Budget, BudgetExceeded, LLMClient, LLMError

    role = resolve_role(cfg)
    messages: list[dict] = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
            + "\n"
            + IMPROVE_PROMPT
            + "\n\n<status>\n"
            + status_snapshot(store, max_runs)
            + "\n</status>\n\n<improvement_context>\n"
            + improvement_context(cfg, store)
            + "\n</improvement_context>",
        }
    ]
    if conversation:
        store.conversation_append(conversation, "user", question)
        msgs = store.conversation_messages(conversation)
        reserved = estimate_tokens(messages)
        context = truncate_context(
            msgs,
            cfg.chat.context_max_messages,
            cfg.chat.context_max_tokens,
            reserved_tokens=reserved,
        )
        for m in context:
            messages.append({"role": m["role"], "content": m["content"]})
        if attachments:
            if _dict_attachments(attachments):
                att_msg = build_attachment_message(cfg, attachments, conversation)
                if att_msg is not None:
                    messages.append(att_msg)
            else:
                messages[-1] = {
                    "role": "user",
                    "content": build_user_content(question, attachments),
                }
        run_id = CHAT_RUN_PREFIX + conversation
        llm_budget = (
            budget
            if budget is not None
            else Budget(
                max(2.0, cfg.limits.budget_usd), store.cost_summary(run_id)["total"]
            )
        )
        try:
            llm = LLMClient(
                cfg,
                store=store,
                run_id=run_id,
                budget=llm_budget,
                transport=transport,
                record_errors=False,
            )
            text = await _run_chat(cfg, llm, role, messages, on_tool)
        except (LLMError, BudgetExceeded, ConfigError, httpx.HTTPError) as e:
            raise ChatError(str(e)) from e
        text, actions = parse_actions(text, store)
        store.conversation_append(conversation, "assistant", text)
        return {
            "answer": text,
            "role": role,
            "model": cfg.roles[role].model,
            "question": question,
            "actions": actions,
            "conversation": conversation,
            "usage": store.cost_summary(run_id),
        }
    for m in history or []:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            messages.append({"role": m["role"], "content": str(m["content"])})
    if attachments and _dict_attachments(attachments):
        messages.append({"role": "user", "content": question})
        att_msg = build_attachment_message(cfg, attachments, conversation)
        if att_msg is not None:
            messages.append(att_msg)
    else:
        messages.append(
            {"role": "user", "content": build_user_content(question, attachments)}
        )
    try:
        llm = LLMClient(
            cfg, store=None, run_id=None, budget=Budget(2.0), transport=transport
        )
        text = await _run_chat(cfg, llm, role, messages, on_tool)
    except (LLMError, BudgetExceeded, ConfigError, httpx.HTTPError) as e:
        raise ChatError(str(e)) from e
    text, actions = parse_actions(text, store)
    return {
        "answer": text,
        "role": role,
        "model": cfg.roles[role].model,
        "question": question,
        "actions": actions,
    }


def ask(
    cfg: Config,
    store: Store,
    question: str,
    history: list[dict] | None = None,
    *,
    conversation: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    max_runs: int = 12,
    attachments: list | None = None,
    on_tool=None,
) -> dict:
    """Synchronous wrapper around answer() for CLI and Streamlit callers."""
    return asyncio.run(
        answer(
            cfg,
            store,
            question,
            history,
            conversation=conversation,
            transport=transport,
            max_runs=max_runs,
            attachments=attachments,
            on_tool=on_tool,
        )
    )


# ======================================================== conversation persistence
DEFAULT_CONVERSATION_TITLE = "General"


def create_conversation(
    store: Store,
    title: str = DEFAULT_CONVERSATION_TITLE,
    last_run_id: str | None = None,
) -> dict:
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
        "SELECT * FROM conversations ORDER BY updated_at DESC, id DESC LIMIT ?",
        (limit,),
    )


def add_message(store: Store, conversation_id: str, role: str, content: str,
                tool_calls: list[dict] | None = None) -> dict:
    """Append a message to a conversation; bumps the conversation's updated_at.

    `tool_calls` is an optional list of {"name", "args", "output"} dicts stored
    as JSON in the chat_messages.tool_calls column (NULL when omitted)."""
    if get_conversation(store, conversation_id) is None:
        raise ChatError(f"unknown conversation {conversation_id}")
    now = time.time()
    calls_json = json.dumps(tool_calls) if tool_calls else None
    with store._lock:
        cur = store.conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, created_at, tool_calls) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversation_id, role, content, now, calls_json),
        )
        store.conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
        store.conn.commit()
        msg_id = cur.lastrowid
    msg = store._one("SELECT * FROM chat_messages WHERE id = ?", (msg_id,))
    assert msg is not None
    return msg


def get_messages(
    store: Store, conversation_id: str, limit: int | None = None
) -> list[dict]:
    """Messages of a conversation, oldest first. With limit, the last `limit` messages."""
    if limit is None:
        return store._all(
            "SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        )
    rows = store._all(
        "SELECT * FROM chat_messages WHERE conversation_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (conversation_id, limit),
    )
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
            "DELETE FROM chat_messages WHERE conversation_id = ?", (conversation_id,)
        )
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
        store.conn.execute(
            "DELETE FROM chat_messages WHERE conversation_id = ?", (conversation_id,)
        )
        cur = store.conn.execute(
            "DELETE FROM conversations WHERE id = ?", (conversation_id,)
        )
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
        outcome = f.get("outcome") or ""
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
        heads = [
            h[3:].strip()
            for h in (root / "README.md").read_text(encoding="utf-8").splitlines()
            if h.startswith("## ")
        ]
        L.append("Features (README sections): " + "; ".join(heads[:40]))
    except OSError:
        pass
    mods = []
    for f in sorted((root / "src" / "swarmforge").glob("*.py")):
        try:
            m = re.match(
                r'\s*(?:from __future__[^\n]*\n)?\s*"""(.*?)(?:\n|""")',
                f.read_text(encoding="utf-8"),
                re.S,
            )
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
        durs = sorted(
            ((r.get("finished_at") or r["created_at"]) - r["created_at"]) / 60
            for r in done
        )
        costs = [store.cost_summary(r["id"]).get("total") or 0 for r in done]
        cats: dict[str, int] = {}
        for f in store.failures(limit=100):
            cats[f.get("category") or "?"] = cats.get(f.get("category") or "?", 0) + 1
        L.append(
            f"Last {len(done)} finished runs: {ok} completed, {len(done) - ok} not; median "
            f"{durs[len(durs) // 2]:.0f} min; average ${sum(costs) / len(costs):.2f}. Failure categories: "
            + ", ".join(
                f"{k}={v}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1])
            )
        )
    # failures that need a decision
    fails = failures_needing_attention(store)
    if fails:
        from .postmortem import resumable

        L.append("Failures needing Dan's decision (use these full run ids in fixes):")
        for f in fails:
            run = store.get_run(f["run_id"]) or {}
            L.append(
                f"- run {f['run_id']} | task: {_trunc(f.get('title') or run.get('issue_title'), 120)} | "
                f"repo: {f.get('repo') or run.get('repo')} | category: {f.get('category')} | "
                f"time limit {((run.get('max_runtime_s') or 0) / 3600):.1f} h, budget ${run.get('budget_usd') or 0:.0f}, "
                f"repair cycles {run.get('max_repair_cycles')} | resumable: {resumable(store, f['run_id'])}\n"
                f"  root cause: {_trunc(f.get('root_cause'), 400)}\n  suggested fix: {_trunc(f.get('fix'), 300)}"
            )
    # lessons from the knowledge base
    try:
        from .knowledge import Vault

        notes = [
            n
            for n in Vault(cfg.vault_path).notes()
            if n.type
            in ("pitfall", "review-lesson", "failure", "user-note", "model-behavior")
        ]
        notes.sort(key=lambda n: n.created or "", reverse=True)
        if notes:
            L.append("Lessons learned (newest first):")
            L += [
                f"- [{n.type}] {n.title}: {_trunc(n.body.replace(chr(10), ' '), 160)}"
                for n in notes[:15]
            ]
    except Exception:  # noqa: BLE001
        pass
    # don't suggest what's already planned
    planned = [
        q["title"] for q in store.queue_items() if q["status"] in ("queued", "running")
    ]
    try:
        from .runqueue import parse_backlog

        bl = root / "docs" / "SELF_IMPROVEMENT_BACKLOG.md"
        if bl.is_file():
            planned += [b.title for b in parse_backlog(bl.read_text(encoding="utf-8"))]
    except Exception:  # noqa: BLE001
        pass
    recent = [r.get("issue_title") for r in runs if r.get("issue_title")]
    if planned:
        L.append(
            "Already queued or in the backlog (don't duplicate): "
            + "; ".join(_trunc(t, 80) for t in planned[:30])
        )
    if recent:
        L.append(
            "Recently worked on: "
            + "; ".join(_trunc(t, 80) for t in dict.fromkeys(recent).keys())
        )
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
    shown = ((text[: m.start()] + text[m.end() :]) if text else "").strip()
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return shown, actions
    for t in (data.get("tasks") or [])[:8]:
        if (
            isinstance(t, dict)
            and str(t.get("title") or "").strip()
            and str(t.get("description") or "").strip()
        ):
            actions["tasks"].append(
                {
                    "title": str(t["title"]).strip()[:150],
                    "description": str(t["description"]).strip()[:6000],
                    "why": str(t.get("why") or "").strip()[:800],
                    "size": str(t.get("size") or "M").upper()[:1]
                    if str(t.get("size") or "M").upper()[:1] in SIZES
                    else "M",
                    "repo": str(t.get("repo") or "").strip()[:200],
                }
            )
    for f in (data.get("fixes") or [])[:10]:
        if not isinstance(f, dict):
            continue
        run = (
            store.get_run(str(f.get("run_id") or ""))
            if store is not None
            else {"id": f.get("run_id")}
        )
        if not run:
            continue
        opts = []
        for o in (f.get("options") or [])[:5]:
            if isinstance(o, dict) and str(o.get("instructions") or "").strip():
                opts.append(
                    {
                        "label": str(o.get("label") or "Retry")[:80],
                        "instructions": str(o["instructions"])[:3000],
                        "time_limit_h": _num(o.get("time_limit_h"), 0.1, 72),
                        "budget_usd": _num(o.get("budget_usd"), 0.5, 500),
                        "max_cycles": int(_num(o.get("max_cycles"), 0, 10))
                        if _num(o.get("max_cycles"), 0, 10) is not None
                        else None,
                        "continue_code": bool(o.get("continue_code")),
                    }
                )
        if opts:
            actions["fixes"].append(
                {
                    "run_id": run["id"],
                    "summary": str(f.get("summary") or "")[:300],
                    "options": opts,
                }
            )
    return shown, actions
