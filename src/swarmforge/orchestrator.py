"""The orchestrator: owns the workflow state machine, safety gates and agent coordination.

LLM agents propose plans, code, tests and reviews. Deterministic code here decides every state
transition, runs the tests itself, applies the approval gate and controls GitHub publishing.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import sys
import os
import secrets
import time
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml

from . import prompts
from .agent.runtime import AgentResult, AgentSpec, run_agent
from .agent.tools import DEV_TOOLS, EXEC_TOOLS, READ_TOOLS, WRITE_TOOLS, TerminalTool, ToolContext
from .config import Config
from .db import Store
from .github import GitHubClient, GitHubError, Issue, get_token, load_issue_file, parse_issue_url, push_branch
from .deps import ensure_deps, plan_install
from .preflight import environment_problems
from .repos import RepoError, _redact, auth_args, normalize_repo, preflight
from .knowledge import NOTES_JSON_SCHEMA, Vault, render_for_prompt, validate_notes, write_distilled
from .llm import Budget, BudgetExceeded, LLMClient
from .plan import (PLAN_JSON_SCHEMA, REVIEW_JSON_SCHEMA, TEST_REPORT_JSON_SCHEMA, Criterion, Plan,
                   PlanValidationError, Task, validate_plan, validate_review, validate_test_report)
from .report import build_pr_body, build_summary, write_report, write_summary
from .sandbox import Sandbox, make_sandbox
from .scheduler import Scheduler
from .state import RunAborted, RunState, StateMachine, TaskState
from .testing import TestEvidence, evaluate_criteria, run_tests
from .repolock import blocking_runs, repo_key as lock_key
from .workspace import EmptyRepository, GitError, GitWorkspace, StaleSeed, git

TEST_PATH_HINTS = ("tests/", "test/", "testing/")


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def is_test_path(p: str) -> bool:
    p = p.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    return (p.startswith(TEST_PATH_HINTS) or "/tests/" in p or name.startswith("test_")
            or name.endswith("_test.py") or name == "conftest.py")


class Orchestrator:
    def __init__(self, config: Config, store: Store, *, mode: str = "swarm",
                 single_role: str | None = None,
                 approve: Callable[[str], bool] | None = None,
                 echo: Callable[[str], None] | None = print,
                 sandbox: Sandbox | None = None,
                 llm_transport=None,
                 test_command: str | None = None,
                 sandbox_image: str | None = None,
                 seed_run_id: str | None = None,
                 heal_of: str | None = None, heal_attempt: int | None = None,
                 heal_context: str | None = None):
        assert mode in ("swarm", "single")
        self.cfg = copy.deepcopy(config)  # per-run copy: project profiles may adjust it
        self.store = store
        self._variant_note = None
        if self.cfg.active_variant and not self.cfg.variant_applied:
            from .variants import apply_overlay
            v = store.variant(self.cfg.active_variant)
            if v:
                self.cfg = apply_overlay(self.cfg, v["overlay"], v["id"])
            else:
                self._variant_note = f"active_variant {self.cfg.active_variant!r} not found; using the base config"
                self.cfg.active_variant = None
        self._override_test_command = test_command
        self._override_image = sandbox_image
        self._repo_poll_s = 15.0   # how often a waiting run re-checks whether its repository is free
        self.seed_run_id = seed_run_id
        self.heal_of, self.heal_attempt, self.heal_context = heal_of, heal_attempt, heal_context
        self.mode = mode
        self.single_role = single_role or config.developer_roles[0]
        self.approve = approve
        self.echo = echo
        self._sandbox_override = sandbox
        self._llm_transport = llm_transport
        self._integration_lock = asyncio.Lock()
        self._conflicts_used = 0
        self.run_id: str | None = None

    # ================================================================ logging
    def log(self, kind: str, msg: str, *, level: str = "info", task_key: str | None = None,
            role: str | None = None, data=None) -> None:
        self.store.event(self.run_id, kind, msg, level=level, task_key=task_key, role=role, data=data)
        if self.echo:
            who = f"[{task_key}/{role}] " if task_key and role else (f"[{task_key}] " if task_key else "")
            self.echo(f"{datetime.now():%H:%M:%S} {level.upper():5} {kind:8} {who}{msg}")

    def _should_cancel(self) -> bool:
        return self.store.cancel_requested(self.run_id)

    # ================================================================== entry
    async def execute(self, *, issue_url: str | None = None, issue_file: str | None = None,
                      repo: str | None = None, base_branch: str | None = None,
                      run_id: str | None = None) -> str:
        self.run_id = run_id or new_run_id()
        self.run_dir = self.cfg.workdir / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "transcripts").mkdir(exist_ok=True)
        self.artifacts = self.run_dir / "artifacts"
        self.store.create_run(
            self.run_id, mode=self.mode, issue_url=issue_url or issue_file, run_dir=str(self.run_dir),
            seed_run_id=self.seed_run_id, heal_of=self.heal_of, heal_attempt=self.heal_attempt,
            budget_usd=self.cfg.limits.budget_usd, pid=os.getpid(), variant_id=self.cfg.active_variant,
            max_runtime_s=int(self.cfg.limits.max_runtime_s),
            max_repair_cycles=int(self.cfg.limits.max_repair_cycles),
            config_json=json.dumps(_config_snapshot(self.cfg)))
        self.sm = StateMachine(self.store, self.run_id)
        self.budget = Budget(self.cfg.limits.budget_usd)
        self.llm = LLMClient(self.cfg, self.store, self.run_id, self.budget, transport=self._llm_transport)
        self.sandbox = self._sandbox_override or make_sandbox(self.cfg.sandbox, run_label=self.run_id)
        self.ws: GitWorkspace | None = None
        self.issue: Issue | None = None
        self.repo_key: str | None = None
        self.knowledge_block = self.knowledge_dev_block = self.knowledge_review_block = ""
        self.knowledge_used: list[str] = []
        if self._variant_note:
            self.log("variant", self._variant_note, level="warn")
        elif self.cfg.active_variant:
            self.log("variant", f"using agent variant {self.cfg.active_variant}")
        self.log("run", f"run {self.run_id} started (mode={self.mode}, budget ${self.cfg.limits.budget_usd:.2f}, "
                        f"sandbox: {self.sandbox.describe()})")
        self._started = time.monotonic()
        self._timed_out = False
        self._issue_file_hint = issue_file
        try:
            await self._wait_for_repo(repo, issue_url)
            self._started = time.monotonic()
            pipeline = asyncio.ensure_future(self._pipeline(issue_url, issue_file, repo, base_branch))
            watchdog = asyncio.ensure_future(self._watchdog(pipeline))
            try:
                await pipeline
            except asyncio.CancelledError:
                if not self._timed_out:
                    raise
                raise asyncio.TimeoutError from None
            finally:
                watchdog.cancel()
        except asyncio.TimeoutError:
            self._abort(RunState.FAILED, f"maximum runtime of {int(self.cfg.limits.max_runtime_s)}s exceeded")
        except RunAborted as e:
            self._abort(e.state, e.reason)
        except BudgetExceeded as e:
            self._abort(RunState.BUDGET_EXCEEDED, str(e))
        except (asyncio.CancelledError, KeyboardInterrupt):
            self.store.record_intervention(self.run_id, "cancel", "run interrupted by user (Ctrl+C)")
            self._abort(RunState.CANCELLED, "interrupted by user")
        except Exception as e:  # unexpected: never report success
            self.log("error", f"unexpected error: {type(e).__name__}: {e}", level="error",
                     data={"traceback": traceback.format_exc()})
            self._abort(RunState.FAILED, f"unexpected error: {type(e).__name__}: {e}")
        finally:
            try:
                await self.sandbox.kill_all()
            except Exception:
                pass
            if getattr(self, "_relay", None) is not None:
                await self._relay.stop()
                self._relay = None
            if self.cfg.knowledge.enabled and self.issue is not None:
                try:
                    await asyncio.wait_for(self._capture_knowledge(), timeout=900)
                except Exception as e:  # knowledge capture must never change the run outcome
                    self.log("knowledge", f"knowledge capture failed: {type(e).__name__}: {e}", level="warn")
            try:
                path = write_report(self.store, self.run_id, self.run_dir)
                self.log("report", f"execution report written to {path}")
            except Exception as e:
                self.log("error", f"failed to write report: {e}", level="error")
            if self.store.get_run(self.run_id)["state"] not in ("COMPLETED", "CANCELLED"):
                try:
                    from .postmortem import diagnose
                    await asyncio.wait_for(diagnose(self.cfg, self.store, self.run_id,
                                                    transport=self._llm_transport), timeout=600)
                except Exception as e:  # the post-mortem must never change the run outcome
                    self.log("postmortem", f"post-mortem failed: {type(e).__name__}: {e}", level="warn")
            try:
                path = write_summary(self.store, self.run_id, self.run_dir)
                self.log("report", f"summary written to {path}")
            except Exception as e:
                self.log("error", f"failed to write summary: {e}", level="warn")
        return self.run_id

    def _apply_live_settings(self) -> None:
        """Pick up limits changed from the dashboard while the run is going (budget, time, repair cycles)."""
        try:
            row = self.store.get_run(self.run_id) or {}
        except Exception:
            return
        lim = self.cfg.limits
        b = row.get("budget_usd")
        if b is not None and abs(float(b) - self.budget.limit) > 1e-9:
            self.log("settings", f"budget changed to ${float(b):.2f}")
            self.budget.limit = float(b)
            lim.budget_usd = float(b)
        t = row.get("max_runtime_s")
        if t and int(t) != int(lim.max_runtime_s):
            self.log("settings", f"time limit changed to {int(t) / 3600:.2f} h")
            lim.max_runtime_s = int(t)
        c = row.get("max_repair_cycles")
        if c is not None and int(c) != int(lim.max_repair_cycles):
            self.log("settings", f"max repair cycles changed to {int(c)}")
            lim.max_repair_cycles = int(c)

    async def _watchdog(self, pipeline: asyncio.Future, every_s: float = 5.0) -> None:
        """Enforces the (live-adjustable) time limit; replaces a fixed asyncio.wait_for."""
        while not pipeline.done():
            self._apply_live_settings()
            if time.monotonic() - self._started > self.cfg.limits.max_runtime_s:
                self._timed_out = True
                pipeline.cancel()
                return
            await asyncio.sleep(min(every_s, max(0.05, self.cfg.limits.max_runtime_s)))

    def _abort(self, state: RunState, reason: str) -> None:
        if self.sm.terminal:
            return
        if state == RunState.CANCELLED and self._should_cancel():
            reason = reason or "cancelled"
        self.log("run", f"stopping: {state.value} - {reason}", level="warn")
        self.sm.transition(state, reason)

    # =============================================================== pipeline
    async def _pipeline(self, issue_url, issue_file, repo, base_branch) -> None:
        issue, source = await asyncio.to_thread(self._load_issue, issue_url, issue_file, repo)
        self.issue = issue
        self.repo_key = repo_key(issue, str(source))
        # the request exactly as the user wrote it (before continue/self-healing context is appended)
        self.store.update_run(self.run_id, issue_title=issue.title, issue_body=(issue.body or "")[:20000])
        if self.seed_run_id:
            ctx = continue_context(self.store, self.seed_run_id)
            if ctx is None:
                raise RunAborted(RunState.BLOCKED, f"cannot continue: run {self.seed_run_id} not found")
            issue.body = issue.body.rstrip() + "\n\n" + ctx
            self.log("issue", f"continuing from run {self.seed_run_id}: its work is the starting point and its "
                              "unresolved problems were added to the task")
        if self.heal_context:
            issue.body = issue.body.rstrip() + "\n\n" + self.heal_context
            self.log("selfheal", f"self-healing attempt {self.heal_attempt} after run {self.heal_of}: the "
                                 "diagnosis of why it failed was added to the task")
        self.store.update_run(self.run_id, issue_title=issue.title, issue_number=issue.number,
                              repo=issue.full_repo or str(source))
        self.log("issue", f"issue: {issue.title}")

        # -------------------------------------------------- inspection
        self.sm.transition(RunState.REPOSITORY_INSPECTION)
        # Accept anything reasonable ("owner/repo", GitHub web URLs like .../tree/main, local paths)
        # and verify it is reachable and non-empty before doing anything else.
        token = get_token(self.cfg.github)
        try:
            ref = normalize_repo(str(source))
            if base_branch:
                ref.branch = base_branch
            ref = await asyncio.to_thread(preflight, ref, token)
        except RepoError as e:
            raise RunAborted(RunState.BLOCKED, str(e))
        self.ref = ref
        if ref.source != str(source):
            self.log("repo", f"repository '{source}' resolved to {ref.source}"
                             + (f" (branch {ref.branch})" if ref.branch else ""))
        source = ref.source
        base_branch = ref.branch
        if ref.full_name and not issue.full_repo:
            self.repo_key = ref.full_name
            self.store.update_run(self.run_id, repo=ref.full_name)
        if self._sandbox_override is None:
            problems = await asyncio.to_thread(environment_problems, self.cfg, self._override_image)
            if problems:
                raise RunAborted(RunState.BLOCKED, " ".join(problems))
        self.ws = GitWorkspace(self.run_dir, self.run_id, self.cfg.github.branch_prefix)
        self.source = str(source)
        try:
            await asyncio.to_thread(self.ws.prepare, str(source), issue.number, base_branch,
                                    ["-c", "credential.helper=",
                                     *(auth_args(str(source), token) if ref.use_auth else [])])
        except EmptyRepository:
            raise RunAborted(RunState.BLOCKED, (
                f"the repository {source} has no commits yet. SwarmForge needs a base commit to branch "
                "from and to open a pull request against. On GitHub, open the repository page and click "
                "'Add a README' (or push any initial commit), then start the run again."))
        except GitError as e:
            raise RunAborted(RunState.BLOCKED, f"could not clone {source}: {_redact(str(e), token)}")
        if self.seed_run_id:
            seed = self.store.get_run(self.seed_run_id)
            seed_repo = Path(seed["run_dir"] or "") / "repo"
            if not seed["integration_branch"] or not seed_repo.is_dir():
                raise RunAborted(RunState.BLOCKED, f"cannot continue from {self.seed_run_id}: its workspace "
                                                   "(clone) no longer exists; use Start again instead")
            try:
                sha = await asyncio.to_thread(self.ws.seed_from, seed_repo, seed["integration_branch"])
            except StaleSeed as e:
                # the base branch moved and the old work conflicts with it: start fresh from the
                # current base (the previous attempt's diagnosis is still part of the task)
                sha = None
                self.log("repo", f"cannot reuse the previous attempt's code: {e}. Starting fresh from "
                                 f"{self.ws.base_branch} @ {self.ws.base_sha[:8]}", level="warn")
            except GitError as e:
                raise RunAborted(RunState.BLOCKED, f"cannot continue from {self.seed_run_id}: {e}")
            if sha and self.ws.seed_updated:
                self.log("repo", f"{self.ws.base_branch} moved on since the previous attempt; merged it "
                                 f"into {seed['integration_branch']} cleanly ({sha[:8]})", level="warn")
            if sha:
                self.log("repo", f"starting from the previous attempt's result {seed['integration_branch']} "
                                 f"({sha[:8]}); reviews and PRs still compare against {self.ws.base_branch}")
        self._apply_project_profile()
        if getattr(self.ws, "source_dirty", False):
            self.log("repo", "source checkout has uncommitted changes; they are NOT included (only "
                             "committed history is cloned)", level="warn")
        self.store.update_run(self.run_id, base_branch=self.ws.base_branch,
                              integration_branch=self.ws.integration_branch)
        self.log("repo", f"cloned into {self.ws.repo} (base {self.ws.base_branch} @ {self.ws.base_sha[:8]}, "
                         f"integration branch {self.ws.integration_branch})")
        repo_summary = await self._inspect_repo()
        self._retrieve_knowledge()

        # -------------------------------------------------- planning
        self.sm.transition(RunState.PLANNING)
        plan = await self._make_plan(repo_summary)
        self.plan = plan
        self.store.update_run(self.run_id, plan_json=json.dumps(plan.to_dict()))
        self.sm.transition(RunState.TASK_VALIDATION)
        self._register_tasks(plan.tasks, cycle=0)
        self.log("plan", f"plan accepted: {len(plan.tasks)} task(s), waves={plan.waves()}",
                 data=plan.to_dict())

        cycle = 0
        tasks = plan.tasks
        done_ids: set[str] = set()
        while True:
            self._check_cancel()
            # -------------------------------------------- execution
            self.sm.transition(RunState.TASK_EXECUTION)
            sched = await self._run_tasks(tasks, cycle, done_ids)
            done_ids.update(sched.succeeded)
            failed_tasks = sched.failed + sched.blocked
            self._check_cancel()

            # -------------------------------------------- integration
            self.sm.transition(RunState.INTEGRATION)
            changed = await asyncio.to_thread(self.ws.changed_files)
            stat = await asyncio.to_thread(self.ws.diff_stat)
            self.log("integrate", f"integration branch now changes {len(changed)} file(s) vs base",
                     data={"diff_stat": stat})
            if not changed:
                raise RunAborted(RunState.FAILED, "no code changes were produced by any task")

            # -------------------------------------------- testing
            self.sm.transition(RunState.TESTING)
            evidence, tester_report = await self._testing(cycle)

            # -------------------------------------------- review
            self.sm.transition(RunState.CODE_REVIEW)
            review = await self._review(cycle, evidence)

            problems = self._gate(cycle, evidence, tester_report, review, failed_tasks)
            if not problems:
                self.sm.transition(RunState.APPROVED,
                                   f"tests passed ({evidence.short()}); no blocking findings")
                break
            self.sm.transition(RunState.FIX_REQUIRED, f"{len(problems)} blocking problem(s)")
            for p in problems:
                self.log("gate", p["summary"], level="warn")
            max_cycles = self.cfg.limits.max_repair_cycles if self.mode == "swarm" else 0
            if cycle >= max_cycles:
                raise RunAborted(RunState.FAILED,
                                 f"repair limit reached ({max_cycles} cycle(s)); "
                                 f"{len(problems)} unresolved problem(s): "
                                 + "; ".join(p["summary"] for p in problems)[:800])
            cycle += 1
            self.store.update_run(self.run_id, repair_cycle=cycle)
            fix_plan = await self._make_fix_plan(cycle, problems, done_ids)
            self.sm.transition(RunState.TASK_VALIDATION)
            self._register_tasks(fix_plan.tasks, cycle=cycle, kind="fix")
            self.log("plan", f"repair cycle {cycle}: {len(fix_plan.tasks)} corrective task(s)",
                     data=fix_plan.to_dict())
            tasks = fix_plan.tasks

        await self._deliver()

    # ============================================================ issue/repo
    def _load_issue(self, issue_url, issue_file, repo) -> tuple[Issue, str]:
        if issue_url:
            gh = GitHubClient(self.cfg.github)
            issue = gh.fetch_issue(issue_url)
            source = repo or issue.clone_url
            self.store.update_run(self.run_id, issue_url=issue.url)
            return issue, source
        if issue_file:
            if not repo:
                raise RunAborted(RunState.BLOCKED, "--repo is required with --issue-file")
            return load_issue_file(Path(issue_file)), repo
        raise RunAborted(RunState.BLOCKED, "no issue given")

    async def _inspect_repo(self) -> str:
        files = await asyncio.to_thread(self.ws.file_tree)
        readme = ""
        for name in ("README.md", "README.rst", "README.txt", "README"):
            p = self.ws.repo / name
            if p.is_file():
                readme = p.read_text(encoding="utf-8", errors="replace")[:3000]
                break
        await self._ensure_deps(self.ws.repo, "repo")
        ev = await run_tests(self.sandbox, self.ws.repo, self.cfg.project.test_command, self.artifacts,
                             "baseline", self.cfg.sandbox.command_timeout_s)
        self._record_tests(ev, cycle=-1)
        if ev.junit_found and ev.total == 0:
            self.log("test", "baseline: the repository has no tests yet (new project); the team must add them")
        else:
            self.log("test", f"baseline tests before any change: {ev.short()}")
        if ev.exit_code != 0 and not ev.junit_found and ev.total == 0:
            self.log("test", "baseline test command did not produce a JUnit report; check "
                             "project.test_command and the sandbox image", level="warn")
        self.baseline = ev
        return "\n".join([
            f"Files ({len(files)} tracked):", *files[:300],
            "", f"Test command: {self.cfg.project.test_command}",
            f"Baseline test result (before changes): {ev.short()}",
            "", "README excerpt:", readme or "(no README)",
        ])

    # ================================================================ planning
    def _plan_terminal(self, existing: set[str], require_criteria: bool) -> TerminalTool:
        def v(args):
            data = args.get("plan", args)
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError as e:
                    return None, f"plan is not valid JSON: {e}"
            try:
                return validate_plan(data, self.cfg.developer_roles, self.cfg.limits.max_tasks,
                                     existing_ids=existing, require_criteria=require_criteria), None
            except PlanValidationError as e:
                return None, "plan validation failed:\n- " + "\n- ".join(e.errors)
        return TerminalTool("submit_plan", "Submit the implementation plan (validated by the orchestrator).",
                            PLAN_JSON_SCHEMA, v)

    async def _make_plan(self, repo_summary: str) -> Plan:
        if self.mode == "single":
            role = self.single_role
            self.log("plan", f"single-agent mode: one task for role {role}, no planner")
            return Plan(summary="Single-agent baseline: one developer implements the whole issue.",
                        tasks=[Task(id="T1", title=self.issue.title, role=role,
                                    description="Implement the entire issue end to end, including tests "
                                                "and documentation.",
                                    acceptance_criteria=["Everything requested in the issue"])],
                        acceptance_criteria=[])
        user = "\n\n".join([
            prompts.render_issue(self.issue.title, self.issue.body),
            prompts.render_repo_summary(repo_summary),
            self.knowledge_block,
            f"Available developer roles: {self.cfg.developer_roles} "
            f"(models: {', '.join(f'{r}={self.cfg.roles[r].model}' for r in self.cfg.developer_roles)}).",
            f"Maximum number of tasks: {self.cfg.limits.max_tasks}. "
            f"Up to {self.cfg.limits.max_concurrent_agents} tasks can run concurrently.",
            "Explore the repository, then call submit_plan.",
        ])
        last_err = None
        for attempt in range(1, self.cfg.limits.max_planning_attempts + 1):
            res = await self._agent("planner", f"plan-{attempt}", prompts.PLANNER_SYSTEM, user,
                                    READ_TOOLS, self._plan_terminal(set(), True),
                                    ToolContext(workspace=self.ws.repo))
            if res.ok:
                return res.value
            last_err = res.error
            self.log("plan", f"planning attempt {attempt} failed: {res.error}", level="warn")
        raise RunAborted(RunState.FAILED, f"planner could not produce a valid plan: {last_err}")

    async def _make_fix_plan(self, cycle: int, problems: list[dict], done_ids: set[str]) -> Plan:
        all_ids = {t["task_key"] for t in self.store.tasks(self.run_id)}
        prob_text = "\n\n".join(f"### Problem {i + 1} ({p['kind']})\n{p['detail']}" for i, p in enumerate(problems))
        user = "\n\n".join([
            prompts.render_issue(self.issue.title, self.issue.body),
            f"Original plan summary: {self.plan.summary}",
            "Acceptance criteria:\n" + "\n".join(f"- {c.id}: {c.text}" for c in self.plan.acceptance_criteria),
            f"Task ids already used (do not reuse): {sorted(all_ids)}. Completed tasks: {sorted(done_ids)}.",
            f"Available developer roles: {self.cfg.developer_roles}.",
            "<problems>\n" + prob_text + "\n</problems>",
            f"This is repair cycle {cycle} of at most {self.cfg.limits.max_repair_cycles}. "
            "Inspect the code, then call submit_plan with corrective tasks.",
        ])
        last_err = None
        for attempt in range(1, self.cfg.limits.max_planning_attempts + 1):
            res = await self._agent("planner", f"fixplan-c{cycle}-{attempt}", prompts.FIX_PLANNER_SYSTEM,
                                    user, READ_TOOLS, self._plan_terminal(all_ids, False),
                                    ToolContext(workspace=self.ws.repo))
            if res.ok:
                return res.value
            last_err = res.error
        raise RunAborted(RunState.FAILED, f"planner could not produce a corrective plan: {last_err}")

    def _register_tasks(self, tasks: list[Task], cycle: int, kind: str = "implement") -> None:
        for t in tasks:
            self.store.upsert_task(
                self.run_id, t.id, cycle=cycle, kind=kind, title=t.title, description=t.description,
                role=t.role, model=self.cfg.roles[t.role].model, depends_on=t.depends_on,
                acceptance_criteria=t.acceptance_criteria, expected_files=t.expected_files,
                state=TaskState.PENDING.value)

    # =============================================================== execution
    async def _run_tasks(self, tasks: list[Task], cycle: int, done_ids: set[str]):
        def on_state(tid, st, why):
            fields = {"state": st.value}
            if st == TaskState.RUNNING:
                fields["started_at"] = time.time()
            elif st in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.BLOCKED, TaskState.CANCELLED):
                fields["ended_at"] = time.time()
                if st == TaskState.SUCCEEDED:
                    fields["state"] = TaskState.INTEGRATED.value
                if why:
                    fields["error"] = why
            self.store.upsert_task(self.run_id, tid, **fields)
            self.log("task", f"{st.value}" + (f": {why}" if why else ""), task_key=tid,
                     level="warn" if st in (TaskState.FAILED, TaskState.BLOCKED) else "info")

        self._current_tasks = tasks
        sched = Scheduler(tasks, lambda t: self._execute_task(t, cycle), on_state=on_state,
                          max_concurrency=self.cfg.limits.max_concurrent_agents,
                          satisfied=done_ids, should_cancel=self._should_cancel)
        res = await sched.run()
        self.log("schedule", f"cycle {cycle}: {len(res.succeeded)} integrated, {len(res.failed)} failed, "
                             f"{len(res.blocked)} blocked; max parallel agents = {res.max_parallel}",
                 data={"order": res.order_started, "max_parallel": res.max_parallel})
        self._check_cancel()
        return res

    def _finish_terminal(self) -> TerminalTool:
        def v(args):
            s = args.get("summary")
            if not isinstance(s, str) or not s.strip():
                return None, "summary must be a non-empty string"
            return {"summary": s.strip(), "tests": str(args.get("tests_run", ""))}, None
        return TerminalTool("finish", "Call when the task is fully implemented and tested.", {
            "type": "object", "required": ["summary"],
            "properties": {"summary": {"type": "string", "description": "What you changed and why."},
                           "tests_run": {"type": "string", "description": "Test commands you ran and results."}}},
            v)

    def _dev_prompt(self, task: Task, attempt: int, prev_error: str | None) -> str:
        others = [t for t in self._current_tasks if t.id != task.id]
        parts = [
            prompts.render_issue(self.issue.title, self.issue.body),
            f"Overall plan: {self.plan.summary}" if hasattr(self, "plan") else "",
            f"## Your task {task.id}: {task.title}\n{task.description}",
            "Acceptance criteria for your task:\n" + "\n".join(f"- {c}" for c in task.acceptance_criteria),
            "Expected files: " + (", ".join(task.expected_files) or "(not specified)"),
        ]
        if others:
            parts.append("Other tasks handled by teammates (do NOT implement these, avoid their files):\n" +
                         "\n".join(f"- {t.id} {t.title} (files: {', '.join(t.expected_files) or '?'})"
                                   for t in others))
        if task.depends_on:
            parts.append(f"Your dependencies ({', '.join(task.depends_on)}) are already merged into your worktree.")
        if self.knowledge_dev_block:
            parts.append(self.knowledge_dev_block)
        parts.append(self._test_hint())
        if attempt > 1:
            parts.append(f"NOTE: a previous attempt at this task ended with: {prev_error}. Your worktree "
                         "contains whatever that attempt changed. Review it and complete the task.")
        return "\n\n".join(p for p in parts if p)

    async def _execute_task(self, task: Task, cycle: int) -> bool:
        path, branch = await asyncio.to_thread(self.ws.create_worktree, task.id)
        await self._ensure_deps(path, task.id)
        self.store.upsert_task(self.run_id, task.id, branch=branch, worktree=str(path))
        self.log("task", f"worktree {path.name} on branch {branch} ({self.cfg.roles[task.role].model})",
                 task_key=task.id, role=task.role)
        res: AgentResult | None = None
        prev_error = None
        attempts = 1 + max(0, self.cfg.limits.task_retries)
        for attempt in range(1, attempts + 1):
            self.store.upsert_task(self.run_id, task.id, attempts=attempt)
            ctx = ToolContext(workspace=path, sandbox=self.sandbox, installer=self._tool_installer(),
                              command_timeout_s=self.cfg.sandbox.command_timeout_s,
                              artifacts_dir=self.artifacts / f"task-{task.id}")
            res = await self._agent(task.role, task.id, prompts.DEVELOPER_SYSTEM,
                                    self._dev_prompt(task, attempt, prev_error),
                                    READ_TOOLS + WRITE_TOOLS + self._exec_tools(), self._finish_terminal(), ctx,
                                    transcript_suffix=f"a{attempt}")
            if res.ok:
                break
            prev_error = res.error
            self.log("task", f"attempt {attempt} failed: {res.error}", task_key=task.id, role=task.role,
                     level="warn")
        if not res or not res.ok:
            self.store.upsert_task(self.run_id, task.id, error=res.error if res else "no result")
            return False
        self.store.upsert_task(self.run_id, task.id, summary=res.value["summary"])
        sha = await asyncio.to_thread(self.ws.commit_all, path, f"{task.id}: {task.title}\n\n{res.value['summary']}")
        if sha is None:
            self.log("task", "agent finished without changing any files", task_key=task.id, level="warn")
            await asyncio.to_thread(self.ws.remove_worktree, path)
            return True
        self.log("task", f"committed {sha[:8]}; files: {', '.join(res.files_written) or '?'}",
                 task_key=task.id, role=task.role)
        ok = await self._integrate(task, branch)
        if ok:
            await asyncio.to_thread(self.ws.remove_worktree, path)
        return ok

    async def _integrate(self, task: Task, branch: str) -> bool:
        async with self._integration_lock:
            mr = await asyncio.to_thread(self.ws.merge_into_integration, branch,
                                         f"Integrate {task.id}: {task.title}")
            if mr.ok:
                self.log("integrate", f"merged {branch} -> {self.ws.integration_branch} ({mr.sha[:8]})",
                         task_key=task.id)
                return True
            self.log("integrate", f"merge conflict in {mr.conflicts}", task_key=task.id, level="warn")
            if self._conflicts_used >= self.cfg.limits.max_conflict_resolutions:
                self.log("integrate", "conflict-resolution limit reached", task_key=task.id, level="error")
                return False
            self._conflicts_used += 1
            key = f"{task.id}-merge"
            rpath, rbranch, conflicts = await asyncio.to_thread(
                self.ws.start_conflict_resolution, key, branch)
            await self._ensure_deps(rpath, key)
            user = "\n\n".join([
                prompts.render_issue(self.issue.title, self.issue.body),
                f"Task being integrated: {task.id} {task.title}\n{task.description}",
                "Conflicted files:\n" + "\n".join(f"- {c}" for c in conflicts),
                self._test_hint(),
            ])
            ctx = ToolContext(workspace=rpath, sandbox=self.sandbox, installer=self._tool_installer(),
                              command_timeout_s=self.cfg.sandbox.command_timeout_s,
                              artifacts_dir=self.artifacts / f"merge-{task.id}")
            res = await self._agent(task.role, key, prompts.CONFLICT_SYSTEM, user,
                                    READ_TOOLS + WRITE_TOOLS + self._exec_tools(), self._finish_terminal(), ctx)
            if not res.ok:
                self.log("integrate", f"conflict resolution failed: {res.error}", task_key=task.id, level="error")
                return False
            ok, bad = await asyncio.to_thread(self.ws.finish_conflict_resolution, rpath,
                                              f"Resolve conflicts integrating {task.id}")
            if not ok:
                self.log("integrate", f"conflict markers remain in {bad}", task_key=task.id, level="error")
                return False
            mr2 = await asyncio.to_thread(self.ws.merge_into_integration, rbranch,
                                          f"Integrate {task.id} (conflicts resolved)")
            if not mr2.ok:
                self.log("integrate", "resolved branch still conflicts", task_key=task.id, level="error")
                return False
            await asyncio.to_thread(self.ws.remove_worktree, rpath)
            self.log("integrate", f"conflicts resolved and merged ({mr2.sha[:8]})", task_key=task.id)
            return True

    # ================================================================= testing
    def _tester_terminal(self) -> TerminalTool:
        ids = {c.id for c in self.plan.acceptance_criteria}
        return TerminalTool("submit_test_report", "Submit the test report (validated).",
                            TEST_REPORT_JSON_SCHEMA, lambda a: validate_test_report(a.get("report", a), ids))

    async def _testing(self, cycle: int) -> tuple[TestEvidence, dict | None]:
        tester_report = None
        if self.mode == "swarm":
            tester_report = await self._run_tester(cycle)
        await self._ensure_deps(self.ws.repo, "repo")
        ev = await run_tests(self.sandbox, self.ws.repo, self.cfg.project.test_command, self.artifacts,
                             f"cycle{cycle}", self.cfg.sandbox.command_timeout_s)
        self._record_tests(ev, cycle)
        self.log("test", f"deterministic test run (cycle {cycle}): {ev.short()}",
                 level="info" if ev.ok else "warn", data={"log": ev.log_path})
        if self.plan.acceptance_criteria:
            mapping = (tester_report or {}).get("criteria_tests", {})
            results = evaluate_criteria(mapping, [(c.id, c.text) for c in self.plan.acceptance_criteria], ev)
            for r in results:
                self.store.record_acceptance(self.run_id, cycle=cycle, **r)
            summary = {s: sum(1 for r in results if r["status"] == s) for s in ("satisfied", "failed", "unverified")}
            self.log("test", f"acceptance criteria: {summary}", data=results)
            self._criteria_results = results
        else:
            self._criteria_results = []
        if tester_report:
            for d in tester_report["defects"]:
                self.store.record_finding(self.run_id, cycle=cycle, source="tester", severity=d["severity"],
                                          file=d.get("file"), line=None, title=d["title"],
                                          description=d["description"], recommendation=d["reproduction"])
        return ev, tester_report

    async def _run_tester(self, cycle: int) -> dict | None:
        key = f"tester-c{cycle}"
        path, branch = await asyncio.to_thread(self.ws.create_worktree, key)
        await self._ensure_deps(path, key)
        crit = "\n".join(f"- {c.id}: {c.text}" for c in self.plan.acceptance_criteria)
        user = "\n\n".join([
            prompts.render_issue(self.issue.title, self.issue.body),
            "Acceptance criteria:\n" + (crit or "(none listed)"),
            "Files changed by the developers:\n" + "\n".join(await asyncio.to_thread(self.ws.changed_files)),
            self._test_hint(),
            "This is a re-test after corrective work; update existing acceptance tests if needed."
            if cycle else "",
        ])
        ctx = ToolContext(workspace=path, sandbox=self.sandbox, installer=self._tool_installer(), command_timeout_s=self.cfg.sandbox.command_timeout_s,
                          artifacts_dir=self.artifacts / key)
        self.store.upsert_task(self.run_id, key, cycle=cycle, kind="test", title="Acceptance testing",
                               role="tester", model=self.cfg.roles["tester"].model, state="RUNNING", attempts=1,
                               started_at=time.time(), branch=branch, worktree=str(path))
        res = await self._agent("tester", key, prompts.TESTER_SYSTEM, user,
                                READ_TOOLS + WRITE_TOOLS + self._exec_tools(), self._tester_terminal(), ctx)
        # Deterministic guard: the tester may only add/modify test files.
        reverted = await asyncio.to_thread(_revert_non_test_changes, path)
        if reverted:
            self.log("test", f"reverted tester changes to non-test files: {reverted}", task_key=key, level="warn")
        sha = await asyncio.to_thread(self.ws.commit_all, path, f"{key}: acceptance tests")
        merged = True
        if sha:
            async with self._integration_lock:
                mr = await asyncio.to_thread(self.ws.merge_into_integration, branch, f"Integrate {key}")
            merged = mr.ok
            if not mr.ok:
                self.log("test", f"tester tests conflict with integration ({mr.conflicts}); discarded",
                         task_key=key, level="warn")
        await asyncio.to_thread(self.ws.remove_worktree, path)
        state = "INTEGRATED" if res.ok and merged else "FAILED"
        self.store.upsert_task(self.run_id, key, state=state, ended_at=time.time(),
                               summary=(res.value or {}).get("summary") if res.ok else None,
                               error=None if res.ok else res.error)
        if not res.ok:
            self.log("test", f"tester agent did not complete: {res.error}", task_key=key, level="warn")
            return None
        return res.value

    def _record_tests(self, ev: TestEvidence, cycle: int) -> None:
        self.store.record_test_run(self.run_id, cycle=cycle, label=ev.label, command=ev.command,
                                   exit_code=ev.exit_code, passed=ev.passed, failed=ev.failed,
                                   errors=ev.errors, skipped=ev.skipped, duration_s=ev.duration_s,
                                   log_path=ev.log_path, junit_path=ev.junit_path,
                                   timed_out=int(ev.timed_out))

    # ================================================================== review
    async def _review(self, cycle: int, ev: TestEvidence) -> dict | None:
        if self.mode == "single":
            self.log("review", "single-agent baseline: independent review skipped by design")
            return None
        diff = await asyncio.to_thread(self.ws.diff)
        crit = "\n".join(f"- {c.id}: {c.text}" for c in self.plan.acceptance_criteria)
        user = "\n\n".join([
            prompts.render_issue(self.issue.title, self.issue.body),
            f"Plan summary: {self.plan.summary}",
            "Acceptance criteria:\n" + crit,
            f"Deterministic test result (executed by the orchestrator): {ev.short()}",
            f"<diff base='{self.ws.base_branch}'>\n{diff}\n</diff>",
            self.knowledge_review_block,
            "The repository you can read with the tools is the integrated result. Review it and call submit_review.",
        ])
        term = TerminalTool("submit_review", "Submit the structured code review.", REVIEW_JSON_SCHEMA,
                            lambda a: validate_review(a.get("review", a)))
        for attempt in (1, 2):
            res = await self._agent("reviewer", f"review-c{cycle}-{attempt}", prompts.REVIEWER_SYSTEM, user,
                                    READ_TOOLS, term, ToolContext(workspace=self.ws.repo))
            if res.ok:
                rv = res.value
                for f in rv["findings"]:
                    self.store.record_finding(self.run_id, cycle=cycle, source="reviewer", severity=f["severity"],
                                              file=f.get("file"), line=f.get("line"), title=f.get("title"),
                                              description=f.get("description"), recommendation=f.get("recommendation"))
                counts = {s: sum(1 for f in rv["findings"] if f["severity"] == s)
                          for s in ("critical", "major", "minor", "info")}
                self.log("review", f"verdict={rv['verdict']} findings={counts}", data=rv)
                return rv
            self.log("review", f"review attempt {attempt} failed: {res.error}", level="warn")
        raise RunAborted(RunState.FAILED, "independent reviewer failed to produce a review; cannot approve")

    # ==================================================================== gate
    def _gate(self, cycle, ev: TestEvidence, tester_report, review, failed_tasks) -> list[dict]:
        """Deterministic approval gate. Returns blocking problems (empty == approved)."""
        problems = []
        if not ev.ok:
            problems.append({"kind": "tests", "summary": f"test suite not passing: {ev.short()}",
                             "detail": ev.failure_report()})
        for r in getattr(self, "_criteria_results", []):
            if r["status"] == "failed":
                problems.append({"kind": "acceptance", "summary": f"acceptance criterion {r['criterion_id']} failing",
                                 "detail": f"{r['criterion_id']}: {r['text']}\nEvidence: {r['evidence']}"})
        blocking = set(self.cfg.review.blocking_severities)
        if review:
            for f in review["findings"]:
                if f["severity"] in blocking:
                    problems.append({"kind": "review", "summary": f"[{f['severity']}] {f['title']} ({f.get('file')})",
                                     "detail": json.dumps(f, indent=1)})
        if tester_report:
            for d in tester_report["defects"]:
                if d["severity"] in blocking:
                    problems.append({"kind": "defect", "summary": f"[defect/{d['severity']}] {d['title']}",
                                     "detail": json.dumps(d, indent=1)})
        for tid in failed_tasks:
            row = next((t for t in self.store.tasks(self.run_id) if t["task_key"] == tid), {})
            problems.append({"kind": "task", "summary": f"task {tid} did not complete",
                             "detail": f"Task {tid} ({row.get('title')}) failed: {row.get('error')}\n"
                                       f"{row.get('description')}"})
        return problems

    # ================================================================ delivery
    async def _deliver(self) -> None:
        issue = self.issue
        if self.mode == "single":
            self.sm.transition(RunState.COMPLETED, "single-agent run finished (no independent review, so no PR is created)")
            return
        owner = issue.owner or (self.ref.owner if getattr(self, "ref", None) else None)
        name = issue.repo or (self.ref.name if getattr(self, "ref", None) else None)
        if not owner:
            src = Path(self.source)
            if src.is_dir() and (src / ".git").exists():
                try:
                    branch = await asyncio.to_thread(self.ws.deliver_to_local, src)
                    self.log("pr", f"result delivered as new branch '{branch}' in {src}. Review with "
                                   f"`git diff {self.ws.base_branch}...{branch}` and merge it yourself.")
                    self.sm.transition(RunState.COMPLETED, f"approved; delivered to branch {branch} in {src}")
                    return
                except GitError as e:
                    self.log("pr", f"could not deliver branch to {src}: {e}", level="warn")
            self.log("pr", f"local issue: no PR. Result is on branch {self.ws.integration_branch} in {self.ws.repo}")
            self.sm.transition(RunState.COMPLETED, f"approved; local run, branch {self.ws.integration_branch}")
            return
        if not self.cfg.github.create_pr:
            self.sm.transition(RunState.COMPLETED, "approved; PR creation disabled in config")
            return
        gh = GitHubClient(self.cfg.github)
        full = f"{owner}/{name}"
        if not gh.token or not await asyncio.to_thread(gh.can_push, owner, name):
            step = (f"Set {self.cfg.github.token_env} to a token with contents:write and pull_requests:write "
                    f"on {full} (or run `gh auth login`), then run: swarmforge publish {self.run_id}")
            self.log("pr", f"cannot publish: no push access to {full}. Remaining step: {step}", level="warn")
            self.sm.transition(RunState.COMPLETED, f"approved; PR not created (no push access). {step}")
            return
        self.sm.transition(RunState.PULL_REQUEST_CREATION)
        url = await self.publish(gh, owner, name)
        if url:
            self.sm.transition(RunState.COMPLETED, f"draft PR created: {url}")
        else:
            self.sm.transition(RunState.COMPLETED, "approved; push declined by user, PR not created")

    async def publish(self, gh: GitHubClient, owner: str, name: str) -> str | None:
        issue, ws = self.issue, self.ws
        full = f"{owner}/{name}"
        if self.cfg.github.require_push_approval:
            q = (f"Push branch '{ws.integration_branch}' to {full} and open a DRAFT pull request "
                 f"against '{ws.base_branch}'?")
            self.log("pr", f"waiting for human approval: {q}")
            approved = bool(self.approve and await asyncio.to_thread(self.approve, q))
            self.store.record_intervention(self.run_id, "push_approval",
                                           f"{'approved' if approved else 'declined'}: {q}")
            if not approved:
                return None
        await asyncio.to_thread(push_branch, ws.repo, owner, name, ws.integration_branch,
                                gh.token, ws.base_branch, self.cfg.github.branch_prefix)
        self.log("pr", f"pushed {ws.integration_branch} to {full}")
        body = build_pr_body(self.store, self.run_id)
        title = f"[SwarmForge] {issue.title}" + (f" (#{issue.number})" if issue.number else "")
        url = await asyncio.to_thread(gh.create_draft_pr, owner, name, ws.integration_branch,
                                      ws.base_branch, title, body)
        self.store.update_run(self.run_id, pr_url=url)
        self.log("pr", f"draft pull request: {url}")
        return url

    # =============================================================== knowledge
    def _apply_project_profile(self) -> None:
        """Per-repository settings from `swarmforge.project.yml` in the repo root (only the test
        command and sandbox image), then CLI/dashboard overrides. Network, limits and every other
        safety setting can only come from the user's own swarmforge.yaml."""
        prof = {}
        f = self.ws.repo / PROJECT_FILE
        if f.is_file():
            try:
                prof = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as e:
                raise RunAborted(RunState.BLOCKED, f"{PROJECT_FILE} is not valid YAML: {e}")
            if not isinstance(prof, dict):
                raise RunAborted(RunState.BLOCKED, f"{PROJECT_FILE} must be a mapping")
            unknown = set(prof) - {"test_command", "agent_test_command", "sandbox_image", "install_command",
                                   "description"}
            if unknown:
                self.log("project", f"ignoring unsupported keys in {PROJECT_FILE}: {sorted(unknown)}", level="warn")
        inst = prof.get("install_command")
        if inst is not None:
            if not isinstance(inst, str) or not inst.strip():
                raise RunAborted(RunState.BLOCKED, "install_command must be a non-empty string")
            self.install_command = inst.strip()
        test_cmd = self._override_test_command or prof.get("test_command")
        quick = prof.get("agent_test_command")
        if quick is not None and not self._override_test_command:
            if not isinstance(quick, str) or not quick.strip():
                raise RunAborted(RunState.BLOCKED, "agent_test_command must be a non-empty string")
            self.cfg.project.agent_test_command = quick.strip()
        image = self._override_image or prof.get("sandbox_image")
        if test_cmd:
            if not isinstance(test_cmd, str) or not test_cmd.strip():
                raise RunAborted(RunState.BLOCKED, "test_command must be a non-empty string")
            self.cfg.project.test_command = test_cmd.strip()
        if image:
            if not isinstance(image, str) or not IMAGE_RE.match(image):
                raise RunAborted(RunState.BLOCKED, f"invalid sandbox image name {image!r}")
            if self.cfg.sandbox.mode == "docker" and self._sandbox_override is None:
                import subprocess as _sp
                if _sp.run([self.cfg.sandbox.docker_binary, "image", "inspect", image],
                           capture_output=True).returncode != 0:
                    raise RunAborted(RunState.BLOCKED, (
                        f"sandbox image '{image}' (requested by {PROJECT_FILE} or the run settings) is not "
                        "built on this machine. SwarmForge never pulls images on a repository's behalf; build "
                        "it first (for SwarmForge itself: `swarmforge sandbox build --dev`)."))
            self.cfg.sandbox.image = image
        if test_cmd or image or prof:
            self.log("project", f"project profile: test command `{self.cfg.project.test_command}`, "
                                f"sandbox image `{self.cfg.sandbox.image}`"
                                + (f" (from {PROJECT_FILE})" if prof else " (run override)"))

    async def _ensure_deps(self, path: Path, key: str) -> str:
        """Install declared project dependencies into the worktree (network only for this step)."""
        if not self.cfg.sandbox.dependency_install:
            return "disabled"
        plan = plan_install(path, getattr(self, "install_command", None))
        failed = getattr(self, "_deps_failed", set())
        if plan.command and plan.fingerprint in failed:
            return "failed"  # same dependency set already failed in this run; don't repeat it
        status, res, plan = await ensure_deps(
            self.sandbox, path, network=self.cfg.sandbox.install_network,
            timeout_s=self.cfg.sandbox.install_timeout_s, cache_dir=self.cfg.workdir / "pipcache",
            install_command=getattr(self, "install_command", None))
        if status == "installed":
            self.log("deps", f"installed dependencies from {', '.join(plan.sources)} in {res.duration_s:.0f}s",
                     task_key=key if key != "repo" else None)
        elif status == "satisfied":
            self.log("deps", f"dependencies from {', '.join(plan.sources)} are already provided by the sandbox image",
                     task_key=key if key != "repo" else None)
        elif status == "failed":
            self._deps_failed = failed | {plan.fingerprint}
            self.log("deps", f"dependency install from {', '.join(plan.sources)} failed (exit {res.exit_code}); "
                             f"tests may fail with import errors:\n{res.output[-1500:]}", level="warn",
                     task_key=key if key != "repo" else None)
        return status

    def _tool_installer(self):
        if not self.cfg.sandbox.dependency_install:
            return None

        async def install(path: Path) -> str:
            status, res, plan = await ensure_deps(
                self.sandbox, path, network=self.cfg.sandbox.install_network,
                timeout_s=self.cfg.sandbox.install_timeout_s, cache_dir=self.cfg.workdir / "pipcache",
                install_command=getattr(self, "install_command", None))
            if status == "none":
                return ("No dependency files found. Declare packages in requirements.txt (one per line) "
                        "or in pyproject.toml [project].dependencies, then call this again.")
            if status == "current":
                return f"Dependencies from {', '.join(plan.sources)} are already installed and unchanged."
            out = res.summary(4000) if res else ""
            return (f"Dependency install {'succeeded' if status == 'installed' else 'FAILED'} "
                    f"(sources: {', '.join(plan.sources)}).\n{out}")
        return install

    def _retrieve_knowledge(self) -> None:
        if not self.cfg.knowledge.enabled:
            return
        vault = Vault(self.cfg.vault_path)
        q = f"{self.issue.title}\n{self.issue.body}"
        k = self.cfg.knowledge.max_notes_in_prompt
        found = vault.search(q, repo=self.repo_key, k=k)
        if not found:
            self.log("knowledge", f"no relevant notes in {vault.root}")
            return
        self.knowledge_used = [n.stem for _, n in found]
        self.knowledge_block = render_for_prompt(found)
        self.knowledge_dev_block = render_for_prompt(
            [x for x in found if x[1].type in ("pitfall", "pattern", "repo-fact", "decision", "user-note")][:4], 600)
        self.knowledge_review_block = render_for_prompt(
            [x for x in found if x[1].type in ("review-lesson", "pitfall", "decision", "user-note")][:4], 600)
        self.log("knowledge", f"retrieved {len(found)} note(s) for planner/developers/reviewer: "
                              + "; ".join(n.title for _, n in found),
                 data=[{"stem": n.stem, "title": n.title, "type": n.type, "score": sc} for sc, n in found])

    def _run_digest(self) -> str:
        run = self.store.get_run(self.run_id)
        tasks = self.store.tasks(self.run_id)
        L = [f"Issue: {self.issue.title}", f"Repository: {self.repo_key}",
             f"Final state: {run['state']} - {run['state_reason']}", f"Repair cycles: {run['repair_cycle']}"]
        plan = getattr(self, "plan", None)
        if plan:
            L.append(f"Plan: {plan.summary}")
        L.append("Tasks:")
        for t in tasks:
            L.append(f"- {t['task_key']} [{t['kind']}, cycle {t['cycle']}] {t['title']} | role {t['role']} "
                     f"model {t['model']} | {t['state']} | attempts {t['attempts']}\n  summary: "
                     f"{(t['summary'] or t['error'] or '')[:700]}")
        L.append("Test runs (executed by orchestrator):")
        for tr in self.store.test_runs(self.run_id):
            L.append(f"- {tr['label']}: exit {tr['exit_code']}, {tr['passed']} passed, {tr['failed']} failed, {tr['errors']} errors")
        f = self.store.findings(self.run_id)
        if f:
            L.append("Review findings and defects (cycle, source, severity, file, title, recommendation):")
            for x in f:
                L.append(f"- c{x['cycle']} {x['source']} {x['severity']} {x['file']}: {x['title']} -> {(x['recommendation'] or '')[:300]}")
        warn = [e for e in self.store.events(self.run_id) if e["level"] in ("warn", "error")]
        if warn:
            L.append("Warnings/errors:")
            L += [f"- {e['kind']} {e['task_key'] or ''}: {e['message'][:240]}" for e in warn[-25:]]
        c = self.store.cost_summary(self.run_id)
        L.append(f"Cost ${c['total']:.4f}, {c['total_tokens']} tokens. By role:")
        for r in self.store.cost_by_role(self.run_id):
            L.append(f"- {r['role']} ({r['model']}): {r['calls']} calls, ${r['cost_usd']:.4f}")
        if self.knowledge_used:
            L.append("Notes that were retrieved for this run: " + "; ".join(self.knowledge_used))
        return "\n".join(L)

    async def _capture_knowledge(self) -> None:
        if not self.store.tasks(self.run_id):
            self.log("knowledge", "run stopped before any work was planned; nothing to record")
            return
        vault = Vault(self.cfg.vault_path)
        existing = vault.notes()
        digest = self._run_digest()
        written: list = []
        role = self.cfg.librarian_role
        try:
            if self._should_cancel():
                raise RunAborted(RunState.CANCELLED, "run was cancelled")
            self.budget.check()
            related = vault.search(digest, repo=self.repo_key, k=25)
            catalogue = "\n".join(f"- [{n.type}] {n.title}" for _, n in related) or "(none yet)"
            term = TerminalTool("submit_notes", "Submit the distilled Zettelkasten notes.", NOTES_JSON_SCHEMA,
                                lambda a: validate_notes(a, self.cfg.knowledge.max_notes_per_run))
            user = "\n\n".join([
                "<run_digest>\n" + digest + "\n</run_digest>",
                "Existing notes you may link to (use their exact titles in `links`):\n" + catalogue,
                f"Write 0-{self.cfg.knowledge.max_notes_per_run} atomic notes, then call submit_notes.",
            ])
            res = await self._agent(role, "librarian", prompts.LIBRARIAN_SYSTEM, user, READ_TOOLS, term,
                                    ToolContext(workspace=self.ws.repo if self.ws and self.ws.repo.exists() else vault.root))
            if res.ok:
                model = self.cfg.roles[role].model
                written = write_distilled(vault, res.value, repo=self.repo_key, run=self.run_id,
                                          source=f"agent:{role}/{model}", existing=existing)
            else:
                self.log("knowledge", f"librarian did not produce notes: {res.error}", level="warn")
        except (BudgetExceeded, RunAborted) as e:
            self.log("knowledge", f"skipping librarian: {e}", level="warn")
        run = self.store.get_run(self.run_id)
        body = [f"Automatic log of SwarmForge run `{self.run_id}` on **{self.repo_key}**.", "",
                f"- Issue: {self.issue.title} ({self.issue.url or '-'})",
                f"- Outcome: **{run['state']}** - {run['state_reason']}",
                f"- Result branch: `{run['integration_branch']}`" + (f", PR {run['pr_url']}" if run['pr_url'] else ""),
                f"- Report: `{self.run_dir / 'report.md'}`", ""]
        if written:
            body += ["## Notes distilled from this run"] + [f"- [[{n.stem}|{n.title}]]" for n in written] + [""]
        if self.knowledge_used:
            body += ["## Notes this run started from"] + [f"- [[{s}]]" for s in self.knowledge_used] + [""]
        try:
            body += ["## Request and summary", "", build_summary(self.store, self.run_id, lessons=False), ""]
        except Exception as e:  # never let the summary break knowledge capture
            self.log("knowledge", f"could not add the summary to the run log: {e}", level="warn")
        body += ["## Digest", "```", digest[:6000], "```"]
        log_note = vault.write(f"Run {self.run_id} - {self.issue.title}", "\n".join(body), type="run-log",
                               tags=["run-log", run["state"].lower()], repo=self.repo_key, run=self.run_id,
                               source="orchestrator")
        vault.refresh_hub(self.repo_key)
        self.log("knowledge", f"wrote {len(written)} note(s) + run log to {vault.root}: "
                              + "; ".join(n.title for n in written),
                 data=[{"stem": n.stem, "title": n.title, "type": n.type} for n in written + [log_note]])

    # ================================================================= helpers
    def _test_hint(self) -> str:
        quick = self.cfg.project.agent_test_command
        return "\n".join([
            f"Full test suite (the orchestrator runs it independently as the evidence): "
            f"`{self.cfg.project.test_command}`",
            "While you work, run only the tests relevant to your change (e.g. "
            "`python -m pytest -q -x tests/test_<module>.py`)"
            + (f", or the quick suite `{quick}` before you finish" if quick else
               "; run the full suite at most once, at the end") + ".",
        ])

    async def _wait_for_repo(self, repo: str | None, issue_url: str | None) -> None:
        """Wait while an earlier run on the same repository is still active (see repolock.py)."""
        if not self.cfg.limits.one_run_per_repo:
            return
        key = lock_key(repo, issue_url)
        if not key:
            return
        row = self.store.get_run(self.run_id) or {}
        if not row.get("repo"):
            self.store.update_run(self.run_id, repo=repo or key)
        hint = getattr(self, "_issue_file_hint", None)
        if not row.get("issue_title") and hint:   # show a real title while waiting
            try:
                first = Path(hint).read_text(encoding="utf-8").splitlines()[0]
                if first.startswith("# "):
                    self.store.update_run(self.run_id, issue_title=first[2:].strip()[:200])
            except (OSError, IndexError):
                pass
        waited, announced = 0.0, None
        while True:
            blockers = await asyncio.to_thread(blocking_runs, self.store, self.run_id, key)
            if not blockers:
                break
            ids = ", ".join(b["id"] for b in blockers)
            if ids != announced:
                self.log("queue", f"waiting: run(s) {ids} are already working on this repository; this run "
                                  "starts when they finish (the time limit starts then)")
                self.store.update_run(self.run_id, state_reason=f"waiting for the repository: run(s) {ids} "
                                                                "are working on it")
                announced = ids
            if self._should_cancel():
                raise RunAborted(RunState.CANCELLED, "cancelled while waiting for the repository")
            await asyncio.sleep(self._repo_poll_s)
            waited += self._repo_poll_s
        if announced:
            self.store.update_run(self.run_id, state_reason=None)
            self.log("queue", f"repository is free after {waited / 60:.0f} min; starting")

    async def _run_pi(self, spec: AgentSpec, rc) -> AgentResult:
        from .agent.pi_runtime import THINKING, ModelRelay, resolve_local_command, run_pi_agent
        from .sandbox import DockerSandbox
        docker = isinstance(spec.ctx.sandbox, DockerSandbox)
        if getattr(self, "_relay", None) is None:
            host = self.cfg.pi.relay_host or ("0.0.0.0" if docker and sys.platform.startswith("linux") else "127.0.0.1")
            self._relay = ModelRelay(self.llm, host=host)
            await self._relay.start()
            self.log("agent", f"Pi model relay listening on {host}:{self._relay.port}")
            if docker and self.cfg.pi.network == "bridge":
                self.log("agent", "Pi agents' containers use the bridge network so they can reach the model relay "
                                  "(the API key stays on the host)", level="warn")
        try:
            command = self.cfg.pi.command if docker else resolve_local_command(self.cfg.pi.command)
        except FileNotFoundError as e:
            return AgentResult(status="error", error=str(e))
        effort = rc.reasoning_effort or self.cfg.reasoning_effort
        return await run_pi_agent(
            relay=self._relay, ctx=spec.ctx, role=spec.role, task_key=spec.task_key, system=spec.system_prompt,
            prompt=spec.user_prompt, terminal=spec.terminal, max_steps=spec.max_steps, timeout_s=spec.timeout_s,
            max_tokens=rc.max_output_tokens, thinking=THINKING.get(effort) if effort else None, command=command,
            network=self.cfg.pi.network, extra_args=list(self.cfg.pi.extra_args), transcript=spec.transcript_path,
            should_cancel=spec.should_cancel, on_event=spec.on_event)

    def _prompt_group(self, role: str) -> str:
        if role in self.cfg.developer_roles:
            return "developer"
        if role == self.cfg.librarian_role and role not in ("planner", "reviewer", "tester"):
            return "librarian"
        return role

    def _exec_tools(self) -> list[str]:
        return EXEC_TOOLS + (DEV_TOOLS if self.cfg.sandbox.dev_tools else [])

    def _check_cancel(self) -> None:
        if self._should_cancel():
            self.store.record_intervention(self.run_id, "cancel", "cancel requested via CLI")
            raise RunAborted(RunState.CANCELLED, "cancel requested")

    async def _agent(self, role: str, key: str, system: str, user: str, tools: list[str],
                     terminal: TerminalTool, ctx: ToolContext, transcript_suffix: str = "") -> AgentResult:
        rc = self.cfg.roles[role]
        name = f"{key}-{role}" + (f"-{transcript_suffix}" if transcript_suffix else "")
        use_pi = rc.runtime == "pi" and terminal.name == "finish" and ctx.sandbox is not None
        if use_pi:   # Pi brings its own tools (read/write/edit/bash): describe the job without ours
            system = prompts.PI_DEVELOPER_SYSTEM if system == prompts.DEVELOPER_SYSTEM else system + prompts.PI_NOTE
        extra = self.cfg.prompt_addenda.get(self._prompt_group(role))
        if extra:
            system = system + "\n\nAdditional guidance for this role:\n" + extra
        spec = AgentSpec(
            role=role, task_key=key, system_prompt=system, user_prompt=user, tools=tools,
            terminal=terminal, ctx=ctx, max_steps=rc.max_steps, timeout_s=rc.timeout_s,
            transcript_path=self.run_dir / "transcripts" / f"{name}.jsonl",
            should_cancel=self._should_cancel,
            on_event=lambda kind, msg, data: self.log("agent", msg, task_key=key, role=role, data=data))
        self.log("agent", f"start ({rc.model}{', Pi harness' if use_pi else ''})", task_key=key, role=role)
        try:
            res = await (self._run_pi(spec, rc) if use_pi else run_agent(self.llm, spec))
        finally:
            await ctx.aclose()   # stop processes/browser session the agent left running
        self.log("agent", f"end: {res.status} after {res.steps} step(s), {res.tool_calls} tool call(s), "
                          f"{res.duration_s:.0f}s" + (f" - {res.error}" if res.error else ""),
                 task_key=key, role=role, level="info" if res.ok else "warn")
        if res.ok and res.hit_limit:
            self.log("agent", f"submitted only after hitting its limit ({res.hit_limit}); the result may be "
                              "incomplete", task_key=key, role=role, level="warn")
        if res.status == "budget":
            raise RunAborted(RunState.BUDGET_EXCEEDED, res.error or "budget exceeded")
        if res.status == "cancelled":
            self.store.record_intervention(self.run_id, "cancel", "cancel requested via CLI")
            raise RunAborted(RunState.CANCELLED, "cancel requested")
        return res


def continue_context(store, run_id: str) -> str | None:
    """Text appended to the task when continuing a stopped run: what was done and what's still open."""
    run = store.get_run(run_id)
    if not run:
        return None
    L = ["## Previous attempt (continue from here)",
         f"A previous SwarmForge run ({run['id']}) worked on this task and stopped: "
         f"{run['state']} - {(run['state_reason'] or '')[:400]}.",
         "Its code changes are ALREADY in this repository. Do not redo finished work; build on it and "
         "resolve the remaining problems below, then make sure every acceptance criterion is met."]
    tasks = [t for t in store.tasks(run_id) if t["kind"] in ("implement", "fix")]
    if tasks:
        L.append("\nWork already done:")
        L += [f"- {t['task_key']} {t['title']} ({t['state']})" for t in tasks]
    last_cycle = run["repair_cycle"]
    blocking = [f for f in store.findings(run_id, last_cycle) if f["severity"] in ("critical", "major")]
    if blocking:
        L.append("\nUnresolved problems from the last review/test cycle:")
        L += [f"- [{f['severity']}] {f['file'] or ''}: {f['title']} -> {(f['recommendation'] or '')[:300]}"
              for f in blocking[:12]]
    tests = [t for t in store.test_runs(run_id) if t["label"] != "baseline"]
    if tests and (tests[-1]["exit_code"] != 0 or tests[-1]["failed"] or tests[-1]["errors"]):
        t = tests[-1]
        L.append(f"\nLast test run was failing: {t['passed']} passed, {t['failed']} failed, {t['errors']} errors.")
    return "\n".join(L)


PROJECT_FILE = "swarmforge.project.yml"
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,200}$")


def repo_key(issue: Issue, source: str) -> str:
    """Stable name for a repository, used to group knowledge notes."""
    if issue.full_repo:
        return issue.full_repo
    src = source.rstrip("/\\")
    m = re.match(r"^(?:https?://|git@)github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", src)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return Path(src).name or src


def _revert_non_test_changes(path: Path) -> list[str]:
    out = git(path, "status", "--porcelain", "-uall")
    reverted = []
    for line in out.splitlines():
        status, f = line[:2], line[3:].strip().strip('"')
        if " -> " in f:
            f = f.split(" -> ", 1)[1]
        if is_test_path(f):
            continue
        if status.strip() == "??":
            (path / f).unlink(missing_ok=True)
        else:
            git(path, "checkout", "HEAD", "--", f, check=False)
        reverted.append(f)
    return reverted


def _config_snapshot(cfg: Config) -> dict:
    return {
        "roles": {k: {"provider": v.provider, "model": v.model, "temperature": v.temperature,
                      "max_steps": v.max_steps,
                      "reasoning_effort": v.reasoning_effort or cfg.reasoning_effort} for k, v in cfg.roles.items()},
        "developer_roles": cfg.developer_roles,
        "limits": asdict(cfg.limits),
        "sandbox": {"mode": cfg.sandbox.mode, "image": cfg.sandbox.image, "network": cfg.sandbox.network},
        "test_command": cfg.project.test_command,
    }
