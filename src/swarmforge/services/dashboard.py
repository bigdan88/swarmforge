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
    def __init__(self, store: Store, config: Config | None = None, *, chat_answer=None, project_interview=None, worker_start=None):
        self.store, self.config = store, config
        self.chat_answer, self.project_interview, self.worker_start = chat_answer, project_interview, worker_start
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
            item_id = store.queue_add(title, body, repo, budget=budget, mode=mode, approval="dashboard", chain=0,
                                      max_runtime_s=self.config.limits.max_runtime_s)
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
            from ..chatbot import answer
            asyncio.run((self.chat_answer or answer)(self.config, store, question, conversation=target))
            return {"conversation_id": target, "message": "Reply recorded"}
        raise ValueError("Unknown action")
