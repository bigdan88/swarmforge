"""Typed, closed lead vocabulary. Model output describes work; it never grants authority."""
from __future__ import annotations

import math
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from .spec import ProjectError, fixed


class ActionKind(StrEnum):
    PLAN = "plan"
    CONTINUE = "continue_plan"
    INSPECT = "inspect"
    INVESTIGATE = "investigate"
    REVIEW = "request_review"
    BROWSER = "request_browser_verification"
    EVALUATE = "evaluate_milestone"
    PAUSE = "pause"
    QUESTION = "ask_user"
    ESCALATE = "escalate"


def text(value: Any, label: str, *, maximum: int = 8000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ProjectError(f"{label} must be nonempty text (at most {maximum} characters)")
    return value


def strings(value: Any, label: str, maximum: int = 12) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ProjectError(f"{label} must be a bounded list")
    result = tuple(text(v, label) for v in value)
    if len(result) != len(set(result)):
        raise ProjectError(f"{label} must not contain duplicates")
    return result


def path(value: Any) -> str:
    value = text(value, "Repository path", maximum=300).replace("\\", "/")
    parts = PurePosixPath(value).parts
    if value.startswith("/") or ":" in value or any(p in ("..", ".git", ".aws") for p in parts):
        raise ProjectError("Repository paths must stay inside the approved repository")
    if any(p.lower() in (".env", "credentials", "secrets.json", "secrets.yaml") for p in parts):
        raise ProjectError("Credential files cannot be requested as lead evidence")
    return value


@dataclass(frozen=True)
class TaskProposal:
    key: str
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]
    files: tuple[str, ...]
    ready: bool
    kind: str
    budget_usd: float
    depends_on: tuple[str, ...] = ()
    replaces: tuple[int, ...] = ()
    max_runtime_s: int | None = None
    check_ids: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: Any) -> TaskProposal:
        allowed = set(cls.__dataclass_fields__)
        if not isinstance(value, dict) or set(value) - allowed:
            raise ProjectError("Task proposal contains unknown fields or permission changes")
        if not isinstance(value.get("ready"), bool) or value.get("kind") not in ("build", "spike"):
            raise ProjectError("Task requires boolean readiness and build/spike kind")
        budget = value.get("budget_usd")
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget <= 0:
            raise ProjectError("Task spending allowance must be finite and positive")
        replaces = value.get("replaces", [])
        if not isinstance(replaces, list) or len(replaces) > 12 or any(type(i) is not int or i <= 0 for i in replaces):
            raise ProjectError("Replacement targets must be queue task IDs")
        runtime = value.get("max_runtime_s")
        if runtime is not None and (type(runtime) is not int or runtime <= 0):
            raise ProjectError("Task runtime must be positive whole seconds")
        key = text(value.get("key"), "Task key", maximum=120)
        if value["kind"] == "spike":
            key = "spike:" + key.removeprefix("spike:")
        return cls(key, text(value.get("title"), "Task title", maximum=300),
                   text(value.get("description"), "Task description"),
                   strings(value.get("acceptance_criteria", []), "Acceptance criteria"),
                   tuple(path(p) for p in strings(value.get("files", []), "Expected files", 40)),
                   value["ready"], value["kind"], float(budget),
                   strings(value.get("depends_on", []), "Dependencies"), tuple(replaces), runtime,
                   strings(value.get("check_ids", []), "Requirement IDs", 30))

    def payload(self) -> dict:
        return {k: list(v) if isinstance(v, tuple) else v for k, v in asdict(self).items()}


@dataclass(frozen=True)
class LeadAction:
    kind: ActionKind
    reason: str
    evidence: tuple[str, ...]
    living: str | None = None
    tasks: tuple[TaskProposal, ...] = ()
    paths: tuple[str, ...] = ()
    run_ids: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    question: str | None = None
    proposal: str | None = None
    attempts: tuple[tuple[int, str], ...] = ()
    task_id: int | None = None

    @classmethod
    def parse(cls, value: Any, default_evidence: str) -> LeadAction:
        if not isinstance(value, dict):
            raise ProjectError("Lead must return one action object")
        raw = value.get("action")
        try:
            kind = ActionKind.EVALUATE if raw == "checkpoint" else ActionKind(raw)
        except (ValueError, TypeError):
            raise ProjectError("Unknown lead action; arbitrary commands and permission changes are forbidden") from None
        common = {"action", "reason", "evidence", "living"}
        extras = {ActionKind.PLAN: {"tasks"}, ActionKind.CONTINUE: {"task_id"}, ActionKind.INSPECT: {"paths", "run_ids"},
                  ActionKind.INVESTIGATE: {"task"}, ActionKind.REVIEW: {"task"},
                  ActionKind.BROWSER: {"checks"}, ActionKind.EVALUATE: {"tasks"},
                  ActionKind.PAUSE: {"question"}, ActionKind.QUESTION: {"question"},
                  ActionKind.ESCALATE: {"attempts", "proposal", "tasks"}}
        unexpected = set(value) - common - extras[kind]
        if unexpected:
            raise ProjectError("Lead action contains fields outside its typed contract: " + ", ".join(sorted(unexpected)) +
                               ". Allowed fields for " + kind.value + ": " + ", ".join(sorted(common | extras[kind])))
        legacy = raw in ("plan", "checkpoint", "escalate") and "evidence" not in value
        evidence = strings(value.get("evidence", [default_evidence] if legacy else []), "Evidence references", 20)
        if not evidence:
            raise ProjectError("Lead action must cite supplied evidence references")
        living = value.get("living")
        if living is not None:
            living = text(living, "Living specification", maximum=60000)
        tasks: tuple[TaskProposal, ...] = ()
        task_id = value.get("task_id")
        if kind == ActionKind.CONTINUE and (type(task_id) is not int or task_id <= 0):
            raise ProjectError("continue_plan requires the integer task_id of the next queued task; it changes no task fields")
        if kind == ActionKind.PLAN:
            raw_tasks = value.get("tasks")
            if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= 12:
                raise ProjectError("Plan must contain 1–12 task proposals")
            tasks = tuple(TaskProposal.parse(t) for t in raw_tasks)
        elif kind in (ActionKind.INVESTIGATE, ActionKind.REVIEW):
            if not isinstance(value.get("task"), dict):
                raise ProjectError("task must be a complete task object, not a key string. Use continue_plan with task_id to run an existing queued task")
            tasks = (TaskProposal.parse(value.get("task")),)
            expected = "spike" if kind == ActionKind.INVESTIGATE else "build"
            if tasks[0].kind != expected or not tasks[0].ready:
                raise ProjectError(f"{kind} requires one ready {expected} task")
        elif not legacy and value.get("tasks"):
            raise ProjectError("This action cannot dispatch implementation tasks")
        paths = tuple(path(p) for p in strings(value.get("paths", []), "Inspection paths", 8))
        runs = strings(value.get("run_ids", []), "Inspection runs", 4)
        checks = strings(value.get("checks", []), "Browser checks", 5)
        if kind == ActionKind.INSPECT and not (paths or runs):
            raise ProjectError("Inspection requires repository paths or project run IDs")
        if kind == ActionKind.BROWSER and not checks:
            raise ProjectError("Browser verification requires approved acceptance checks")
        question = value.get("question")
        if kind == ActionKind.QUESTION or question is not None:
            question = text(question, "Concrete question", maximum=3000)
        attempts = []
        if kind == ActionKind.ESCALATE:
            raw_attempts = value.get("attempts", [])
            if not isinstance(raw_attempts, list) or len(raw_attempts) > 8:
                raise ProjectError("Escalation attempts must be bounded")
            for a in raw_attempts:
                if not isinstance(a, dict) or set(a) != {"step", "result"} or type(a["step"]) is not int or a["step"] not in (1, 2, 3, 4):
                    raise ProjectError("Escalation attempts require a known ladder step and result")
                attempts.append((a["step"], text(a["result"], "Attempt result")))
            if len({a[0] for a in attempts}) < 2:
                raise ProjectError("Escalation rejected: try at least two distinct ladder steps")
        return cls(kind, text(value.get("reason"), "Decision reason"), evidence, living, tasks, paths, runs, checks,
                   question, text(value.get("proposal"), "Concrete proposal") if kind == ActionKind.ESCALATE else None,
                   tuple(attempts), task_id)

    def legacy(self, spec: str) -> dict:
        action = "plan" if self.tasks else "checkpoint" if self.kind == ActionKind.EVALUATE else "escalate"
        return {"action": action, "reason": self.reason,
                "living": self.living or spec[len(fixed(spec)) + len("## Living\n"):],
                "tasks": [t.payload() for t in self.tasks], "proposal": self.proposal,
                "attempts": [{"step": n, "result": result} for n, result in self.attempts]}


def validate_scope(action: LeadAction, project: dict, items: list[dict], evidence: dict,
                   *, max_runtime_s: int, investigation_timeout_s: int) -> None:
    if project["status"] != "running" or not project.get("approved_at"):
        raise ProjectError("Lead actions require explicit human launch approval")
    if set(action.evidence) - set(evidence):
        raise ProjectError("Decision cites unknown or out-of-project evidence")
    if any("run:" + rid not in evidence for rid in action.run_ids):
        raise ProjectError("Inspection run belongs to another project or is unavailable")
    approved_checks = project["definition"]["milestones"][project["current_milestone"]]["checks"]
    if set(action.checks) - set(approved_checks):
        raise ProjectError("Browser checks must come from the approved current milestone")
    targets = {q["id"]: q for q in items}
    if action.kind == ActionKind.CONTINUE:
        queued = [q for q in items if q["status"] == "queued" and q["milestone"] == project["current_milestone"]]
        target = targets.get(action.task_id)
        if project.get("previous_spec"):
            raise ProjectError("An amended specification must be replanned before continuing old tasks")
        if not queued or target is None or target["id"] != queued[0]["id"] or target.get("run_id") or target.get("project_processed"):
            raise ProjectError("continue_plan can select only the next undispatched queued task in the current milestone")
        if target.get("project_id") != project["id"] or target.get("queue") != "project-" + project["id"]:
            raise ProjectError("The queued task must belong to this project's own lane")
        available = {q["project_key"] for q in items if q["milestone"] == project["current_milestone"] and
                     (q.get("merged_sha") or q["status"] == "done" and q["mode"] == "single" and q["project_processed"])}
        if set(json.loads(target.get("depends_on") or "[]")) - available:
            raise ProjectError("The next queued task still has unfinished prerequisites")
    available = {q["project_key"] for q in items if q["milestone"] == project["current_milestone"] and
                 (q.get("merged_sha") or q["status"] == "done" and q["mode"] == "single" and q["project_processed"])}
    available -= {task.key for task in action.tasks}
    planned = {task.key: task for task in action.tasks}
    if len(planned) != len(action.tasks):
        raise ProjectError("Task keys must be unique")
    # Provisional work describes a future graph; it is never eligible for dispatch.
    # Validate the whole graph without pretending those prerequisites are complete.
    visiting, visited = set(), set()
    def visit(key):
        if key in visiting:
            raise ProjectError("Task dependencies contain a cycle")
        if key in visited:
            return
        visiting.add(key)
        for dependency in planned[key].depends_on:
            if dependency in planned:
                visit(dependency)
            elif dependency not in available:
                raise ProjectError(f"Dependencies include an unknown or unfinished task: {dependency}")
        visiting.remove(key)
        visited.add(key)
    for key in planned:
        visit(key)
    seen: set[str] = set()
    from ..verification import project_requirements
    requirement_ids = {r["id"] for r in project_requirements(project["definition"], project["current_milestone"])}
    for task in action.tasks:
        if set(task.check_ids) - requirement_ids:
            raise ProjectError("Task check_ids must name approved current or earlier requirements")
        if task.key in seen:
            raise ProjectError("Task keys must be unique")
        if task.ready and set(task.depends_on) - available:
            raise ProjectError("Dependencies must be completed evidence or earlier eligible tasks in this plan")
        for target in task.replaces:
            q = targets.get(target)
            if not q or q["milestone"] != project["current_milestone"] or q["status"] not in ("queued", "tentative", "failed"):
                raise ProjectError("Replacement targets must be eligible tasks in this project and milestone; completed evidence is immutable")
        ceiling = investigation_timeout_s if task.kind == "spike" else max_runtime_s
        if task.max_runtime_s is not None and task.max_runtime_s > ceiling:
            raise ProjectError("Requested execution time exceeds the configured task allowance")
        seen.add(task.key)
        if task.ready:
            available.add(task.key)


def existing_task_reference(value, project, items):
    """Adapt the older reference-shaped investigation to an unchanged queued task.

    Only known descriptive fields are accepted. A different allowance, deadline,
    scope, permission or ambiguous target must go through normal model repair.
    The original response remains in the decision journal.
    """
    if not isinstance(value, dict) or value.get("action") not in ("investigate", "request_review") or not isinstance(value.get("task"), str):
        return value, None
    allowed = {"action", "reason", "evidence", "living", "task", "hypothesis", "observable_result", "budget_usd", "max_runtime_s"}
    if set(value) - allowed:
        return value, None
    key = value["task"].removeprefix("spike:")
    matches = [q for q in items if q["status"] == "queued" and q["milestone"] == project["current_milestone"] and
               q["project_key"].removeprefix("spike:") == key]
    if len(matches) != 1:
        return value, None
    task = matches[0]
    if (value["action"] == "investigate") != (task["mode"] == "single" and task["project_key"].startswith("spike:")):
        return value, None
    if value["action"] == "request_review" and task["mode"] not in ("swarm", "reviewed"):
        return value, None
    if any(field in value and (type(value[field]) not in (int, float) or value[field] != task.get(column))
           for field, column in (("budget_usd", "budget"), ("max_runtime_s", "max_runtime_s"))):
        return value, None
    return {"action": "continue_plan", "reason": value.get("reason"), "evidence": value.get("evidence"),
            "task_id": task["id"]}, f"Selected existing task #{task['id']} without changing its scope, allowance or the remaining plan."
