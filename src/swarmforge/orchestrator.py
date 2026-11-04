"""The orchestrator: owns the workflow state machine, safety gates and agent coordination.

LLM agents propose plans, code, tests and reviews. Deterministic code here decides every state
transition, runs the tests itself, applies the approval gate and controls GitHub publishing.
"""
from __future__ import annotations

import asyncio
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
from . import evidence
from .agent.runtime import AgentResult, AgentSpec, run_agent
from .agent.navtools import NAV_READONLY, NAV_TOOLS
from .agent.tools import DEV_TOOLS, EXEC_TOOLS, READ_TOOLS, WRITE_TOOLS, TerminalTool, ToolContext, ToolError
from .config import Config
from .db import Store
from .github import GitHubClient, GitHubError, Issue, get_token, load_issue_file, parse_issue_url, push_branch
from .deps import ensure_deps, plan_install
from .preflight import environment_problems, sandbox_tool_warnings
from .repos import RepoError, _redact, auth_args, normalize_repo, preflight
from .knowledge import NOTES_JSON_SCHEMA, Vault, render_for_prompt, validate_notes, write_distilled
from .llm import Budget, BudgetExceeded, LLMClient, LLMError
from .plan import (PLAN_JSON_SCHEMA, REVIEW_JSON_SCHEMA, TEST_REPORT_JSON_SCHEMA, Criterion, Plan,
                   PlanValidationError, Task, validate_plan, validate_review, validate_test_report)
from .report import build_summary, write_report, write_summary
from .sandbox import Sandbox, make_sandbox
from .scheduler import Scheduler
from .state import (RunAborted, RunState, StateMachine, TaskState,
                    normalize_scope_path, out_of_scope_findings, repair_tasks_from_findings)
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
        assert mode in ("swarm", "single", "reviewed")
        from .effective_config import resolve_variant
        self.cfg, self._variant_note = resolve_variant(config, store)
        self.store = store
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
        self._evidence_done = False
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
        from .effective_config import effective_config, policy_summary
        (self.run_dir / "effective-config.json").write_text(json.dumps(effective_config(self.cfg), indent=2), encoding="utf-8")
        self.log("policy", policy_summary(self.cfg))
        self.log("run", f"run {self.run_id} started (mode={self.mode}, budget ${self.cfg.limits.budget_usd:.2f}, "
                        f"sandbox: {self.sandbox.describe()})")
        self._started = time.monotonic()
        self._timed_out = False
        self._issue_file_hint = issue_file
        self._repo_arg, self._issue_url_arg = repo, issue_url
        self._git_auth: list[str] = []
        try:
            if self._same_repo_mode() == "serial":
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
                if self.store.get_run(self.run_id)["state"] != "COMPLETED":
                    await self._collect_evidence()
            except Exception as e:  # evidence must never change the run outcome
                self.log("evidence", f"evidence collection failed: {type(e).__name__}: {e}", level="warn")
            try:
                path = write_report(self.store, self.run_id, self.run_dir)
                self.log("report", f"execution report written to {path}")
            except Exception as e:
                self.log("error", f"failed to write report: {e}", level="error")
            try:
                from .metrics import format_run, run_metrics, write_run_metrics
                write_run_metrics(self.store, self.run_id, self.run_dir)
                m = run_metrics(self.store, self.run_id)
                if m:
                    self.log("metrics", "where the time went:\n" + format_run(m))
            except Exception as e:  # metrics are for study; never change the outcome
                self.log("metrics", f"could not compute metrics: {e}", level="warn")
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
    def _merged_base_gone(self, base_branch: str | None, err: Exception) -> bool:
        """True when the requested base is one of our own result branches that has disappeared."""
        prefix = self.cfg.github.branch_prefix or "swarmforge/"
        return bool(base_branch and base_branch.startswith(prefix) and "does not exist in" in str(err))

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
        if self._same_repo_mode() == "coordinate":
            await self._coordinate("start")

        # -------------------------------------------------- inspection
        self.sm.transition(RunState.REPOSITORY_INSPECTION)
        # Accept anything reasonable ("owner/repo", GitHub web URLs like .../tree/main, local paths)
        # and verify it is reachable and non-empty before doing anything else.
        token = get_token(self.cfg.github)
        try:
            ref = normalize_repo(str(source))
            if base_branch:
                ref.branch = base_branch
            try:
                ref = await asyncio.to_thread(preflight, ref, token)
            except RepoError as e:
                if not self._merged_base_gone(base_branch, e):
                    raise
                # The base was an earlier SwarmForge result whose PR has since been merged (merging deletes
                # the branch), so its work is in the default branch now: start from there instead.
                self.log("repo", f"base branch '{base_branch}' no longer exists (its pull request was most likely "
                                 "merged and the branch deleted); starting from the default branch instead",
                         level="warn")
                ref = normalize_repo(str(source))
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
                # marks the block as an environment problem: the queue pauses instead of failing every task
                self.log("environment", " ".join(problems)[:1000], level="error")
                raise RunAborted(RunState.BLOCKED, " ".join(problems))
            for w in await asyncio.to_thread(sandbox_tool_warnings, self.cfg, self._override_image):
                self.log("environment", f"{w} (not blocking: the run continues)", level="warn")
        self.ws = GitWorkspace(self.run_dir, self.run_id, self.cfg.github.branch_prefix)
        self.source = str(source)
        try:
            self._git_auth = ["-c", "credential.helper=", *(auth_args(str(source), token) if ref.use_auth else [])]
            await asyncio.to_thread(self.ws.prepare, str(source), issue.number, base_branch, self._git_auth)
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
        from .effective_config import effective_config
        (self.run_dir / "effective-config.json").write_text(json.dumps(effective_config(self.cfg), indent=2), encoding="utf-8")
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
        if self._same_repo_mode() == "coordinate":
            await self._coordinate("plan")

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
            self._visual_findings = await self._visual_review(cycle)
            review = await self._review(cycle, evidence)

            problems = self._gate(cycle, evidence, tester_report, review, failed_tasks)
            if not problems:
                problems = await self._update_with_base(cycle)
            if not problems:
                self.sm.transition(RunState.APPROVED,
                                   f"tests passed ({evidence.short()}); no blocking findings")
                break
            self.sm.transition(RunState.FIX_REQUIRED, f"{len(problems)} blocking problem(s)")
            for p in problems:
                self.log("gate", p["summary"], level="warn")
            max_cycles = self.cfg.limits.max_repair_cycles if self.mode != "single" else 0
            if cycle >= max_cycles:
                raise RunAborted(RunState.FAILED,
                                 f"repair limit reached ({max_cycles} cycle(s)); "
                                 f"{len(problems)} unresolved problem(s): "
                                 + "; ".join(p["summary"] for p in problems)[:800])
            cycle += 1
            self.store.update_run(self.run_id, repair_cycle=cycle)
            blocking = set(self.cfg.review.blocking_severities)
            review_findings = (review or {}).get("findings") or []
            scope = self._repair_scope()
            for f in out_of_scope_findings(review_findings, scope, blocking):
                self.log("scope", f"out-of-scope finding skipped from repair creation "
                         f"(not in original plan file set): {f.get('title')} ({f.get('file')})",
                         level="warn", data=f)
            repair_tasks = repair_tasks_from_findings(review_findings,
                                                      self.cfg.developer_roles, cycle, blocking,
                                                      scope_files=scope)
            if repair_tasks:
                self._register_tasks(repair_tasks, cycle=cycle, kind="fix")
                self.log("plan", f"cycle {cycle}: {len(repair_tasks)} mandatory repair task(s) "
                         "created from reviewer findings",
                         data=[asdict(t) for t in repair_tasks])
            fix_plan = await self._make_fix_plan(cycle, problems, done_ids)
            self.sm.transition(RunState.TASK_VALIDATION)
            self._register_tasks(fix_plan.tasks, cycle=cycle, kind="fix")
            self.log("plan", f"repair cycle {cycle}: {len(fix_plan.tasks)} corrective task(s)",
                     data=fix_plan.to_dict())
            tasks = repair_tasks + fix_plan.tasks

        await self._collect_evidence()
        await self._deliver()

    # ============================================================ visual evidence
    def _plan_file_set(self) -> list[str]:
        """Files the plan intends to change, plus what actually changed (deduped)."""
        files: list[str] = []
        plan = getattr(self, "plan", None)
        if plan is not None:
            for f in evidence.plan_files(plan):
                if f not in files:
                    files.append(f)
        ws = getattr(self, "ws", None)
        if ws is not None:
            try:
                for f in ws.changed_files():
                    if f not in files:
                        files.append(f)
            except Exception:  # a missing git workspace must never block evidence
                pass
        return files

    async def _collect_evidence(self) -> None:
        """Screenshot the dashboard tabs the plan touched, once per run.

        Never changes the run's outcome: capture failures are logged as warnings and skipped.
        """
        from . import visual
        if self._evidence_done:
            return
        if self.mode == "single":
            self.log("evidence", "no evidence screenshots: single-agent mode")
            self._evidence_done = True
            return
        files = self._plan_file_set()
        if not evidence.touches_ui(files):
            self.log("evidence", "no evidence screenshots: the plan does not touch dashboard files")
            self._evidence_done = True
            return
        if getattr(self, "ws", None) is None or self.run_dir is None:
            self.log("evidence", "no evidence screenshots: no workspace to capture from", level="warn")
            return
        failed = True
        try:
            failed = self.store.get_run(self.run_id)["state"] != "COMPLETED"
        except Exception:
            pass
        try:
            shots = await asyncio.to_thread(
                evidence.screenshot_tabs, self.ws.repo, self.run_dir / "screenshots", files,
                failed=failed)
        except (OSError, RuntimeError, visual.RegressError, visual.PreviewError) as e:
            self.log("evidence", f"screenshots skipped: {e}"[:1500], level="warn")
            return
        self._evidence_done = True
        self.log("evidence", f"{len(shots)} evidence screenshot(s) taken", data={"shots": shots})

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
        import hashlib
        cache_key = "baseline:" + hashlib.sha1("|".join([
            self.ws.base_sha or "", self.cfg.project.test_command, self.cfg.sandbox.image]).encode()).hexdigest()[:20]
        cached = self.store.kv_get(cache_key) if self.cfg.project.reuse_baseline else None
        if cached:
            d = json.loads(cached)
            ev = TestEvidence(label="baseline", command=d["command"], exit_code=0, timed_out=False, duration_s=0.0,
                              passed=d["passed"], failed=d["failed"], errors=d["errors"], skipped=d["skipped"],
                              junit_found=d["junit_found"], log_path=d.get("log_path"))
            self.log("test", f"baseline reused from run {d['run_id']} (same commit {(self.ws.base_sha or '')[:8]}, "
                             "same test command); skipped re-running it")
        else:
            ev = await run_tests(self.sandbox, self.ws.repo, self.cfg.project.test_command, self.artifacts,
                                 "baseline", self.cfg.sandbox.command_timeout_s)
            if ev.exit_code == 0 and not ev.timed_out:
                self.store.kv_set(cache_key, json.dumps({
                    "run_id": self.run_id, "command": ev.command, "passed": ev.passed, "failed": ev.failed,
                    "errors": ev.errors, "skipped": ev.skipped, "junit_found": ev.junit_found,
                    "log_path": ev.log_path}))
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
        if self.mode in ("single", "reviewed"):
            role = self.single_role
            self.log("plan", f"{self.mode} mode: one task for role {role}, no initial planner")
            return Plan(summary=f"{self.mode} workflow: one developer implements the whole issue.",
                        tasks=[Task(id="T1", title=self.issue.title, role=role,
                                    description="Implement the entire issue end to end, including tests "
                                                "and documentation.",
                                    acceptance_criteria=["Everything requested in the issue"])],
                        acceptance_criteria=([Criterion(id="AC1", text="Everything requested in the issue")]
                                             if self.mode == "reviewed" else []))
        user = "\n\n".join([
            prompts.render_issue(self.issue.title, self.issue.body),
            prompts.render_repo_summary(repo_summary),
            self.knowledge_block,
            f"Available developer roles: {self.cfg.developer_roles} "
            f"(models: {', '.join(f'{r}={self.cfg.roles[r].model}' for r in self.cfg.developer_roles)}).",
            f"Maximum number of tasks: {self.cfg.limits.max_tasks}. "
            f"Up to {self.cfg.limits.max_concurrent_agents} tasks can run concurrently.",
            self._peer_note(),
            "Explore the repository, then call submit_plan.",
        ])
        last_err = None
        for attempt in range(1, self.cfg.limits.max_planning_attempts + 1):
            res = await self._agent("planner", f"plan-{attempt}", prompts.PLANNER_SYSTEM, user,
                                    self._plan_tools(), self._plan_terminal(set(), True),
                                    ToolContext(workspace=self.ws.repo, base_sha=self.ws.base_sha))
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
                                    user, self._plan_tools(), self._plan_terminal(all_ids, False),
                                    ToolContext(workspace=self.ws.repo, base_sha=self.ws.base_sha))
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
                              artifacts_dir=self.artifacts / f"task-{task.id}",
                              test_command=self.cfg.project.test_command, base_sha=self.ws.base_sha)
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
                              artifacts_dir=self.artifacts / f"merge-{task.id}",
                              test_command=self.cfg.project.test_command, base_sha=self.ws.base_sha)
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
        if self.mode != "single":
            if cycle == 0 or getattr(self.cfg.limits, "tester_every_cycle", False) \
                    or getattr(self, "_first_tester_report", None) is None:
                tester_report = await self._run_tester(cycle)
                if tester_report and getattr(self, "_first_tester_report", None) is None:
                    self._first_tester_report = tester_report
            else:
                # Repair cycle: the acceptance tests from the first cycle still apply; just re-run the
                # suite (minutes) instead of having the tester agent write more tests (~10 min/cycle).
                self.log("test", "repair cycle: re-running the existing tests without the tester agent "
                                 "(limits.tester_every_cycle: true brings it back)")
                tester_report = {**self._first_tester_report, "defects": []}
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
                          artifacts_dir=self.artifacts / key, test_command=self.cfg.project.test_command,
                          base_sha=self.ws.base_sha)
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
    async def _visual_review(self, cycle: int) -> list[dict]:
        """Screenshots of the app (light + dark) and a vision model's look at them. Never fails the run
        by itself: a preview that won't start or a model error is logged and skipped."""
        from . import visual
        pv = getattr(self, "preview", None)
        if self.mode == "single" or pv is None:
            return []
        changed = await asyncio.to_thread(self.ws.changed_files)
        if not visual.should_capture(pv, changed):
            self.log("visual", "no screenshots: the change doesn't touch the files the preview watches")
            return []
        ctx = ToolContext(workspace=self.ws.repo, sandbox=self.sandbox, artifacts_dir=self.artifacts,
                          installer=self._tool_installer(), command_timeout_s=self.cfg.sandbox.command_timeout_s,
                          test_command=self.cfg.project.test_command, base_sha=self.ws.base_sha)
        try:
            shots = await visual.capture(ctx, pv, cycle, log=lambda m: self.log("visual", m))
        except (ToolError, OSError, RuntimeError) as e:
            self.log("visual", f"could not take screenshots (review continues without them): {e}"[:1500],
                     level="warn")
            return []
        finally:
            await ctx.aclose()
        self.log("visual", f"{sum(1 for s in shots if s['file'])} screenshot(s) taken (cycle {cycle})",
                 data={"cycle": cycle, "shots": [{k: s[k] for k in ("page", "path", "theme", "file", "problems")}
                                                 for s in shots]})
        golden_findings: list[dict] = []
        try:
            golden_findings = await asyncio.to_thread(visual.golden_gate, shots, self.ws.repo,
                                                       threshold=visual.DIFF_THRESHOLD)
        except (OSError, RuntimeError, visual.PreviewError) as e:
            self.log("visual", f"golden comparison skipped: {e}"[:800], level="warn")
        self.log("visual", f"golden diff: {len(shots)} tab(s) checked, {len(golden_findings)} regression(s)")
        findings = golden_findings + visual.page_error_findings(shots)
        role = self.cfg.visual_role
        if role and role in self.cfg.roles and any(s["file"] for s in shots):
            from .llm import model_supports_vision
            if not model_supports_vision(self.cfg.roles[role].model, self.cfg.vision_models):
                self.log("visual", f"the {role} model ({self.cfg.roles[role].model}) can't see images; add it to "
                                   "vision_models in swarmforge.yaml if it can", level="warn")
            else:
                msgs = visual.review_messages(prompts.VISUAL_REVIEW_SYSTEM, self.issue.title, self.issue.body,
                                              changed, shots)
                try:
                    res = await self.llm.chat(role, msgs, task_key=f"visual-c{cycle}")
                    summary, more = visual.parse_review(res.content)
                    findings += more
                    self.log("visual", f"visual review ({self.cfg.roles[role].model}): {summary}",
                             data={"cycle": cycle, "summary": summary})
                except (LLMError, ValueError) as e:
                    self.log("visual", f"visual review failed (screenshots kept): {e}"[:800], level="warn")
        elif not role or role not in self.cfg.roles:
            self.log("visual", "screenshots taken; no vision model configured (roles.designer) to review them")
        for f in findings:
            self.store.record_finding(self.run_id, cycle=cycle, source="visual", severity=f["severity"],
                                      file=f"{f['page']} ({f['theme']})" if f.get("theme") else f["page"],
                                      line=None, title=f["title"], description=f["description"],
                                      recommendation=f["recommendation"])
        counts = {sv: sum(1 for f in findings if f["severity"] == sv) for sv in visual.SEVERITIES}
        self.log("visual", f"visual findings: {counts} (critical and golden regressions block)",
                 level="warn" if counts["critical"] else "info")
        return findings

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
            ("Repair review: prioritize the corrective diff and unresolved prior findings. Verify each fix, "
             "check adjacent regressions, and do not repeat resolved findings without new evidence.\n"
             + json.dumps(self.store.findings(self.run_id)[-30:], default=str)
             if cycle else "Review changed interfaces and acceptance coverage before style concerns."),
            "The repository you can read with the tools is the integrated result. Review it and call submit_review.",
        ])
        term = TerminalTool("submit_review", "Submit the structured code review.", REVIEW_JSON_SCHEMA,
                            lambda a: validate_review(a.get("review", a)))
        for attempt in (1, 2):
            res = await self._agent("reviewer", f"review-c{cycle}-{attempt}", prompts.REVIEWER_SYSTEM, user,
                                    self._review_tools(), term,
                                    ToolContext(workspace=self.ws.repo, base_sha=self.ws.base_sha))
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
    def _repair_scope(self) -> set[str]:
        """Normalized set of files the original plan's tasks declared as expected."""
        plan = getattr(self, "plan", None)
        if plan is None:
            return set()
        return {normalize_scope_path(f) for t in plan.tasks for f in t.expected_files
                if normalize_scope_path(f)}

    def _gate(self, cycle, ev: TestEvidence, tester_report, review, failed_tasks) -> list[dict]:
        from .services.assessment import assess_gate
        return assess_gate(ev, criteria=getattr(self, "_criteria_results", []),
                           tester_report=tester_report, review=review, failed_tasks=failed_tasks,
                           task_rows=self.store.tasks(self.run_id), scope=self._repair_scope(),
                           visual_findings=getattr(self, "_visual_findings", []), policy=self.cfg.review)

    # ================================================================ delivery
    async def _deliver(self) -> None:
        issue = self.issue
        if self.mode == "single":
            self.sm.transition(RunState.COMPLETED, "single-agent run finished (no independent review, so no PR is created)")
            return
        owner = issue.owner or (self.ref.owner if getattr(self, "ref", None) else None)
        name = issue.repo or (self.ref.name if getattr(self, "ref", None) else None)
        if not owner:
            from .services.delivery import deliver_local
            outcome = await deliver_local(self.ws, self.source)
            self.log("pr", outcome.warning or outcome.reason, level="warn" if outcome.warning else "info")
            self.sm.transition(RunState.COMPLETED, outcome.reason)
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
            await self._auto_merge(gh, f"{owner}/{name}", url)
        else:
            self.sm.transition(RunState.COMPLETED, "approved; push declined by user, PR not created")

    def _publication_service(self):
        from .services.publication import PublicationService
        return PublicationService(cfg=self.cfg, store=self.store, run_id=self.run_id,
                                  issue=self.issue, ws=self.ws, llm=self.llm, log=self.log,
                                  update_base=self._update_with_base, push=push_branch,
                                  protected_paths=getattr(self, "protected_paths", []),
                                  approve=getattr(self, "approve", None))

    async def _auto_merge(self, gh: GitHubClient, repo: str, url: str) -> None:
        await self._publication_service()._auto_merge(gh, repo, url)

    async def publish(self, gh: GitHubClient, owner: str, name: str) -> str | None:
        return await self._publication_service().publish(gh, owner, name)

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
            unknown = set(prof) - {"test_command", "agent_test_command", "agent_env", "sandbox_image",
                                   "install_command", "description", "preview", "protected_paths"}
            if unknown:
                self.log("project", f"ignoring unsupported keys in {PROJECT_FILE}: {sorted(unknown)}", level="warn")
        prot = prof.get("protected_paths") or []
        self.protected_paths = [str(p) for p in prot] if isinstance(prot, list) else []
        self.preview = None
        if prof.get("preview") is not None:
            from .visual import PreviewError, parse_preview
            try:
                self.preview = parse_preview(prof["preview"])
                self.log("project", f"screenshots after each test run: {len(self.preview.pages)} page(s) in "
                                    f"{'/'.join(self.preview.themes)} (preview in {PROJECT_FILE})")
            except PreviewError as e:   # a bad preview section only costs the screenshots, not the run
                self.log("project", f"ignoring preview in {PROJECT_FILE}: {e}", level="warn")
        inst = prof.get("install_command")
        if inst is not None:
            if not isinstance(inst, str) or not inst.strip():
                raise RunAborted(RunState.BLOCKED, "install_command must be a non-empty string")
            self.install_command = inst.strip()
        test_cmd = self._override_test_command or prof.get("test_command")
        env = prof.get("agent_env")
        if env is not None:
            if not isinstance(env, dict) or not all(
                    isinstance(k, str) and re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", k)
                    and not re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|^LD_|^DYLD_|^PATH$", k)
                    and isinstance(v, (str, int, float)) for k, v in env.items()):
                raise RunAborted(RunState.BLOCKED, "agent_env must map plain UPPER_CASE names to text values")
            if not self._override_test_command:
                self.cfg.project.agent_env = {k: str(v) for k, v in env.items()}
        if self.cfg.project.agent_env:
            self.sandbox.agent_env = dict(self.cfg.project.agent_env)
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
        skip = self.cfg.project.agent_env.get("PYTEST_ADDOPTS")
        return "\n".join([
            *([f"In your sandbox pytest runs with PYTEST_ADDOPTS={skip!r} (slow tests are skipped there; the "
               "orchestrator still runs everything)."] if skip else []),
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

    # ===================================================== several runs on one repository
    def _peer_note(self) -> str:
        """For the planner: what teammates are changing on this repository right now."""
        if self._same_repo_mode() != "coordinate":
            return ""
        peers = self._active_peers()
        if not peers:
            return ""
        lines = [f"- run {p['id']}: {p.get('issue_title') or '?'}"
                 + (f" — files: {', '.join(sorted(self._plan_files(p))[:15])}" if self._plan_files(p) else "")
                 for p in peers]
        return ("Other work is in progress on this repository on separate branches:\n" + "\n".join(lines)
                + "\nPlan this task so it avoids editing those files where reasonably possible (list the files you "
                  "really need in expected_files); if you must touch them, keep those edits small.")

    def _same_repo_mode(self) -> str:
        lim = self.cfg.limits
        if not lim.one_run_per_repo:
            return "parallel"
        return lim.same_repo

    def _active_peers(self) -> list[dict]:
        key = lock_key(self._repo_arg, self._issue_url_arg) or lock_key(
            (self.store.get_run(self.run_id) or {}).get("repo"))
        return blocking_runs(self.store, self.run_id, key) if key else []

    @staticmethod
    def _plan_files(run: dict) -> set[str]:
        try:
            plan = json.loads(run.get("plan_json") or "{}")
        except json.JSONDecodeError:
            return set()
        return {f.strip().lstrip("./") for t in plan.get("tasks", []) for f in (t.get("expected_files") or [])
                if isinstance(f, str) and f.strip()}

    async def _coordinate(self, stage: str) -> None:
        """Like an engineering manager: before starting, check the work in progress on this repository;
        after planning, make sure this run won't edit the same files as an earlier run still going."""
        peers = self._active_peers()
        if not peers:
            return
        if stage == "start":
            decision = await self._triage(peers)
            ids = [p["id"] for p in peers]
            if decision["decision"] == "duplicate":
                raise RunAborted(RunState.BLOCKED, f"duplicate of run(s) {', '.join(decision['overlaps_with'] or ids)}"
                                                   f": {decision['reason']}")
            if decision["decision"] == "wait":
                wait = [i for i in decision["overlaps_with"] if i in ids] or ids
                await self._wait_runs(wait, decision["reason"])
                await self._refresh_base_after_wait()
            return
        mine = self._plan_files({"plan_json": json.dumps(self.plan.to_dict())})
        clash = {p["id"]: sorted(mine & self._plan_files(p)) for p in peers}
        clash = {k: v for k, v in clash.items() if v}
        if not clash:
            self.log("coordination", f"no file overlap with the {len(peers)} other run(s) on this repository; "
                                     "working in parallel on branch "
                                     + str(getattr(self.ws, "integration_branch", None) or "?"))
            return
        detail = "; ".join(f"{k}: {', '.join(v[:5])}" for k, v in clash.items())
        await self._wait_runs(list(clash), f"the plans edit the same files ({detail})")
        await self._refresh_base_after_wait()

    async def _triage(self, peers: list[dict]) -> dict:
        lines = []
        for p in peers:
            plan = {}
            try:
                plan = json.loads(p.get("plan_json") or "{}")
            except json.JSONDecodeError:
                pass
            tasks = "; ".join(f"{t.get('title')} [{', '.join(t.get('expected_files') or [])}]"
                              for t in plan.get("tasks", []))[:1500]
            lines.append(f"### Run {p['id']} ({p['state']})\nTask: {p.get('issue_title') or '-'}\n"
                         f"{(p.get('issue_body') or '')[:1500]}\n"
                         + (f"Plan: {plan.get('summary', '')[:600]}\nWork items: {tasks}" if plan else "Not planned yet."))
        user = (f"## NEW task\n{self.issue.title}\n{(self.issue.body or '')[:3000]}\n\n"
                "## Work in progress on the same repository\n" + "\n\n".join(lines))
        fallback = {"decision": "wait", "overlaps_with": [p["id"] for p in peers],
                    "reason": "could not get a coordination decision; waiting to be safe"}
        try:
            res = await self.llm.chat("planner", [{"role": "system", "content": prompts.COORDINATOR_SYSTEM},
                                                  {"role": "user", "content": user}], task_key="coordination")
            m = re.search(r"\{.*\}", res.content or "", re.S)
            d = json.loads(m.group(0)) if m else {}
            if d.get("decision") not in ("parallel", "wait", "duplicate"):
                d = fallback
        except (LLMError, json.JSONDecodeError) as e:
            self.log("coordination", f"manager check failed ({e}); waiting to be safe", level="warn")
            d = fallback
        d["overlaps_with"] = [str(x) for x in (d.get("overlaps_with") or [])]
        d["reason"] = str(d.get("reason") or "")[:500]
        self.log("coordination", f"manager: {d['decision']} — {d['reason']}"
                                 + (f" (overlaps with {', '.join(d['overlaps_with'])})" if d["overlaps_with"] else ""),
                 data=d)
        return d

    async def _wait_runs(self, ids: list[str], reason: str) -> None:
        from .launcher import pid_alive
        from .state import TERMINAL
        terminal = {str(s) for s in TERMINAL}
        self.log("coordination", f"waiting for run(s) {', '.join(ids)}: {reason}")
        self.store.update_run(self.run_id, state_reason=f"waiting for the repository: run(s) {', '.join(ids)} "
                                                        f"— {reason}"[:400])
        t0 = time.monotonic()
        while True:
            left = [i for i in ids if (r := self.store.get_run(i)) and r["state"] not in terminal and pid_alive(r["pid"])]
            if not left:
                break
            self._check_cancel()
            await asyncio.sleep(self._repo_poll_s)
        waited = time.monotonic() - t0
        self._started += waited             # waiting doesn't use this run's time limit
        self.store.update_run(self.run_id, state_reason=None)
        self.log("coordination", f"run(s) {', '.join(ids)} finished after {waited / 60:.0f} min; continuing")

    async def _refresh_base_after_wait(self) -> None:
        """After waiting, start from the newest base (a teammate's work may have been merged meanwhile)."""
        if self.ws is None or not self.ws.repo.exists():
            return
        if await asyncio.to_thread(self.ws.changed_files):
            return                           # work already exists; it's updated before the PR instead
        try:
            new = await asyncio.to_thread(self.ws.fetch_base, self._git_auth)
        except GitError as e:
            self.log("coordination", f"could not fetch the latest {self.ws.base_branch}: {e}", level="warn")
            return
        if new != self.ws.base_sha:
            await asyncio.to_thread(git, self.ws.repo, "reset", "--quiet", "--hard", new)
            self.ws.base_sha = new
            self.log("coordination", f"starting from the latest {self.ws.base_branch} ({new[:8]})")

    async def _update_with_base(self, cycle: int) -> list[dict]:
        """Before the merge request: bring the branch up to date with the latest base, resolve conflicts,
        and re-run the tests if anything changed. Returns blocking problems (empty = ready)."""
        if self.ws is None:
            return []
        try:
            new = await asyncio.to_thread(self.ws.fetch_base, self._git_auth)
        except GitError as e:
            self.log("integrate", f"could not fetch the latest {self.ws.base_branch}; opening the PR against the "
                                  f"base it started from: {e}", level="warn")
            return []
        if new == self.ws.base_sha or await asyncio.to_thread(self.ws.is_ancestor, new, self.ws.integration_branch):
            return []
        self.log("integrate", f"{self.ws.base_branch} moved on ({self.ws.base_sha[:8]} -> {new[:8]}) while this run "
                              "worked; updating the branch before the merge request")
        ref = f"refs/remotes/origin/{self.ws.base_branch}"
        ok = await self._merge_with_resolution(ref, f"update-c{cycle}",
                                               f"Update with latest {self.ws.base_branch} ({new[:8]})",
                                               self.cfg.developer_roles[0],
                                               f"Bringing this branch up to date with the latest {self.ws.base_branch}, "
                                               "which now contains other merged work. Keep BOTH this branch's "
                                               "changes and the new base's changes.")
        if not ok:
            return [{"kind": "merge", "summary": f"could not merge the latest {self.ws.base_branch} into this branch",
                     "detail": "Resolve the conflicts between this work and the latest base branch."}]
        self.ws.base_sha = new
        self.store.update_run(self.run_id, base_branch=self.ws.base_branch)
        ev = await run_tests(self.sandbox, self.ws.repo, self.cfg.project.test_command, self.artifacts,
                             f"update{cycle}", self.cfg.sandbox.command_timeout_s)
        self._record_tests(ev, cycle)
        self.log("test", f"tests after updating with {self.ws.base_branch}: {ev.short()}",
                 level="info" if ev.ok else "warn")
        if ev.ok:
            return []
        return [{"kind": "tests", "summary": f"tests fail after updating with the latest {self.ws.base_branch}: "
                                             f"{ev.short()}", "detail": ev.failure_report()}]

    async def _merge_with_resolution(self, branch: str, key: str, message: str, role: str, context: str) -> bool:
        async with self._integration_lock:
            mr = await asyncio.to_thread(self.ws.merge_into_integration, branch, message)
            if mr.ok:
                self.log("integrate", f"merged {branch} cleanly ({mr.sha[:8]})")
                return True
            self.log("integrate", f"merge conflict in {mr.conflicts}; asking an agent to resolve it", level="warn")
            rpath, rbranch, conflicts = await asyncio.to_thread(self.ws.start_conflict_resolution, key, branch)
            await self._ensure_deps(rpath, key)
            user = "\n\n".join([prompts.render_issue(self.issue.title, self.issue.body), context,
                                 "Conflicted files:\n" + "\n".join(f"- {c}" for c in conflicts), self._test_hint()])
            ctx = ToolContext(workspace=rpath, sandbox=self.sandbox, installer=self._tool_installer(),
                              command_timeout_s=self.cfg.sandbox.command_timeout_s, artifacts_dir=self.artifacts / key,
                              test_command=self.cfg.project.test_command, base_sha=self.ws.base_sha)
            res = await self._agent(role, key, prompts.CONFLICT_SYSTEM, user,
                                    READ_TOOLS + WRITE_TOOLS + self._exec_tools(), self._finish_terminal(), ctx)
            if not res.ok:
                return False
            ok, bad = await asyncio.to_thread(self.ws.finish_conflict_resolution, rpath, message + " (conflicts resolved)")
            if not ok:
                self.log("integrate", f"conflict markers remain in {bad}", level="error")
                return False
            mr2 = await asyncio.to_thread(self.ws.merge_into_integration, rbranch, message)
            await asyncio.to_thread(self.ws.remove_worktree, rpath)
            return mr2.ok

    def _exec_tools(self) -> list[str]:
        return EXEC_TOOLS + (DEV_TOOLS + NAV_TOOLS if self.cfg.sandbox.dev_tools else [])

    def _review_tools(self) -> list[str]:
        """Read-only tools for the reviewer: files plus outline/find_symbol/git_changes/git_history."""
        return READ_TOOLS + NAV_READONLY

    def _plan_tools(self) -> list[str]:
        """Read-only tools for the planner: files plus outline/find_symbol/git_changes/git_history."""
        return READ_TOOLS + NAV_READONLY

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
            from .agent.harness import FunctionHarness
            harness = FunctionHarness((lambda spec: self._run_pi(spec, rc)) if use_pi
                                      else (lambda spec: run_agent(self.llm, spec)))
            res = await harness.run(spec)
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
    from .effective_config import effective_config
    # Keep the historical test-command field for existing metrics/report readers.
    return {**effective_config(cfg), "test_command": cfg.project.test_command}
