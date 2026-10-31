"""Failure post-mortems and self-healing.

Every run that does not complete gets a post-mortem: a deterministic category, the evidence (failed
tasks, errors, last tests, unresolved review findings), and, when possible, a diagnosis from a model
answering "why did this fail and what should the next attempt do differently?". Post-mortems are
stored in the `failures` table (dashboard -> Failures), in `<run dir>/postmortem.md`, and as a
`failure` note in the knowledge vault so later runs on the same repository learn from them.

Self-healing: when a run fails for a reason a retry can fix, a new run continues from the failed
run's code with the diagnosis added to the task and more effort (time, budget, repair cycles, agent
steps). The number of attempts and the total spend are capped.
"""
from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .config import Config
from .db import Store

CATEGORIES = {
    "time_limit": "Ran out of time",
    "budget": "Ran out of budget",
    "model_unavailable": "Model provider busy or unavailable",
    "environment": "Environment problem (Docker, keys, sandbox)",
    "repository": "Repository problem",
    "stale_base": "Base branch moved since the previous attempt",
    "planning": "Planner could not make a valid plan",
    "tests_failing": "Tests kept failing",
    "review_rejected": "Reviewer kept rejecting the work",
    "agent_failure": "Coding agents could not finish their tasks",
    "crash": "Unexpected error inside SwarmForge",
    "other": "Other",
}
# failures a new attempt (with more effort) can plausibly fix; the rest need a human
RETRYABLE = {"stale_base", "time_limit", "budget", "model_unavailable", "planning", "tests_failing", "review_rejected",
             "agent_failure", "crash", "other"}
FAILED_STATES = {"FAILED", "BLOCKED", "BUDGET_EXCEEDED"}

SYSTEM = """You are the post-mortem analyst of SwarmForge, an autonomous multi-agent software team
(planner -> parallel developers -> integration -> tester -> independent reviewer -> repair cycles).
A run did not complete. From the evidence, work out WHY it failed (the root cause, not the symptom)
and what the next attempt must do differently so it succeeds.

Answer with ONE JSON object and nothing else:
{
  "category": one of %s,
  "root_cause": "2-4 sentences: the real reason, citing the evidence",
  "fix": "concrete instructions for the next attempt (what to do first, what to avoid, what to verify)",
  "retry": true/false  (false only if a human must act first, e.g. Docker down, missing key, bad repo,
                        or the task itself is impossible/contradictory),
  "lesson": "one sentence a future run on ANY task could learn from"
}"""


@dataclass
class Diagnosis:
    category: str
    root_cause: str
    fix: str
    retry: bool
    lesson: str
    diagnosed_by: str = "rules"


# ------------------------------------------------------------------ evidence
def classify(run: dict, events: list[dict], tasks: list[dict], tests: list[dict]) -> str:
    reason = (run.get("state_reason") or "").lower()
    errors = " ".join(e["message"].lower() for e in events if e["level"] in ("error", "warn"))[-4000:]
    state = run["state"]
    if state == "CANCELLED":
        return "cancelled"
    if state == "BUDGET_EXCEEDED" or "budget" in reason and "exhausted" in reason:
        return "budget"
    if "maximum runtime" in reason:
        return "time_limit"
    if "unexpected error" in reason or "process running this run has exited" in reason:
        return "crash"
    busy = re.compile(r"\b(429|503|529)\b|busy|overloaded|rate.?limit|unavailable|no model could")
    if busy.search(reason):
        return "model_unavailable"
    if re.search(r"is not based on|moved on since the previous", reason):
        return "stale_base"
    if state == "BLOCKED":
        if re.search(r"repositor|clone|empty|branch|not a git", reason):
            return "repository"
        return "environment"
    if re.search(r"docker|sandbox image|environment variable|api key", reason):
        return "environment"
    if "repair limit" in reason or "fix_required" in reason:
        real = [t for t in tests if t["label"] != "baseline"]
        if real and (real[-1]["exit_code"] != 0 or real[-1]["failed"] or real[-1]["errors"]):
            return "tests_failing"
        return "review_rejected"
    if "plan" in reason:
        return "planning"
    if any(t["state"] in ("FAILED", "BLOCKED") for t in tasks):
        return "agent_failure"
    if busy.search(errors):
        return "model_unavailable"
    return "other"


def evidence(store: Store, run_id: str) -> str:
    run = store.get_run(run_id)
    events = store.events(run_id)
    tasks = store.tasks(run_id)
    tests = store.test_runs(run_id)
    cost = store.cost_summary(run_id)
    elapsed = (run["finished_at"] or time.time()) - run["created_at"]
    try:
        limits = json.loads(run["config_json"] or "{}").get("limits", {})
    except json.JSONDecodeError:
        limits = {}
    L = [f"Task: {run['issue_title'] or run['issue_url'] or '-'}",
         f"Repository: {run['repo'] or '-'}  mode: {run['mode']}",
         f"Outcome: {run['state']} - {run['state_reason'] or ''}",
         f"Ran {elapsed / 60:.0f} min, spent ${cost['total']:.3f} of ${run['budget_usd'] or 0:.2f}; "
         f"time limit {(run.get('max_runtime_s') or limits.get('max_runtime_s') or 0) / 3600:.2f} h; "
         f"repair cycle reached {run['repair_cycle']} of max "
         f"{run.get('max_repair_cycles') if run.get('max_repair_cycles') is not None else limits.get('max_repair_cycles', '?')}"]
    if run.get("heal_attempt"):
        L.append(f"This run was already self-healing attempt {run['heal_attempt']} (previous run {run['heal_of']}).")
    states = [json.loads(e["data"]).get("to") for e in events if e["kind"] == "state" and e["data"]]
    L.append("Phases visited: " + " -> ".join(s for s in states if s))
    if tasks:
        L.append("\nTasks:")
        for t in tasks:
            line = f"- [{t['state']}] {t['task_key']} ({t['role']}, cycle {t['cycle']}): {t['title'] or ''}"
            if t.get("error"):
                line += f"\n    error: {str(t['error'])[:400]}"
            elif t.get("summary") and t["state"] != "INTEGRATED":
                line += f"\n    reported: {str(t['summary'])[:300]}"
            L.append(line)
    real = [t for t in tests if t["label"] != "baseline"]
    if real:
        L.append("\nTest runs: " + "; ".join(
            f"{t['label']} c{t['cycle']}: {t['passed']} passed, {t['failed']} failed, {t['errors']} errors"
            + (" (timed out)" if t["timed_out"] else "") for t in real[-4:]))
        log = real[-1].get("log_path")
        if log and Path(log).is_file() and (real[-1]["failed"] or real[-1]["errors"] or real[-1]["exit_code"]):
            tail = Path(log).read_text(encoding="utf-8", errors="replace")[-2500:]
            L.append("Tail of the last test log:\n" + tail)
    blocking = [f for f in store.findings(run_id, run["repair_cycle"]) if f["severity"] in ("critical", "major")]
    if blocking:
        L.append("\nUnresolved review/test findings (last cycle):")
        L += [f"- [{f['severity']}] {f['file'] or ''}: {f['title']} -> {(f['recommendation'] or '')[:250]}"
              for f in blocking[:10]]
    bad = [e for e in events if e["level"] in ("error", "warn")][-20:]
    if bad:
        L.append("\nLast warnings/errors:")
        L += [f"- [{e['kind']}] {e['message'][:300]}" for e in bad]
    tail = [e for e in events if e["level"] == "info"][-8:]
    if tail:
        L.append("\nLast activity:")
        L += [f"- [{e['kind']}] {e['message'][:200]}" for e in tail]
    return "\n".join(L)[:14000]


def rules_diagnosis(category: str, run: dict) -> Diagnosis:
    reason = run.get("state_reason") or ""
    fixes = {
        "time_limit": "Give the run more time, keep tasks smaller, and avoid slow full-suite runs inside agents.",
        "budget": "Give the run more budget, or split the task into smaller ones.",
        "model_unavailable": "Retry later; the provider was busy. Fallback models are tried automatically.",
        "environment": "Fix the environment first (start Docker, build the sandbox image, set API keys), then retry.",
        "repository": "Check the repository URL/branch and that it is not empty, then retry.",
        "stale_base": ("New commits landed on the base branch after the previous attempt. Continue merges "
                       "them into the old work automatically; if that conflicts, start again from scratch."),
        "planning": "Make the task description more concrete (interfaces, files, acceptance criteria).",
        "tests_failing": "Start by running the failing tests and fixing their root cause before anything else.",
        "review_rejected": "Address every unresolved review finding explicitly and verify each with a test.",
        "agent_failure": "Inspect the failed tasks' errors; give smaller, more specific tasks.",
        "crash": "See the Events/Console tabs for the traceback.",
        "other": "See the run's Events tab.",
    }
    return Diagnosis(category=category, root_cause=f"{CATEGORIES.get(category, category)}: {reason}"[:600],
                     fix=fixes.get(category, ""), retry=category in RETRYABLE,
                     lesson="", diagnosed_by="rules")


def parse_diagnosis(text: str, fallback: Diagnosis) -> Diagnosis | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict) or not d.get("root_cause"):
        return None
    cat = d.get("category") if d.get("category") in CATEGORIES else fallback.category
    retry = d.get("retry")
    retry = bool(retry) if isinstance(retry, bool) else fallback.retry
    if fallback.category in ("environment", "repository", "cancelled"):
        retry = False  # a new attempt can't fix these; never loop on them
    return Diagnosis(category=cat, root_cause=str(d["root_cause"])[:2000], fix=str(d.get("fix") or fallback.fix)[:3000],
                     retry=retry, lesson=str(d.get("lesson") or "")[:500])


async def diagnose(cfg: Config, store: Store, run_id: str, *, transport=None) -> Diagnosis | None:
    """Build and store the post-mortem for a run that did not complete. Returns None for completed or
    cancelled runs. Never raises: a broken diagnosis must not hide the failure itself."""
    from .llm import Budget, LLMClient
    run = store.get_run(run_id)
    if not run or run["state"] == "COMPLETED":
        return None
    events, tasks, tests = store.events(run_id), store.tasks(run_id), store.test_runs(run_id)
    if run["state"] == "CANCELLED":
        return None
    if run["state"] not in FAILED_STATES:  # not finished: only a crashed (dead) process counts as a failure
        from .launcher import pid_alive
        if not run["pid"] or pid_alive(run["pid"]):
            return None
        run = {**run, "state_reason": "the process running this run has exited without finishing "
                                      f"(last phase {run['state']})"}
    category = classify(run, events, tasks, tests)
    if category == "cancelled":
        return None
    ev = evidence(store, run_id)
    diag = rules_diagnosis(category, run)
    # environment/repository problems are clear from the rules and need a human; don't spend a model call
    if cfg.self_heal.diagnose and category not in ("environment", "repository"):
        role = cfg.self_heal.diagnosis_role
        try:
            llm = LLMClient(cfg, store, run_id, Budget(cfg.self_heal.diagnosis_budget_usd), transport=transport)
            res = await llm.chat(role, [
                {"role": "system", "content": SYSTEM % json.dumps(list(CATEGORIES))},
                {"role": "user", "content": f"Rule-based first guess: {category}\n\n<evidence>\n{ev}\n</evidence>"}],
                task_key="postmortem")
            got = parse_diagnosis(res.content, diag)
            if got:
                got.diagnosed_by = f"{role}/{cfg.roles[role].model}"
                diag = got
        except Exception as e:  # noqa: BLE001 - keep the rules diagnosis
            store.event(run_id, "postmortem", f"model diagnosis unavailable: {type(e).__name__}: {e}"[:400],
                        level="warn")
    cost = store.cost_summary(run_id)["total"]
    elapsed = (run["finished_at"] or time.time()) - run["created_at"]
    note_stem = _write_note(cfg, run, diag, ev)
    store.failure_save(run_id, title=run["issue_title"] or run["issue_url"] or run_id, repo=run["repo"],
                       state=run["state"], reason=run["state_reason"], category=diag.category,
                       root_cause=diag.root_cause, fix=diag.fix, lesson=diag.lesson, retry=int(diag.retry),
                       diagnosed_by=diag.diagnosed_by, evidence=ev, note_stem=note_stem, cost_usd=cost,
                       elapsed_s=elapsed, outcome=None if diag.retry else "needs you: not retried automatically")
    if run["run_dir"] and Path(run["run_dir"]).is_dir():
        try:
            (Path(run["run_dir"]) / "postmortem.md").write_text(_markdown(run, diag, ev), encoding="utf-8")
        except OSError:
            pass
    store.event(run_id, "postmortem", f"post-mortem: {CATEGORIES.get(diag.category, diag.category)} - "
                                      f"{diag.root_cause[:300]}", data={"category": diag.category,
                                                                        "retry": diag.retry,
                                                                        "by": diag.diagnosed_by})
    return diag


def _markdown(run: dict, d: Diagnosis, ev: str) -> str:
    return "\n".join([
        f"# Post-mortem: {run['issue_title'] or run['id']}", "",
        f"- Run: `{run['id']}` on `{run['repo'] or '-'}`",
        f"- Outcome: **{run['state']}** - {run['state_reason'] or ''}",
        f"- Category: **{CATEGORIES.get(d.category, d.category)}** (diagnosed by {d.diagnosed_by})", "",
        "## Why it failed", d.root_cause, "",
        "## What the next attempt should do", d.fix, "",
        *(["## Lesson", d.lesson, ""] if d.lesson else []),
        "## Evidence", "```", ev, "```", ""])


def _write_note(cfg: Config, run: dict, d: Diagnosis, ev: str) -> str | None:
    if not cfg.knowledge.enabled:
        return None
    try:
        from .knowledge import Vault, slugify
        vault = Vault(cfg.vault_path)
        stem = f"Failure - {run['id'][:8]} - {slugify(run['issue_title'] or run['id'], 50)}"
        body = "\n".join([
            f"SwarmForge run `{run['id']}` failed: **{run['state']}** - {run['state_reason'] or ''}", "",
            f"**Category:** {CATEGORIES.get(d.category, d.category)}", "",
            "## Why it failed", d.root_cause, "",
            "## What to do differently", d.fix, "",
            *(["## Lesson", d.lesson, ""] if d.lesson else []),
            "[[Failures]]"])
        vault.write(f"Failure: {run['issue_title'] or run['id']}", body, type="failure",
                    tags=["failure", d.category], repo=_repo_key(run), run=run["id"],
                    source="orchestrator", stem=stem)
        refresh_failures_hub(vault, cfg)
        return stem
    except Exception:  # noqa: BLE001 - knowledge is best-effort
        return None


def _repo_key(run: dict) -> str | None:
    src = (run.get("repo") or "").rstrip("/\\")
    if not src:
        return None
    m = re.match(r"^(?:https?://|git@)github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", src)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    if re.match(r"^[\w.-]+/[\w.-]+$", src):
        return src
    return Path(src).name or src


def refresh_failures_hub(vault, cfg: Config) -> None:
    notes = sorted((n for n in vault.notes() if n.type == "failure"), key=lambda n: n.stem, reverse=True)
    lines = ["Every SwarmForge run that failed, newest first, with its diagnosis. Regenerated automatically.", ""]
    lines += [f"- [[{n.stem}|{n.title}]]" + (f" · {n.repo}" if n.repo else "") for n in notes]
    vault.write("Failures", "\n".join(lines), type="hub", tags=["hub", "failure"], source="orchestrator",
                stem="Failures")


# --------------------------------------------------------------- self-healing
def resumable(store: Store, run_id: str) -> bool:
    """True if a stopped run left a clone with integrated work that a new run can continue from."""
    r = store.get_run(run_id)
    if not r or not r["integration_branch"] or not r["run_dir"]:
        return False
    if not (Path(r["run_dir"]) / "repo").is_dir():
        return False
    return any(t["state"] == "INTEGRATED" and t["kind"] in ("implement", "fix") for t in store.tasks(run_id))


def escalate(cfg: Config, attempt: int, *, budget_cap: float | None = None, category: str = "") -> Config:
    """A copy of the config with more effort for self-healing attempt `attempt` (1, 2, ...)."""
    sh = cfg.self_heal
    c = copy.deepcopy(cfg)
    c.limits.max_runtime_s = int(min(72 * 3600, cfg.limits.max_runtime_s * sh.time_factor ** attempt))
    budget = cfg.limits.budget_usd * sh.budget_factor ** attempt
    if budget_cap is not None:
        budget = min(budget, budget_cap)
    c.limits.budget_usd = round(max(0.1, budget), 2)
    c.limits.max_repair_cycles = min(10, cfg.limits.max_repair_cycles + sh.extra_repair_cycles * attempt)
    if category == "model_unavailable":
        c.limits.busy_wait_s = int(cfg.limits.busy_wait_s * 2 ** attempt)
    for r in c.roles.values():
        r.max_steps = int(min(200, r.max_steps * sh.steps_factor ** attempt))
        r.timeout_s = int(min(6 * 3600, r.timeout_s * sh.time_factor ** attempt))
    return c


def heal_context(fail: dict, attempt: int, max_attempts: int) -> str:
    return "\n".join([
        f"## Self-healing attempt {attempt} of {max_attempts}: why the previous attempt failed",
        f"The previous run ({fail['run_id']}) failed: {CATEGORIES.get(fail['category'], fail['category'])}.",
        f"Diagnosis: {fail['root_cause']}",
        f"What to do differently now: {fail['fix']}",
        "Take more care than last time: first reproduce and understand the problem (read the code, run the "
        "failing tests), fix the root cause rather than the symptom, and verify every acceptance criterion "
        "with a test before finishing. You have more time, budget and steps than the previous attempt."])


Launch = Callable[[Config, "str | None", "str | None", str, int], Awaitable[str]]


async def heal(cfg: Config, store: Store, run_id: str, launch: Launch, *, attempts: int | None = None,
               echo=None) -> str:
    """After `run_id` finished: while it failed in a fixable way, diagnose and start a new attempt with
    more effort. `launch(cfg, seed_run_id, heal_context, heal_of, attempt)` runs one attempt and returns
    its run id. Returns the id of the last run."""
    sh = cfg.self_heal
    max_attempts = sh.max_attempts if attempts is None else max(0, min(5, attempts))
    first = store.get_run(run_id)
    cap = sh.max_total_budget_usd or 3 * (first["budget_usd"] or cfg.limits.budget_usd)
    chain = [run_id]
    attempt = 0
    while True:
        run = store.get_run(run_id)
        if run["state"] == "COMPLETED" or not sh.enabled or attempt >= max_attempts:
            break
        if run["state"] == "CANCELLED" or run["cancel_requested"]:
            break
        fail = store.failure(run_id)
        if not fail:
            break
        if not fail["retry"]:
            store.failure_update(run_id, outcome="needs you: not retried automatically")
            break
        spent = sum(store.cost_summary(r)["total"] for r in chain)
        left = cap - spent
        if left < 0.25:
            store.failure_update(run_id, outcome=f"not retried: self-healing budget cap ${cap:.2f} reached")
            break
        attempt += 1
        seed = run_id if resumable(store, run_id) else None
        c = escalate(cfg, attempt, budget_cap=left, category=fail["category"])
        if echo:
            echo(f"self-healing: attempt {attempt}/{max_attempts} after {fail['category']} "
                 f"({'continuing from' if seed else 'restarting; nothing to continue from in'} {run_id}); "
                 f"time {c.limits.max_runtime_s / 3600:.1f} h, budget ${c.limits.budget_usd:.2f}, "
                 f"repair cycles {c.limits.max_repair_cycles}")
        store.event(run_id, "selfheal", f"starting self-healing attempt {attempt}/{max_attempts}")
        new_id = await launch(c, seed, heal_context(fail, attempt, max_attempts), run_id, attempt)
        store.failure_update(run_id, heal_run_id=new_id, outcome=f"retrying (attempt {attempt})")
        chain.append(new_id)
        run_id = new_id
    final = store.get_run(run_id)
    if len(chain) > 1:
        msg = (f"fixed itself on attempt {len(chain) - 1}" if final["state"] == "COMPLETED"
               else f"still failing after {len(chain) - 1} self-healing attempt(s)")
        for r in chain[:-1]:
            store.failure_update(r, outcome=msg)
        if store.failure(run_id) and final["state"] != "COMPLETED" and not store.failure(run_id)["outcome"]:
            store.failure_update(run_id, outcome=msg)
    return run_id
