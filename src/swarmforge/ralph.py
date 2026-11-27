"""Ralph mode: one developer agent in a loop of short, fresh passes.

Named after Geoffrey Huntley's "Ralph Wiggum" technique: the same instructions every pass, a fresh
agent with no memory each time, and all memory in files. Each pass the agent reads the plan
(`PLAN.md`, a checklist), its notes (`NOTES.md`) and the pass log (`LOG.md`), does the single most
important unchecked item, and updates the plan and notes.

Code still decides, not the agent: after every pass SwarmForge runs the test suite itself. A pass is
kept (committed to the result branch) only if it changed something and no test fails that passed
before; otherwise the whole pass is thrown away and the log says why, so the next fresh agent sees
it. The loop ends when the plan has no open items, when several passes in a row make no progress,
or before money or time run out, and the result is published like any other run.

The plan files live in `.swarmforge/ralph/` in the agent's working copy but are never committed:
SwarmForge keeps them with the run and in the work log, so a retry or a continuation starts from them.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from . import prompts
from .state import RunAborted, RunState
from .testing import TestEvidence, run_tests

DIR = ".swarmforge/ralph"
FILES = ("PLAN.md", "NOTES.md", "LOG.md")
MAX_PASSES = 60
STUCK_AFTER = 4          # passes in a row without kept progress
MAX_FILE_CHARS = 40_000
OPEN = re.compile(r"^\s*[-*]\s*\[ \]", re.M)
DONE = re.compile(r"^\s*[-*]\s*\[[xX]\]", re.M)

RALPH_SYSTEM = prompts.SECURITY_PREAMBLE + prompts.PRODUCT_VERIFICATION_GUIDANCE + """
You are a senior software engineer working ALONE, in a loop of short passes (Ralph mode). Each pass
starts fresh: you remember nothing from earlier passes. Everything you need is in files:
- `.swarmforge/ralph/PLAN.md`: the checklist for the whole task (`- [ ]` open, `- [x]` done).
- `.swarmforge/ralph/NOTES.md`: what earlier passes learned (how to run things, pitfalls, decisions).
- `.swarmforge/ralph/LOG.md`: written by SwarmForge: what each earlier pass did, which passes were kept,
  and exactly why others were thrown away (failing tests with their errors).

Every pass:
1. Read PLAN.md, NOTES.md and the end of LOG.md first. If the last pass was thrown away, fix what
   broke before anything else and do not repeat the approach the log says failed.
2. If PLAN.md does not exist or is empty, write it first from the task: a checklist of small items,
   each finishable in ONE pass, in a sensible order, ending with an item that checks every acceptance
   criterion with tests. Then do the first item.
3. Do exactly ONE item (the most important open one). Implement it completely, with tests. Read the
   relevant code first; follow the existing style; no placeholders or TODOs.
4. Run the tests relevant to your change and make them pass. Keep every test that passed before
   passing: after your pass SwarmForge runs the full suite, and if anything that passed before now
   fails, your whole pass is thrown away.
5. Update PLAN.md: tick your item, add items you discovered, split items that turned out too big.
   Add short, useful learnings to NOTES.md (commands, gotchas, decisions). Do not edit LOG.md.
6. Finish with a one-paragraph summary: which item you did, what changed, which tests you ran.

Do not run git commands: SwarmForge commits for you. Stay inside the repository.
"""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_CHARS]
    except OSError:
        return ""


def open_items(plan: str) -> int:
    return len(OPEN.findall(plan))


def failing(ev: TestEvidence) -> set[str]:
    return {k for k, v in ev.cases.items() if v in ("failed", "error")}


def acceptable(ev: TestEvidence, before: TestEvidence | None) -> tuple[bool, str]:
    """Keep a pass when the suite is green, or when nothing that passed before fails now."""
    if ev.timed_out:
        return False, f"the test suite timed out after {ev.duration_s:.0f}s"
    if ev.ok:
        return True, ev.short()
    if before is not None and ev.junit_found and before.junit_found and not before.ok:
        broken = failing(ev) - failing(before)
        if not broken and ev.passed >= before.passed:
            return True, f"{ev.short()} (no new failures; {len(failing(ev))} were already failing)"
        return False, "tests that passed before now fail: " + ", ".join(sorted(broken)[:8])
    if not ev.junit_found and ev.exit_code != 0:
        return False, f"the test command failed (exit {ev.exit_code}) without a test report"
    return False, ev.short()


class RalphLoop:
    def __init__(self, orch):
        self.o = orch
        self.state_dir = orch.run_dir / "ralph"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.kept = self.thrown = 0
        self.stopped = ""
        self.pass_costs: list[float] = []
        self.pass_times: list[float] = []
        self.repo_summary = ""

    # ------------------------------------------------------------ plan files
    def _state_key(self) -> str:
        from .worklog import lineage
        return "ralph_files:" + lineage(self.o.store, self.o.run_id)

    def restore(self) -> None:
        """Start from this run's files, else from the last attempt at the same work."""
        if any((self.state_dir / f).exists() for f in FILES):
            return
        try:
            saved = json.loads(self.o.store.kv_get(self._state_key()) or "{}")
        except ValueError:
            saved = {}
        for name in FILES:
            if isinstance(saved.get(name), str) and saved[name].strip():
                (self.state_dir / name).write_text(saved[name], encoding="utf-8")
        if saved:
            self.o.log("ralph", "continuing from the plan, notes and log of the previous attempt at this work")

    def save(self) -> None:
        files = {name: _read(self.state_dir / name) for name in FILES}
        self.o.store.kv_set(self._state_key(), json.dumps(files))

    def plan(self) -> str:
        return _read(self.state_dir / "PLAN.md")

    def _into(self, worktree: Path) -> None:
        d = worktree / DIR
        d.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            src = self.state_dir / name
            if src.exists():
                (d / name).write_text(_read(src), encoding="utf-8")

    def _from(self, worktree: Path, *, keep_plan: bool) -> None:
        d = worktree / DIR
        for name in ("PLAN.md", "NOTES.md"):
            if name == "PLAN.md" and not keep_plan:
                continue
            text = _read(d / name)
            if text.strip():
                (self.state_dir / name).write_text(text, encoding="utf-8")

    def append_log(self, text: str) -> None:
        path = self.state_dir / "LOG.md"
        log = _read(path) or "# Ralph pass log (written by SwarmForge)\n"
        log = log.rstrip() + "\n\n" + text.strip() + "\n"
        if len(log) > MAX_FILE_CHARS:  # keep the most recent passes
            tail = log[-(MAX_FILE_CHARS - 500):]
            i = tail.find("\n## Pass ")
            log = "# Ralph pass log (written by SwarmForge)\n\n(older passes trimmed)\n\n" + (tail[i + 1:] if i >= 0 else tail)
        path.write_text(log, encoding="utf-8")

    def _exclude(self) -> None:
        """The plan files are SwarmForge's working memory, not part of the product: never commit them."""
        exclude = self.o.ws.repo / ".git" / "info" / "exclude"
        try:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if f"/{DIR}/" not in text:
                exclude.write_text(text.rstrip("\n") + f"\n/{DIR}/\n", encoding="utf-8")
        except OSError as e:
            self.o.log("ralph", f"could not exclude the plan files from commits: {e}", level="warn")

    # ---------------------------------------------------------------- guards
    def _room(self) -> str | None:
        """Why another pass would not fit in the money or time left (None = it fits)."""
        from .execution import snapshot
        snap = snapshot(self.o.store, self.o.run_id) or {}
        left_usd = snap.get("remaining_usd")
        if left_usd is None:
            left_usd = self.o.budget.limit - self.o.budget.spent
        need_usd = max(.05, 1.3 * (sum(self.pass_costs[-5:]) / len(self.pass_costs[-5:]))) if self.pass_costs else .05
        if left_usd < need_usd:
            return f"the money left (${left_usd:.2f}) would not cover another pass (about ${need_usd:.2f})"
        left_s = self.o.cfg.limits.max_runtime_s - (time.monotonic() - self.o._started)
        if snap.get("remaining_s") is not None:
            left_s = min(left_s, snap["remaining_s"])
        need_s = 1.3 * max(self.pass_times[-5:]) + 60 if self.pass_times else 60
        if left_s < need_s:
            return f"the time left ({left_s / 60:.0f} min) would not fit another pass (about {need_s / 60:.0f} min)"
        return None

    # ------------------------------------------------------------------ loop
    def _prompt(self, n: int) -> str:
        o = self.o
        plan = self.plan()
        parts = [prompts.render_issue(o.issue.title, o.issue.body),
                 "## Repository at the start of the task\n" + self.repo_summary[:4000],
                 f"## Ralph pass {n}",
                 ("PLAN.md has " + f"{open_items(plan)} open and {len(DONE.findall(plan))} done item(s)."
                  if plan.strip() else "There is no PLAN.md yet: write it first, then do the first item."),
                 o._test_hint()]
        if o.knowledge_dev_block:
            parts.append(o.knowledge_dev_block)
        if o.cfg.roles[o.single_role].runtime == "pi":
            parts.append('End with a JSON object {"summary": "which item you did, what changed, which tests you ran"}.')
        return "\n\n".join(p for p in parts if p)

    async def one_pass(self, n: int, before: TestEvidence | None) -> TestEvidence | None:
        o = self.o
        from .agent.tools import READ_TOOLS, WRITE_TOOLS, ToolContext
        started, spent0 = time.monotonic(), o.budget.spent
        path, branch = await asyncio.to_thread(o.ws.create_worktree, f"pass{n}")
        key = f"pass{n}"
        o.store.upsert_task(o.run_id, key, title=f"Ralph pass {n}", kind="implement", role=o.single_role,
                            state="RUNNING", branch=branch, worktree=str(path), started_at=time.time(), cycle=n)
        try:
            self._into(path)
            await o._ensure_deps(path, key)
            ctx = ToolContext(workspace=path, sandbox=o.sandbox, installer=o._tool_installer(),
                              command_timeout_s=o.cfg.sandbox.command_timeout_s,
                              artifacts_dir=o.artifacts / key, test_command=o.cfg.project.test_command,
                              base_sha=o.ws.base_sha)
            res = await o._agent(o.single_role, key, RALPH_SYSTEM, self._prompt(n),
                                 READ_TOOLS + WRITE_TOOLS + o._exec_tools(), o._finish_terminal(), ctx)
            summary = (res.value or {}).get("summary", "") if res.ok else ""
            if not res.ok:
                self._from(path, keep_plan=False)
                self.thrown += 1
                why = f"the agent did not finish: {res.error}"
                self.append_log(f"## Pass {n}: thrown away\n{why}")
                o.store.upsert_task(o.run_id, key, state="FAILED", error=why[:500], ended_at=time.time())
                o.log("ralph", f"pass {n} thrown away: {why}", task_key=key, level="warn")
                return None
            sha = await asyncio.to_thread(o.ws.commit_all, path, f"Ralph pass {n}\n\n{summary}"[:4000])
            if sha is None:
                self._from(path, keep_plan=True)
                self.append_log(f"## Pass {n}: no code changes\n{summary}")
                o.store.upsert_task(o.run_id, key, state="SUCCEEDED", summary=summary[:2000], ended_at=time.time())
                o.log("ralph", f"pass {n} changed no code" + (" (plan updated)" if self.plan().strip() else ""), task_key=key)
                return None
            await o._ensure_deps(path, key)
            ev = await run_tests(o.sandbox, path, o.cfg.project.test_command, o.artifacts, key,
                                 o.cfg.sandbox.command_timeout_s)
            o._record_tests(ev, n)
            ok, why = acceptable(ev, before)
            if ok:
                mr = await asyncio.to_thread(o.ws.merge_into_integration, branch, f"Ralph pass {n}: keep")
                ok = mr.ok
                why = why if mr.ok else f"could not merge the pass ({', '.join(mr.conflicts or [])})"
            if ok:
                self._from(path, keep_plan=True)
                self.kept += 1
                self.append_log(f"## Pass {n}: kept\n{summary}\nTests: {why}")
                o.store.upsert_task(o.run_id, key, state="INTEGRATED", summary=summary[:2000], ended_at=time.time())
                o.log("ralph", f"pass {n} kept: {why}", task_key=key, data={"summary": summary})
                return ev
            self._from(path, keep_plan=False)
            self.thrown += 1
            details = "\n".join(f"- `{f['id']}`: {(f.get('message') or f.get('text') or '')[:300]}" for f in ev.failures[:8])
            self.append_log(f"## Pass {n}: thrown away\nThe agent said: {summary}\nWhy it was thrown away: {why}\n{details}")
            o.store.upsert_task(o.run_id, key, state="FAILED", summary=summary[:2000], error=why[:500], ended_at=time.time())
            o.log("ralph", f"pass {n} thrown away: {why}", task_key=key, level="warn")
            return None
        finally:
            self.pass_times.append(time.monotonic() - started)
            self.pass_costs.append(max(0.0, o.budget.spent - spent0))
            self.save()
            await asyncio.to_thread(o.ws.remove_worktree, path)

    async def run(self, repo_summary: str) -> None:
        o = self.o
        self.repo_summary = repo_summary
        self._exclude()
        self.restore()
        o.plan_summary = "Ralph mode: one developer, fresh passes, one plan item per pass"
        o.store.update_run(o.run_id, plan_json=json.dumps({"summary": o.plan_summary, "tasks": [], "acceptance_criteria": [],
                                                           "mode": "ralph"}))
        o.sm.transition(RunState.PLANNING)
        o.sm.transition(RunState.TASK_VALIDATION, "Ralph mode: the agent keeps its own plan")
        o.log("ralph", f"Ralph mode: up to {MAX_PASSES} fresh passes by {o.cfg.roles[o.single_role].model}; "
                       "each pass is kept only if the tests do not get worse")
        before: TestEvidence | None = getattr(o, "baseline", None)
        idle = 0
        n = 0
        o.sm.transition(RunState.TASK_EXECUTION)
        while True:
            o._check_cancel()
            if n >= MAX_PASSES:
                self.stopped = f"it reached the limit of {MAX_PASSES} passes"
                break
            if self.plan().strip() and open_items(self.plan()) == 0:
                self.stopped = "every item in the plan is done"
                break
            room = self._room()
            if room:
                self.stopped = room
                break
            n += 1
            o.store.update_run(o.run_id, repair_cycle=n)
            ev = await self.one_pass(n, before)
            if ev is not None:
                before, idle = ev, 0
            else:
                idle += 1
                if idle >= STUCK_AFTER:
                    self.stopped = f"{idle} passes in a row made no progress"
                    break
        summary = {"passes": n, "kept": self.kept, "thrown_away": self.thrown, "stopped": self.stopped,
                   "open_items": open_items(self.plan()) if self.plan().strip() else None}
        o.store.kv_set("ralph_summary:" + o.run_id, json.dumps(summary))
        (o.artifacts).mkdir(parents=True, exist_ok=True)
        for name in FILES:
            src = self.state_dir / name
            if src.exists():
                (o.artifacts / f"ralph-{name}").write_text(_read(src), encoding="utf-8")
        o.log("ralph", f"loop ended after {n} pass(es): {self.kept} kept, {self.thrown} thrown away; stopped because {self.stopped}",
              data=summary)
        o.sm.transition(RunState.INTEGRATION)
        changed = await asyncio.to_thread(o.ws.changed_files)
        if not changed:
            raise RunAborted(RunState.FAILED, f"Ralph mode kept no changes in {n} pass(es); stopped because {self.stopped}")
        o.sm.transition(RunState.TESTING)
        await o._ensure_deps(o.ws.repo, "repo")
        final = await run_tests(o.sandbox, o.ws.repo, o.cfg.project.test_command, o.artifacts, "final",
                                o.cfg.sandbox.command_timeout_s)
        o._record_tests(final, n + 1)
        o.log("test", f"final test run of the kept passes: {final.short()}", level="info" if final.ok else "warn")
        o.sm.transition(RunState.CODE_REVIEW, "Ralph mode has no separate reviewer; the tests are the gate")
        baseline = getattr(o, "baseline", None)
        ok, why = acceptable(final, baseline)
        if not ok:
            raise RunAborted(RunState.FAILED, f"the kept passes together fail the tests: {why}")
        left = open_items(self.plan()) if self.plan().strip() else 0
        o.sm.transition(RunState.APPROVED, f"{self.kept} pass(es) kept; {final.short()}"
                        + (f"; {left} plan item(s) still open ({self.stopped})" if left else ""))
