"""Explicit human dashboard actions with durable duplicate/uncertain-outcome guards."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from typing import Any

from ..config import Config
from ..db import Store
from ..projects.spec import ProjectError, validate


class ActionConflict(ValueError):
    pass


def text(args: dict[str, Any], key: str, maximum: int, default: str = "") -> str:
    value = args.get(key, default)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"Provide {key} as text within {maximum} characters")
    return value.strip()


class DashboardActions:
    def __init__(self, store: Store, config: Config | None = None, *, chat_answer=None, project_interview=None, worker_start=None,
                 chat_background: bool = True, diagnose=None, pr_api=None, model_test=None):
        from .chat_requests import release_interrupted
        self.store, self.config = store, config
        self.chat_answer, self.project_interview, self.worker_start = chat_answer, project_interview, worker_start
        self.chat_background, self.diagnose, self.model_test = chat_background, diagnose, model_test
        from .pull_requests import PullRequests
        self.pull_requests = PullRequests(store, config, pr_api)
        release_interrupted(store)
        store._exec("""CREATE TABLE IF NOT EXISTS dashboard_requests (
            key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, operation TEXT NOT NULL,
            target TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, result TEXT)""")

    def execute(self, key: str, operation: str, target: str, args: dict[str, Any]) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", key):
            raise ValueError("A valid Idempotency-Key is required")
        fingerprint = hashlib.sha256(json.dumps([operation, target, args], sort_keys=True, allow_nan=False).encode()).hexdigest()
        with self.store._lock, self.store.conn:
            self.store.conn.execute("BEGIN IMMEDIATE")
            old = self.store.conn.execute("SELECT * FROM dashboard_requests WHERE key=?", (key,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise ActionConflict("This request key belongs to a different action")
                if old["result"]:
                    return json.loads(old["result"])
                raise ActionConflict("This action is running or its outcome is uncertain. Inspect current state; do not repeat it.")
            self.store.conn.execute("INSERT INTO dashboard_requests(key,fingerprint,operation,target,status,created_at) VALUES (?,?,?,?,'pending',?)",
                                    (key, fingerprint, operation, target, time.time()))
        try:
            result = {"ok": True, "message": "Action recorded", **self._apply(operation, target, args)}
        except (ValueError, ProjectError) as exc:
            result = {"ok": False, "message": str(exc)[:500]}
        except Exception:
            # An external action may already have happened. Retain pending intent
            # and never replay it automatically after a transport/process failure.
            raise ActionConflict("Action outcome is uncertain. Inspect current state before taking another action.") from None
        self.store._exec("UPDATE dashboard_requests SET status=?,result=? WHERE key=? AND status='pending'",
                         ("done" if result["ok"] else "rejected", json.dumps(result), key))
        return result

    def _apply(self, operation, target, args):
        from ..projects import service
        from ..projects.store import Projects
        from .runs import request_cancel
        store = self.store
        ps = Projects(store)
        if operation == "cancel_work":
            if target.startswith("task-") and target[5:].isdigit():
                item = store.queue_item(int(target[5:]))
                if not item:
                    raise ValueError("Task not found")
                if item["status"] in ("queued", "tentative"):
                    store.queue_update(item["id"], status="cancelled", finished_at=time.time())
                elif item.get("run_id"):
                    request_cancel(store, item["run_id"])
            elif target.startswith("run-"):
                request_cancel(store, target[4:])
            else:
                raise ValueError("Work item not found")
            return {"message": "Cancellation requested"}
        if operation == "retry_work":
            from ..runqueue import requeue
            if args.get("spending_authorized") is not True:
                raise ValueError("Explicit retry spending authorization is required")
            if not target.startswith("task-") or not target[5:].isdigit():
                raise ValueError("Only retained queue tasks can be retried here")
            item = store.queue_item(int(target[5:]))
            if not item or item["status"] not in ("done", "failed", "cancelled", "skipped"):
                raise ValueError("Only finished tasks can be retried")
            if item.get("project_id"):
                raise ValueError("Project retries must go through the project lead so accounting and merge barriers are preserved")
            if self.config is None:
                raise ValueError("Load configuration before retrying work")
            if item.get("run_id"):
                try:
                    from ..execution import approve_retry
                except ImportError:
                    raise ValueError("Durable task allowance support is required for retries in this interface") from None
                approve_retry(store, self.config, item["run_id"])
            fields = {k: item[k] for k in ("budget", "mode", "max_cycles", "max_runtime_s", "approval", "chain", "heal_attempts", "team") if item.get(k) is not None}
            fields["approval"] = "dashboard"
            item_id, _ = requeue(store, item, **fields)
            return {"message": "Retry queued within the existing task allowance", "item_id": f"task-{item_id}"}
        if operation == "decide_approval":
            if not target.isdigit():
                raise ValueError("Approval not found")
            row = store.approval(int(target))
            if not row or row["status"] != "pending":
                raise ValueError("Approval is no longer pending")
            run = store.get_run(row["run_id"])
            if not run or run["state"] in {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "BUDGET_EXCEEDED"}:
                raise ValueError("This run is no longer awaiting approval")
            if run.get("cancel_requested"):
                raise ValueError("This run has been cancelled")
            if run.get("execution_id"):
                allowance = store._one("SELECT deadline,cancelled FROM execution_allowances WHERE id=?", (run["execution_id"],))
                if allowance and (allowance["cancelled"] or allowance["deadline"] <= time.time()):
                    raise ValueError("The shared task allowance is no longer active")
            if not isinstance(args.get("approved"), bool):
                raise ValueError("Choose approve or decline")
            if args["approved"] and (len(row["question"]) > 4000 or "\0" in row["question"]):
                raise ValueError("Review the complete approval question in the existing dashboard before approving")
            store.decide_approval(int(target), args["approved"], by="react-dashboard")
            return {"message": "Approval decision recorded"}
        if operation == "queue_task":
            if self.config is None:
                raise ValueError("Load configuration before adding tasks")
            from ..repos import normalize_repo
            fields: dict[str, Any] = {}
            issue_url = args.get("issue_url")
            if issue_url:
                from ..github import parse_issue_url
                if not isinstance(issue_url, str) or len(issue_url) > 500:
                    raise ValueError("Paste a GitHub issue URL")
                try:
                    owner, name, number = parse_issue_url(issue_url.strip())
                except Exception:
                    raise ValueError("That is not a GitHub issue URL (https://github.com/owner/repo/issues/N)") from None
                fields["issue_url"] = issue_url.strip()
                repo = (args.get("repo") or "").strip() or f"https://github.com/{owner}/{name}"
                title = (args.get("title") or "").strip()[:300] or f"{owner}/{name} issue #{number}"
                body = (args.get("body") or "").strip()[:30000] or f"Work on {issue_url.strip()}"
            else:
                title, body, repo = text(args, "title", 300), text(args, "body", 30000), text(args, "repo", 4096)
            normalize_repo(repo)  # Parsing only; no network/repository creation.
            budget = args.get("budget")
            if isinstance(budget, bool) or not isinstance(budget, (float, int)) or not 0.5 <= budget <= 500:
                raise ValueError("Choose a task budget from $0.50 to $500")
            if args.get("spending_authorized") is not True:
                raise ValueError("Explicit spending authorization is required")
            mode = args.get("mode", "swarm")
            if mode not in ("swarm", "reviewed", "single", "auto"):
                raise ValueError("Unknown workflow")
            def whole(key, low, high):
                value = args.get(key)
                if value is None or value == "":
                    return None
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) or not low <= value <= high:
                    raise ValueError(f"{key.replace('_', ' ')} must be a whole number from {low} to {high}")
                return int(value)
            if (cycles := whole("max_cycles", 0, 10)) is not None:
                fields["max_cycles"] = cycles
            if (heal := whole("heal_attempts", 0, 5)) is not None:
                fields["heal_attempts"] = heal
            hours = args.get("hours")
            if hours not in (None, ""):
                if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0.05 <= hours <= 72:
                    raise ValueError("Choose a time limit from 0.05 to 72 hours")
                fields["max_runtime_s"] = int(hours * 3600)
            else:
                fields["max_runtime_s"] = self.config.limits.max_runtime_s
            team = args.get("team") or None
            if team is not None:
                from ..config import ConfigError
                from ..teams import apply_team, list_teams
                if not isinstance(team, str) or team not in list_teams(self.config):
                    raise ValueError("Choose a listed model team")
                try:
                    apply_team(self.config, team)
                except ConfigError as exc:
                    raise ValueError(f"Model team {team}: {exc}") from None
                fields["team"] = team
            item_id = store.queue_add(title, body, repo, budget=budget, mode=mode, approval="dashboard", chain=0, **fields)
            return {"message": "Task added to the queue; publication still requires approval", "item_id": f"task-{item_id}"}
        if operation == "start_queue":
            if not self.config or not self.config.source_path or args.get("spending_authorized") is not True:
                raise ValueError("A configured worker and explicit authorization are required")
            from ..runqueue import start_worker
            (self.worker_start or start_worker)(self.config, store)
            return {"message": "Queue worker start requested"}
        if operation == "pause_queue":
            from ..runqueue import PAUSED_KEY
            store.kv_set(PAUSED_KEY, "1")
            return {"message": "Queue paused; running work can finish"}
        if operation == "create_project":
            definition = validate(args.get("definition"))
            from ..projects.spec import render
            render(definition)  # Reject malformed Fixed/Living headings before creating anything.
            p = ps.create(definition["name"])
            ps.draft(p["id"], definition)
            return {"project_id": p["id"], "message": "Project draft saved. Nothing has launched."}
        if operation == "save_project":
            p = ps.get(target)
            if args.get("expected_spec") != hashlib.sha256(p["spec"].encode()).hexdigest():
                raise ValueError("The spec changed. Reload and review before saving.")
            ps.draft(target, validate(args.get("definition")), expected_spec=args.get("expected_spec"))
            return {"project_id": target, "message": "Draft saved. Nothing has launched."}
        if operation in ("launch_project", "resume_project", "pause_project", "cancel_project"):
            if operation in ("launch_project", "resume_project") and args.get("spending_authorized") is not True:
                raise ValueError("Explicit project authorization is required")
            if operation in ("launch_project", "resume_project") and self.config is None:
                raise ValueError("Load configuration before launching or resuming a project")
            if operation == "launch_project":
                project = ps.get(target)
                if len(project["spec"]) > 256000 or len(json.dumps(project["definition"])) > 64000:
                    raise ValueError("This specification exceeds the web review limit; review and approve it in the existing dashboard")
                service.approve(store, target, expected_spec=args.get("expected_spec", ""))
            elif operation == "resume_project":
                service.resume(store, target, text(args, "reply", 16000, "continue"), confirm_manual=args.get("confirm_manual") is True)
            elif operation == "pause_project":
                service.pause(store, target)
            else:
                service.cancel(store, target)
            return {"project_id": target, "message": "Project action recorded; existing launch, merge and spending rules apply"}
        if operation == "create_conversation":
            cid = uuid.uuid4().hex
            store.conversation_create(cid, text(args, "title", 200, "New chat"))
            return {"conversation_id": cid, "message": "Conversation created"}
        if operation == "project_message":
            if not self.config or args.get("spending_authorized") is not True:
                raise ValueError("Configured model and explicit interview spending authorization are required")
            p = ps.get(target)
            expected = args.get("expected_spec")
            if p["status"] != "drafting" or expected != hashlib.sha256(p["spec"].encode()).hexdigest():
                raise ValueError("Reload the current draft before continuing its interview")
            question = text(args, "message", 16000)
            from ..projects.lead import interview
            asyncio.run((self.project_interview or interview)(self.config, store, target, question, expected_spec=expected))
            return {"project_id": target, "message": "Project reply recorded. Review any updated spec before launch."}
        if operation == "send_message":
            if not self.config or not store.conversation_get(target) or target.startswith("project-"):
                raise ValueError("Choose a general conversation with model configuration loaded")
            if args.get("spending_authorized") is not True:
                raise ValueError("Sending uses the configured model; explicit authorization is required")
            question = text(args, "message", 16000)
            from .. import chatbot
            from .chat_requests import start_reply
            start_reply(store, self.config, self.chat_answer or chatbot.answer, target, question,
                        args.get("attachments"), background=self.chat_background)
            return {"conversation_id": target, "message": "Message sent. The reply appears here as it arrives."}
        if operation == "set_archived":
            if not isinstance(args.get("archived"), bool):
                raise ValueError("Choose archive or unarchive")
            run_id = None
            if target.startswith("run-"):
                run_id = target[4:]
            elif target.startswith("task-") and target[5:].isdigit():
                item = store.queue_item(int(target[5:]))
                run_id = item and item.get("run_id")
            run = store.get_run(run_id) if run_id else None
            if not run:
                raise ValueError("Only tasks that have started a run can be archived")
            if args["archived"] and run["finished_at"] is None and run["state"] not in ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "BUDGET_EXCEEDED"):
                raise ValueError("Running work cannot be archived; cancel it or wait for it to finish")
            store.set_archived(run["id"], args["archived"])
            return {"message": "Archived; history, report and transcripts are kept" if args["archived"] else "Shown in Work again"}
        if operation == "save_settings":
            return self._save_settings(args)
        if operation == "test_models":
            if not self.config or args.get("spending_authorized") is not True:
                raise ValueError("Testing sends one small request to each role's model; authorize it explicitly")
            roles = args.get("roles") or None
            if roles is not None and (not isinstance(roles, list) or not all(r in self.config.roles for r in roles)):
                raise ValueError("Choose configured roles")
            from .. import settings
            rows = (self.model_test or settings.test_models)(self.config, roles)
            details = [{"role": str(r.get("role")), "model": str(r.get("model")), "ok": bool(r.get("ok")),
                        "detail": str(r.get("detail", ""))[:500], "cost_usd": r.get("cost_usd")} for r in rows]
            ok = sum(d["ok"] for d in details)
            return {"message": f"{ok} of {len(details)} models answered with a tool call", "details": details}
        if operation == "merge_pr":
            return self.pull_requests.merge(target, args)
        if operation == "update_pr":
            return self.pull_requests.update(target, args)
        if operation == "mark_features_read":
            from ..history import entries
            store.kv_set("feature_history_seen", ",".join(e["id"] for e in entries()))
            return {"message": "All highlights marked as read"}
        if operation in ("create_note", "save_note", "delete_note"):
            return self._note_action(operation, target, args)
        if operation == "diagnose_failure":
            if not self.config or args.get("spending_authorized") is not True:
                raise ValueError("Diagnosis may call the planner model; load configuration and authorize it explicitly")
            run = store.get_run(target)
            if not run or run["state"] not in ("FAILED", "BLOCKED", "BUDGET_EXCEEDED", "CANCELLED"):
                raise ValueError("Only finished, unsuccessful runs can be diagnosed")
            from ..postmortem import diagnose
            result = asyncio.run((self.diagnose or diagnose)(self.config, store, target))
            if result is None:
                raise ValueError("No diagnosis was produced; the run's retained evidence may be missing")
            return {"message": "Diagnosis recorded"}
        raise ValueError("Unknown action")

    def _save_settings(self, args):
        """Validated role/limit edits written with a backup, exactly as the Streamlit Settings page does."""
        from pathlib import Path

        from .. import settings
        cfg = self.config
        if cfg is None or cfg.source_path is None:
            raise ValueError("This configuration has no source file to save")
        path = Path(cfg.source_path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != args.get("expected_config_sha"):
            raise ValueError("swarmforge.yaml changed since this page loaded. Reload before saving.")
        roles, limits = args.get("roles"), args.get("limits")
        if not isinstance(roles, dict) or not isinstance(limits, dict):
            raise ValueError("Send role and limit values")
        model_edits = {}
        for role, fields in roles.items():
            if role not in cfg.roles or not isinstance(fields, dict):
                raise ValueError(f"Unknown role {role}")
            model, temp, steps = fields.get("model"), fields.get("temperature"), fields.get("max_steps")
            if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(c in model for c in "\n\r\0"):
                raise ValueError(f"Give {role} a model name")
            if isinstance(temp, bool) or not isinstance(temp, (int, float)) or not 0 <= temp <= 2:
                raise ValueError(f"{role}: temperature must be between 0 and 2")
            if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 500:
                raise ValueError(f"{role}: max steps must be between 1 and 500")
            model_edits[role] = {"model": model.strip(), "temperature": float(temp), "max_steps": steps}
        bounds = {"budget_usd": (0.1, 10000, float), "max_runtime_s": (60, 7 * 24 * 3600, int),
                  "max_concurrent_agents": (1, 8, int), "max_repair_cycles": (0, 10, int)}
        limits_edits = {}
        for key, value in limits.items():
            if key not in bounds:
                raise ValueError(f"Unknown limit {key}")
            low, high, kind = bounds[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high or (kind is int and value != int(value)):
                raise ValueError(f"{key.replace('_', ' ')} must be between {low} and {high}")
            limits_edits[key] = kind(value)
        updated = settings.apply_edits(settings.read_raw(path), model_edits, limits_edits)
        errors = settings.validate_for_save(updated, path)
        if errors:
            raise ValueError("; ".join(errors)[:500])
        settings.save_config_with_backup(path, updated)
        return {"message": f"Saved {path.name} (backup: {path.name}.bak). Restart the web interface to use the new values."}

    def _note_action(self, operation, target, args):
        from pathlib import Path

        from ..knowledge import Vault, parse_note
        from ..webapp.notes import EDITABLE_TYPES, NotesReader
        root = getattr(getattr(self.config, "knowledge", None), "resolved_vault", None) if self.config else None
        if not root:
            raise ValueError("Load configuration with a knowledge vault before editing notes")
        reader, vault = NotesReader(Path(root)), Vault(Path(root))
        tags = args.get("tags", [])
        if not isinstance(tags, list) or len(tags) > 30 or not all(isinstance(t, str) and 0 < len(t.strip()) <= 60 for t in tags):
            raise ValueError("Give at most 30 short tags")
        kind = args.get("type", "user-note")
        if kind not in EDITABLE_TYPES:
            raise ValueError("Choose an editable note type")
        repo = args.get("repo") or None
        if repo is not None and (not isinstance(repo, str) or len(repo) > 300 or "\n" in repo):
            raise ValueError("Give the repository as a short name or URL")
        if operation == "create_note":
            note = vault.write(text(args, "title", 300), text(args, "body", 60000), type=kind,
                               tags=[t.strip() for t in tags], repo=repo.strip() if repo else None, source="user")
            from ..webapp.notes import note_id
            return {"note_id": note_id((f"{note.stem}.md",)), "message": "Note created"}
        parts = reader.locate(target)
        if parts is None:
            raise ValueError("Note not found. Reload the list.")
        path = reader.files._checked_parts(parts)
        current = path.read_bytes()
        if hashlib.sha256(current).hexdigest() != args.get("expected_sha"):
            raise ValueError("This note changed since you opened it (perhaps in your vault editor). Reload before saving.")
        note = parse_note(path)
        if note.type not in EDITABLE_TYPES:
            raise ValueError("Generated notes of this type are maintained by SwarmForge and cannot be edited here")
        if operation == "delete_note":
            if args.get("confirm") is not True:
                raise ValueError("Confirm deletion first")
            path.unlink()
            return {"message": "Note deleted"}
        note.title, note.type, note.body = text(args, "title", 300), kind, text(args, "body", 60000)
        note.tags, note.repo = [t.strip() for t in tags], repo.strip() if repo else None
        note.source = note.source if note.source.startswith("user") else f"{note.source} (edited by user)"
        vault.save(note)
        return {"message": "Note saved"}
