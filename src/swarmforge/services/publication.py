"""GitHub publication and auto-fix operations, independent of lifecycle transitions."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .. import prompts
from ..config import Config
from ..db import Store
from ..github import GitHubClient, GitHubError, Issue, pr_number_from_url
from ..llm import LLMClient, LLMError
from ..report import build_pr_body
from ..workspace import GitError, GitWorkspace


@dataclass
class PublicationService:
    cfg: Config
    store: Store
    run_id: str
    issue: Issue
    ws: GitWorkspace
    llm: LLMClient
    log: Callable[..., None]
    update_base: Callable[[int], Awaitable[list[dict]]]
    push: Callable
    protected_paths: list[str] = field(default_factory=list)
    approve: Callable[[str], bool] | None = None

    async def _auto_merge(self, gh: GitHubClient, repo: str, url: str) -> None:
        """Merge the PR now if the auto_merge rules (or the judge model) allow it; resolve a conflict with
        the latest base first; if it is still held, queue one fix-up task that replaces it. Never changes
        the run's outcome; every decision is recorded for the Pull requests page."""
        from .. import automerge as am
        amc = self.cfg.auto_merge
        if not am.repo_enabled(self.cfg, repo):
            return
        info: dict = {"repo": repo, "url": url, "title": self.issue.title}
        protected = getattr(self, "protected_paths", [])
        open_findings: list[dict] = []
        try:
            info["number"] = number = int(url.rstrip("/").split("/")[-1])
            await self._close_superseded(gh, repo, url)
            pr = await asyncio.to_thread(am.get_pr, gh, repo, number)
            files = await asyncio.to_thread(am.pr_files, gh, repo, number)
            info["node_id"] = pr.node_id
            lines = pr.additions + pr.deletions
            run = self.store.get_run(self.run_id)
            if run is None:
                raise ValueError(f"run not found: {self.run_id}")
            open_findings = [f for f in self.store.findings(self.run_id) if f["cycle"] == run["repair_cycle"]]
            tests = [t for t in self.store.test_runs(self.run_id) if t["label"] != "baseline"]
            tests_ok = bool(tests) and tests[-1]["exit_code"] == 0
            d = am.rule_decision(run, tests_ok, open_findings, files, lines, protected, amc.max_lines,
                                 amc.judge_max_lines, protected_mode=amc.protected, majors_mode=amc.major_findings)
            if d is None:
                role = amc.judge_role if amc.judge_role in self.cfg.roles else "planner"
                t = tests[-1]
                bar = am.judge_bar(files, protected)
                diff = await asyncio.to_thread(self.ws.diff)
                msgs = am.judge_messages(prompts.MERGE_JUDGE_SYSTEM, self.issue.title, self.issue.body, files, lines,
                                         diff, open_findings,
                                         f"exit {t['exit_code']}: {t['passed']} passed, {t['failed']} failed",
                                         protected)
                res = await self.llm.chat(role, msgs, task_key="merge-judge")
                ok, risk, why = am.parse_judgement(res.content, max_risk=bar)
                d = am.Decision(ok, f"{lines}-line change{' touching core files' if bar == 'low' else ''}; "
                                    f"{self.cfg.roles[role].model.split('/')[-1]} judged risk {risk}"
                                    f"{' (core files need low)' if bar == 'low' and not ok else ''}: {why}",
                                lines, judged=True, risk=risk)
            info.update(lines=d.lines, reason=d.reason, judged=d.judged, risk=d.risk)
            if d.merge:
                if pr.state == "dirty" or pr.mergeable is False:
                    pr = await self._resolve_pr_conflict(gh, repo, pr)
                info["sha"] = await asyncio.to_thread(am.apply, gh, pr, amc.method)
                self.store.record_intervention(self.run_id, am.MERGED, json.dumps(info))
                self.store.update_run(self.run_id, state_reason=f"draft PR created: {url} · auto-merged")
                self.log("merge", f"auto-merged {url}: {d.reason}")
                return
        except (GitHubError, GitError, LLMError, ValueError, KeyError, OSError) as e:
            info["reason"] = f"could not auto-merge: {e}"[:600]
        await self._hold_or_fix(info, open_findings)


    async def _resolve_pr_conflict(self, gh: GitHubClient, repo: str, pr):
        """main moved on and the PR conflicts: merge the latest base in (an agent resolves conflicts),
        re-run the tests, push, and return the refreshed PR. Raises GitHubError if that fails."""
        from .. import automerge as am
        self.log("merge", f"{pr.url} conflicts with the latest {self.ws.base_branch}; resolving before merging")
        try:
            problems = await self.update_base((self.store.get_run(self.run_id) or {}).get("repair_cycle", 0))
        except Exception as e:  # noqa: BLE001 - reported as a hold, never crashes the run's end
            raise GitHubError(f"it conflicts with main and updating the branch failed: {e}") from e
        if problems:
            raise GitHubError("it conflicts with main and resolving it failed: " + problems[0]["summary"])
        owner, name = repo.split("/", 1)
        await asyncio.to_thread(self.push, self.ws.repo, owner, name, self.ws.integration_branch,
                                gh.token, self.ws.base_branch, self.cfg.github.branch_prefix)
        await asyncio.sleep(3)                                  # let GitHub recompute mergeability
        return await asyncio.to_thread(am.get_pr, gh, repo, pr.number)


    async def _close_superseded(self, gh: GitHubClient, repo: str, url: str) -> None:
        """A fix-up task's PR replaces the held PR it continues: close that one."""
        from .. import automerge as am
        _, old = am.fix_depth(self.issue.body)
        if not old or old == url or "/pull/" not in old:
            return
        try:
            await asyncio.to_thread(am.close_pr, gh, repo, int(old.rstrip("/").split("/")[-1]),
                                    f"Superseded by {url} (SwarmForge fix-up).")
            self.store.record_intervention(self.run_id, am.CLOSED, json.dumps({"url": old, "replaced_by": url}))
            self.log("merge", f"closed {old}: replaced by {url}")
        except (GitHubError, ValueError) as e:
            self.log("merge", f"could not close the replaced PR {old}: {e}", level="warn")


    async def _hold_or_fix(self, info: dict, open_findings: list[dict]) -> None:
        """Held: queue one automatic fix-up (continues this run's code, replaces the PR), else wait for you."""
        from .. import automerge as am
        amc = self.cfg.auto_merge
        depth, _ = am.fix_depth(self.issue.body)
        if amc.auto_fix and depth < amc.fix_attempts:
            args = dict(title=self.issue.title, body=self.issue.body, repo=info["repo"], url=info["url"],
                        reason=info.get("reason", ""), findings=[f for f in open_findings if f["severity"] != "info"])
            try:
                info["fix_run"] = am.start_fix(self.cfg, self.run_id, **args)       # starts right away
                self.store.record_intervention(self.run_id, am.FIXING, json.dumps(info))
                self.log("merge", f"not merged ({info.get('reason')}); started fix-up run {info['fix_run']} that "
                                  "continues this work and replaces the PR", level="warn")
                return
            except Exception as e:  # noqa: BLE001 - fall back to the queue
                self.log("merge", f"could not start the fix-up run now ({e}); queueing it", level="warn")
            item = self.store._one("SELECT queue FROM queue_items WHERE run_id = ?", (self.run_id,))
            try:
                info["fix_task"] = am.queue_fix(self.store, self.run_id, queue=(item or {}).get("queue"), **args)
                self.store.record_intervention(self.run_id, am.FIXING, json.dumps(info))
                self.log("merge", f"not merged ({info.get('reason')}); queued fix-up task #{info['fix_task']} that "
                                  "continues this work and replaces the PR", level="warn")
                return
            except Exception as e:  # noqa: BLE001 - never let this change the run outcome
                info["reason"] = f"{info.get('reason')} (could not queue a fix-up: {e})"
        self.store.record_intervention(self.run_id, am.HELD, json.dumps(info))
        self.log("merge", f"waiting for you to merge {info['url']}: {info.get('reason')}", level="warn")


    async def _attach_evidence(self, gh: GitHubClient, owner: str, name: str, url: str,
                               original_body: str | None = None) -> None:
        """Upload the run's dashboard screenshots as PR assets and append them to the draft PR body.
        Best effort: any problem is logged as a warning and never affects the PR itself."""
        run = self.store.get_run(self.run_id) or {}
        try:
            from .. import evidence
        except Exception as e:  # noqa: BLE001 - evidence support is optional
            self.log("evidence", f"screenshot upload skipped: {type(e).__name__}: {e}", level="warn")
            return
        try:
            shots = evidence.read_manifest(Path(run.get("run_dir") or ""))
            if not shots:
                return
            if not gh.token:
                self.log("evidence", "screenshot upload skipped: no GitHub token", level="warn")
                return
            number = pr_number_from_url(url)
            if number is None:
                self.log("evidence", f"cannot parse a PR number from {url}; skipping screenshots",
                         level="warn")
                return
            lines = []
            for s in shots:
                p = Path(s["file"])
                if not p.is_absolute():
                    p = Path(run.get("run_dir") or ".") / p
                if not p.is_file():
                    continue
                try:
                    asset = await asyncio.to_thread(gh.upload_pr_asset, owner, name, number, p)
                except Exception as e:  # noqa: BLE001 - one bad asset must not drop the rest
                    self.log("evidence", f"could not upload {p.name}: {type(e).__name__}: {e}",
                             level="warn")
                    continue
                if not asset:
                    self.log("evidence", f"upload of {p.name} returned no URL; skipping it", level="warn")
                    continue
                lines.append(f"![{s['tab']} — {s['theme']}]({asset})")
            if not lines:
                return
            section = "## 📸 Visual evidence\n\n" + "\n".join(lines)
            # Patch the PR's *live* body so manual edits made after the PR was
            # created are preserved; only fall back to a rebuilt body when the
            # live one cannot be fetched.
            current = None
            getter = getattr(gh, "get_pr_body", None)
            if getter is not None:
                try:
                    current = await asyncio.to_thread(getter, owner, name, number)
                except Exception as e:  # noqa: BLE001 - fall back to the original body
                    self.log("evidence", f"could not fetch the PR body: {type(e).__name__}: {e}",
                             level="warn")
            if not current:
                current = original_body or build_pr_body(self.store, self.run_id)
            body = current.rstrip() + "\n\n" + section
            await asyncio.to_thread(gh.update_pr_body, owner, name, number, body)
            self.log("evidence", f"attached {len(lines)} dashboard screenshot(s) to {url}")
        except Exception as e:  # noqa: BLE001 - never let evidence problems break publication
            self.log("evidence", f"screenshot upload skipped: {type(e).__name__}: {e}", level="warn")

    async def publish(self, gh: GitHubClient, owner: str, name: str) -> str | None:
        issue, ws = self.issue, self.ws
        project_item = self.store._one("SELECT project_id FROM queue_items WHERE run_id=? AND project_id IS NOT NULL", (self.run_id,))
        if project_item:
            from ..projects.spec import fixed
            from ..projects.store import Projects
            p = Projects(self.store).get(project_item["project_id"])
            spec = ws.repo / p["spec_path"]
            if not spec.exists() or fixed(spec.read_bytes().decode("utf-8")) != fixed(p["spec"]):
                raise GitError("Project task changed or removed the approved fixed spec; publication refused")
        if not ws.integration_branch or not ws.base_branch:
            raise GitError("cannot publish a workspace without integration and base branches")
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
        await asyncio.to_thread(self.push, ws.repo, owner, name, ws.integration_branch,
                                gh.token, ws.base_branch, self.cfg.github.branch_prefix)
        self.log("pr", f"pushed {ws.integration_branch} to {full}")
        body = build_pr_body(self.store, self.run_id)
        title = f"[SwarmForge] {issue.title}" + (f" (#{issue.number})" if issue.number else "")
        url = await asyncio.to_thread(gh.create_draft_pr, owner, name, ws.integration_branch,
                                      ws.base_branch, title, body)
        await self._attach_evidence(gh, owner, name, url, original_body=body)
        self.store.update_run(self.run_id, pr_url=url)
        self.log("pr", f"draft pull request: {url}")
        return url
