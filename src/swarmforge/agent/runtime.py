"""The SwarmForge coding-agent runtime: a tool-calling loop over an OpenAI-compatible model.

Why a built-in runtime instead of OpenCode: see docs/ARCHITECTURE.md ("Agent runtime").
In short: every command an agent issues must go through the Docker sandbox, every model call
must be metered for tokens/cost against the run budget, and each role must get a restricted
toolset. Owning the loop makes those guarantees enforceable in code.
"""
from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..llm import BudgetExceeded, LLMClient, LLMError
from .tools import (ASYNC_IMPLS, SYNC_IMPLS, TOOL_SCHEMAS, TerminalTool, ToolContext, ToolError,
                    parse_args)

COMPACT_AFTER_CHARS = 160_000   # total transcript size before old tool outputs get trimmed
KEEP_RECENT_TOOL_MSGS = 8
MAX_NUDGES = 3
GRACE_CALLS = 2  # wrap-up calls allowed after a time/step limit is hit


class AgentCancelled(RuntimeError):
    pass


@dataclass
class AgentSpec:
    role: str
    task_key: str
    system_prompt: str
    user_prompt: str
    tools: list[str]
    terminal: TerminalTool
    ctx: ToolContext
    max_steps: int = 40
    timeout_s: int = 900
    transcript_path: Path | None = None
    should_cancel: Callable[[], bool] | None = None
    on_event: Callable[[str, str, dict | None], None] | None = None  # (kind, message, data)


@dataclass
class AgentResult:
    status: str               # "completed" | "max_steps" | "timeout" | "error" | "cancelled" | "budget"
    value: Any = None         # validated terminal tool payload
    steps: int = 0
    error: str | None = None
    tool_calls: int = 0
    duration_s: float = 0.0
    files_written: list[str] = field(default_factory=list)
    commands_run: list[dict] = field(default_factory=list)
    hit_limit: str | None = None  # set if the result was submitted during the wrap-up grace

    @property
    def ok(self) -> bool:
        return self.status == "completed"


def _clean_assistant(msg: dict) -> dict:
    out = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("tool_calls"):
        out["tool_calls"] = [
            {"id": tc.get("id") or f"call_{i}", "type": "function",
             "function": {"name": tc["function"]["name"],
                          "arguments": tc["function"].get("arguments") or "{}"}}
            for i, tc in enumerate(msg["tool_calls"])
        ]
    if msg.get("reasoning_content"):
        out["reasoning_content"] = msg["reasoning_content"]
    return out


def _jsonable(o):
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    return str(o)


def _compact(messages: list[dict]) -> None:
    total = sum(len(json.dumps(m)) for m in messages)
    if total < COMPACT_AFTER_CHARS:
        return
    tool_idx = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    for i in tool_idx[:-KEEP_RECENT_TOOL_MSGS]:
        c = messages[i]["content"]
        if len(c) > 600:
            messages[i]["content"] = c[:300] + "\n...[older tool output trimmed to save context]...\n" + c[-200:]
    for m in messages[2:-KEEP_RECENT_TOOL_MSGS]:
        if m["role"] == "assistant":
            m.pop("reasoning_content", None)


async def run_agent(llm: LLMClient, spec: AgentSpec) -> AgentResult:
    start = time.time()
    deadline = start + spec.timeout_s
    tools = [TOOL_SCHEMAS[t] for t in spec.tools] + [spec.terminal.schema]
    allowed = set(spec.tools)
    messages: list[dict] = [
        {"role": "system", "content": spec.system_prompt},
        {"role": "user", "content": spec.user_prompt},
    ]
    result = AgentResult(status="error")
    nudges = 0
    transcript = spec.transcript_path.open("a", encoding="utf-8") if spec.transcript_path else None

    def log(kind: str, msg: str, data: dict | None = None):
        if spec.on_event:
            spec.on_event(kind, msg, data)

    def tx(obj: dict):
        if transcript:
            transcript.write(json.dumps({"ts": time.time(), **obj}, ensure_ascii=False,
                                        default=_jsonable) + "\n")
            transcript.flush()

    tx({"type": "start", "role": spec.role, "task": spec.task_key,
        "system": spec.system_prompt, "user": spec.user_prompt})
    try:
        step = 0
        grace_left: int | None = None   # set once a limit is hit: wrap-up calls remaining
        limit = ("", "")                # (status, error) to report if the wrap-up fails
        warned = False
        while True:
            step += 1
            result.steps = step
            if spec.should_cancel and spec.should_cancel():
                raise AgentCancelled()
            if grace_left is None:
                if time.time() > deadline:
                    limit = ("timeout", f"agent exceeded {spec.timeout_s}s")
                elif step > spec.max_steps:
                    limit = ("max_steps", f"agent did not finish within {spec.max_steps} steps")
                if limit[0]:
                    grace_left = GRACE_CALLS
                    log("agent", f"{limit[1]}; asking for a final {spec.terminal.name}", None)
                    messages.append({"role": "user", "content": (
                        f"LIMIT REACHED ({limit[1]}). Stop working now. Call `{spec.terminal.name}` "
                        "immediately with your results so far. It is fine to report incomplete work, "
                        "but state clearly what is unfinished or unverified. No other tools are available.")})
            if grace_left is not None:
                if grace_left <= 0:
                    result.status, result.error = limit
                    return result
                grace_left -= 1
            _compact(messages)
            call_tools = [spec.terminal.schema] if grace_left is not None else tools
            resp = await llm.chat(spec.role, messages, tools=call_tools, task_key=spec.task_key)
            amsg = _clean_assistant(resp.message)
            messages.append(amsg)
            tx({"type": "assistant", "step": step, "message": amsg,
                "usage": resp.usage.__dict__, "finish_reason": resp.finish_reason})

            if not amsg.get("tool_calls"):
                if nudges >= MAX_NUDGES and grace_left is None:
                    result.status = "error"
                    result.error = "model stopped calling tools without calling " + spec.terminal.name
                    return result
                nudges += 1
                messages.append({"role": "user", "content": (
                    f"Continue working using the tools. When you are completely done you MUST call "
                    f"the `{spec.terminal.name}` tool; plain-text answers are not accepted.")})
                continue

            for tc in amsg["tool_calls"]:
                name = tc["function"]["name"]
                result.tool_calls += 1
                try:
                    args = parse_args(tc["function"]["arguments"])
                    if name == spec.terminal.name:
                        value, err = spec.terminal.validate(args)
                        if err:
                            out = f"REJECTED: {err}\nFix the problems and call {name} again."
                            log("tool", f"{name} rejected: {err[:200]}", {"tool": name})
                        else:
                            tx({"type": "terminal", "tool": name, "value": value, "after_limit": bool(limit[0])})
                            log("tool", f"{name} accepted" + (" (after limit)" if limit[0] else ""), {"tool": name})
                            result.status, result.value = "completed", value
                            result.hit_limit = limit[1] or None
                            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "accepted"})
                            return result
                    elif grace_left is not None:
                        out = f"ERROR: the limit was reached; only `{spec.terminal.name}` may be called now."
                    elif name not in allowed:
                        out = f"ERROR: tool '{name}' is not available to the {spec.role} role."
                    elif name in ASYNC_IMPLS:
                        log("tool", f"{name}: {str(args.get('command') or args.get('url') or args.get('name') or args.get('paths') or '')[:160]}", {"tool": name})
                        out = await ASYNC_IMPLS[name](spec.ctx, **args)
                    else:
                        out = SYNC_IMPLS[name](spec.ctx, **args)
                        if name in ("write_file", "edit_file", "delete_file"):
                            log("tool", out, {"tool": name})
                except ToolError as e:
                    out = f"ERROR: {e}"
                except TypeError as e:
                    out = f"ERROR: bad arguments for {name}: {e}"
                except (OSError, UnicodeError) as e:
                    out = f"ERROR: {type(e).__name__}: {e}"
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
                tx({"type": "tool", "name": name, "args": tc["function"]["arguments"][:4000],
                    "output": out[:8000]})
            # budget reminders so agents wrap up before the hard limit
            remaining = deadline - time.time()
            steps_left = spec.max_steps - step
            if not warned and grace_left is None and (remaining < 0.25 * spec.timeout_s or steps_left <= 3):
                warned = True
                messages[-1]["content"] += (
                    f"\n\n[SwarmForge: about {max(0, int(remaining // 60))} min and {max(0, steps_left)} "
                    f"step(s) left. Wrap up and call `{spec.terminal.name}` soon.]")
    except AgentCancelled:
        result.status, result.error = "cancelled", "run cancelled"
        return result
    except BudgetExceeded as e:
        result.status, result.error = "budget", str(e)
        return result
    except LLMError as e:
        result.status, result.error = "error", str(e)
        return result
    except Exception as e:  # never let an agent crash the orchestrator silently
        result.status, result.error = "error", f"{type(e).__name__}: {e}"
        return result
    finally:
        result.duration_s = time.time() - start
        result.files_written = sorted(spec.ctx.files_written)
        result.commands_run = list(spec.ctx.commands_run)
        tx({"type": "end", "status": result.status, "error": result.error, "steps": result.steps})
        if transcript:
            transcript.close()
