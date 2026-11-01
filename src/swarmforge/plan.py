"""Plan schema, validation, and dependency analysis (deterministic; no LLM involvement)."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")

PLAN_JSON_SCHEMA: dict = {
    "type": "object",
    "required": ["summary", "tasks"],
    "properties": {
        "summary": {"type": "string", "description": "One-paragraph summary of the approach."},
        "acceptance_criteria": {
            "type": "array",
            "description": "Issue-level acceptance criteria that tests must demonstrate.",
            "items": {"type": "object", "required": ["id", "text"],
                      "properties": {"id": {"type": "string"}, "text": {"type": "string"}}},
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "title", "description", "role", "depends_on",
                             "acceptance_criteria", "expected_files"],
                "properties": {
                    "id": {"type": "string", "description": "Short id like T1"},
                    "title": {"type": "string"},
                    "description": {"type": "string",
                                    "description": "Precise implementation instructions incl. interfaces."},
                    "role": {"type": "string", "description": "Developer role to assign."},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    "expected_files": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


@dataclass
class Task:
    id: str
    title: str
    description: str
    role: str
    depends_on: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)


@dataclass
class Criterion:
    id: str
    text: str


@dataclass
class Plan:
    summary: str
    tasks: list[Task]
    acceptance_criteria: list[Criterion] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def task(self, tid: str) -> Task:
        return next(t for t in self.tasks if t.id == tid)

    def waves(self) -> list[list[str]]:
        return topo_waves({t.id: t.depends_on for t in self.tasks})


class PlanValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def topo_waves(deps: dict[str, list[str]]) -> list[list[str]]:
    """Group nodes into waves; every node in wave k depends only on nodes in waves < k.
    Raises ValueError on cycles."""
    remaining = {k: set(v) for k, v in deps.items()}
    done: set[str] = set()
    waves = []
    while remaining:
        ready = sorted(k for k, v in remaining.items() if v <= done)
        if not ready:
            raise ValueError(f"dependency cycle among: {sorted(remaining)}")
        waves.append(ready)
        done.update(ready)
        for k in ready:
            del remaining[k]
    return waves


def _str_list(v: Any, field_name: str, errors: list[str], ctx: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        errors.append(f"{ctx}.{field_name} must be a list of strings")
        return []
    return v


def validate_plan(data: Any, developer_roles: list[str], max_tasks: int,
                  existing_ids: set[str] | None = None,
                  require_criteria: bool = True) -> Plan:
    """Validate a planner submission. `existing_ids` are ids of already-completed tasks that
    new tasks may depend on (used for corrective plans)."""
    errors: list[str] = []
    if not isinstance(data, dict):
        raise PlanValidationError(["plan must be a JSON object"])
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary must be a non-empty string")
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlanValidationError(errors + ["tasks must be a non-empty list"])
    if len(raw_tasks) > max_tasks:
        errors.append(f"too many tasks ({len(raw_tasks)}); the maximum is {max_tasks}")

    criteria: list[Criterion] = []
    raw_ac = data.get("acceptance_criteria") or []
    if not isinstance(raw_ac, list):
        errors.append("acceptance_criteria must be a list")
        raw_ac = []
    for i, c in enumerate(raw_ac):
        if isinstance(c, str):
            criteria.append(Criterion(id=f"AC{i + 1}", text=c))
        elif isinstance(c, dict) and isinstance(c.get("text"), str):
            criteria.append(Criterion(id=str(c.get("id") or f"AC{i + 1}"), text=c["text"]))
        else:
            errors.append(f"acceptance_criteria[{i}] must have id and text")
    if require_criteria and not criteria:
        errors.append("acceptance_criteria must list the issue-level criteria (at least one)")

    tasks: list[Task] = []
    seen: set[str] = set()
    for i, t in enumerate(raw_tasks):
        ctx = f"tasks[{i}]"
        if not isinstance(t, dict):
            errors.append(f"{ctx} must be an object")
            continue
        tid = str(t.get("id", "")).strip()
        if not ID_RE.match(tid):
            errors.append(f"{ctx}.id '{tid}' must match {ID_RE.pattern}")
        if tid in seen or (existing_ids and tid in existing_ids):
            errors.append(f"{ctx}.id '{tid}' is duplicated")
        seen.add(tid)
        for f in ("title", "description"):
            if not isinstance(t.get(f), str) or not t[f].strip():
                errors.append(f"{ctx}.{f} must be a non-empty string")
        role = t.get("role")
        if role not in developer_roles:
            errors.append(f"{ctx}.role '{role}' must be one of {developer_roles}")
        exp = _str_list(t.get("expected_files"), "expected_files", errors, ctx)
        for p in exp:
            if p.startswith(("/", "\\")) or ".." in p.replace("\\", "/").split("/") or p.startswith(".git"):
                errors.append(f"{ctx}.expected_files entry '{p}' must be a relative path inside the repo")
        tasks.append(Task(
            id=tid, title=str(t.get("title", "")), description=str(t.get("description", "")),
            role=str(role), depends_on=_str_list(t.get("depends_on"), "depends_on", errors, ctx),
            acceptance_criteria=_str_list(t.get("acceptance_criteria"), "acceptance_criteria", errors, ctx),
            expected_files=exp,
        ))

    known = seen | (existing_ids or set())
    for t in tasks:
        for d in t.depends_on:
            if d == t.id:
                errors.append(f"task {t.id} depends on itself")
            elif d not in known:
                errors.append(f"task {t.id} depends on unknown task '{d}'")
    if not errors:
        try:
            topo_waves({t.id: [d for d in t.depends_on if d in seen] for t in tasks})
        except ValueError as e:
            errors.append(str(e))
    if errors:
        raise PlanValidationError(errors)
    return auto_split_plan(Plan(summary=summary.strip(), tasks=tasks, acceptance_criteria=criteria))


# ------------------------------------------------------- auto-split post-pass
MAX_FILES_PER_TASK = 3
WIDGET_SPLIT_THRESHOLD = 3

WIDGET_TERMS: dict[str, tuple[str, ...]] = {
    "filter": ("filter", "filters", "filtering"),
    "button": ("button", "buttons"),
    "panel": ("panel", "panels"),
    "sidebar": ("sidebar", "sidebars"),
    "tab": ("tab", "tabs"),
    "chart": ("chart", "charts"),
    "table": ("table", "tables"),
    "form": ("form", "forms"),
    "menu": ("menu", "menus"),
    "modal": ("modal", "modals"),
    "dialog": ("dialog", "dialogs"),
    "dropdown": ("dropdown", "dropdowns"),
    "slider": ("slider", "sliders"),
    "checkbox": ("checkbox", "checkboxes"),
    "toggle": ("toggle", "toggles"),
    "search": ("search", "searching"),
    "metric": ("metric", "metrics"),
}
_WIDGET_RE = {cat: re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE)
               for cat, words in WIDGET_TERMS.items()}


def mentioned_widgets(description: str) -> list[str]:
    """Distinct UI-widget categories mentioned in `description`, sorted alphabetically."""
    return sorted(cat for cat, rx in _WIDGET_RE.items() if rx.search(description))


def needs_split(task: Task) -> bool:
    """True iff the task touches too many files or bundles too many UI features."""
    return (len(task.expected_files) > MAX_FILES_PER_TASK
            or len(mentioned_widgets(task.description)) >= WIDGET_SPLIT_THRESHOLD)


def _chunks(items: list[str], parts: int) -> list[list[str]]:
    """Split `items` into `parts` contiguous groups as evenly as possible."""
    base, extra = divmod(len(items), parts)
    out: list[list[str]] = []
    idx = 0
    for i in range(parts):
        n = base + (1 if i < extra else 0)
        out.append(items[idx:idx + n])
        idx += n
    return out


def split_task(task: Task, parts: int | None = None) -> list[Task]:
    """Split an oversized task into 2-3 sequential sub-tasks."""
    if parts is None:
        parts = 3 if (len(task.expected_files) > 2 * MAX_FILES_PER_TASK
                      or len(mentioned_widgets(task.description)) >= 5) else 2
    parts = max(2, min(3, parts))

    widgets = mentioned_widgets(task.description)
    if len(task.expected_files) > MAX_FILES_PER_TASK:
        chunks = _chunks(task.expected_files, parts)
        scopes = [", ".join(c) for c in chunks]
    elif widgets:
        # Widget-driven split: keep the (small) file list whole in every part.
        chunks = [task.expected_files] * parts
        scopes = [", ".join(c) for c in _chunks(widgets, parts)]
    else:
        chunks = _chunks(task.expected_files, parts)
        scopes = [", ".join(c) for c in chunks]

    subs: list[Task] = []
    for i, chunk in enumerate(chunks):
        subs.append(Task(
            id=f"{task.id}-{i + 1}",
            title=f"{task.title} (part {i + 1}/{parts})",
            description=(f"Part {i + 1} of {parts} split from task '{task.id}'. "
                         f"Your scope: {scopes[i]}. Implement ONLY this part; do not touch "
                         f"files or features assigned to the other parts. "
                         f"Original instructions: {task.description}"),
            role=task.role,
            depends_on=list(task.depends_on) if i == 0 else [subs[i - 1].id],
            acceptance_criteria=list(task.acceptance_criteria),
            expected_files=list(chunk),
        ))
    return subs


def auto_split_plan(plan: Plan) -> Plan:
    """Deterministic post-pass: split oversized tasks into sequential sub-tasks.

    Note: this may produce more tasks than the planner's max_tasks cap — that is
    intentional, since splitting oversized tasks is the whole point of the pass.
    The sequential construction guarantees no cycles, so the result still passes
    topo_waves.
    """
    if not any(needs_split(t) for t in plan.tasks):
        return plan
    new_tasks: list[Task] = []
    split_map: dict[str, str] = {}  # original id -> last sub-task id
    for t in plan.tasks:
        if needs_split(t):
            subs = split_task(t)
            split_map[t.id] = subs[-1].id
            new_tasks.extend(subs)
        else:
            new_tasks.append(t)
    for t in new_tasks:
        t.depends_on = [split_map.get(d, d) for d in t.depends_on]
    plan.tasks = new_tasks
    return plan


# ---------------------------------------------------------------- review data
SEVERITIES = ("critical", "major", "minor", "info")

REVIEW_JSON_SCHEMA = {
    "type": "object",
    "required": ["verdict", "summary", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {
            "type": "object",
            "required": ["severity", "file", "title", "description", "recommendation"],
            "properties": {
                "severity": {"type": "string", "enum": list(SEVERITIES)},
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "recommendation": {"type": "string"},
            }}},
    },
}


def validate_review(data: Any) -> tuple[dict | None, str | None]:
    errs = []
    if not isinstance(data, dict):
        return None, "review must be an object"
    if data.get("verdict") not in ("approve", "request_changes"):
        errs.append("verdict must be 'approve' or 'request_changes'")
    if not isinstance(data.get("summary"), str):
        errs.append("summary must be a string")
    fnd = data.get("findings")
    if not isinstance(fnd, list):
        errs.append("findings must be a list (possibly empty)")
        fnd = []
    clean = []
    for i, f in enumerate(fnd):
        if not isinstance(f, dict):
            errs.append(f"findings[{i}] must be an object")
            continue
        sev = str(f.get("severity", "")).lower()
        if sev not in SEVERITIES:
            errs.append(f"findings[{i}].severity must be one of {SEVERITIES}")
        for k in ("file", "title", "description", "recommendation"):
            if not isinstance(f.get(k), str):
                errs.append(f"findings[{i}].{k} must be a string")
        line = f.get("line")
        try:
            line = int(line) if line is not None else None
        except (TypeError, ValueError):
            line = None
        clean.append({**f, "severity": sev, "line": line})
    if data.get("verdict") == "approve" and any(c["severity"] in ("critical", "major") for c in clean):
        errs.append("verdict 'approve' is inconsistent with critical/major findings")
    if errs:
        return None, "; ".join(errs)
    return {"verdict": data["verdict"], "summary": data["summary"], "findings": clean}, None


TEST_REPORT_JSON_SCHEMA = {
    "type": "object",
    "required": ["summary", "criteria_tests", "defects"],
    "properties": {
        "summary": {"type": "string"},
        "criteria_tests": {
            "type": "array",
            "description": "Map each acceptance criterion id to pytest node ids that verify it.",
            "items": {"type": "object", "required": ["criterion_id", "tests"],
                      "properties": {"criterion_id": {"type": "string"},
                                     "tests": {"type": "array", "items": {"type": "string"}}}}},
        "defects": {"type": "array", "items": {
            "type": "object", "required": ["title", "description", "reproduction"],
            "properties": {"title": {"type": "string"}, "description": {"type": "string"},
                           "reproduction": {"type": "string"},
                           "file": {"type": "string"},
                           "severity": {"type": "string", "enum": list(SEVERITIES)}}}},
    },
}


def validate_test_report(data: Any, criterion_ids: set[str]) -> tuple[dict | None, str | None]:
    if not isinstance(data, dict):
        return None, "report must be an object"
    errs = []
    if not isinstance(data.get("summary"), str):
        errs.append("summary must be a string")
    ct = data.get("criteria_tests")
    if not isinstance(ct, list):
        errs.append("criteria_tests must be a list")
        ct = []
    mapping: dict[str, list[str]] = {}
    for i, c in enumerate(ct):
        if not isinstance(c, dict) or not isinstance(c.get("tests"), list):
            errs.append(f"criteria_tests[{i}] must have criterion_id and tests[]")
            continue
        cid = str(c.get("criterion_id"))
        if criterion_ids and cid not in criterion_ids:
            errs.append(f"criteria_tests[{i}].criterion_id '{cid}' is not one of {sorted(criterion_ids)}")
        mapping.setdefault(cid, []).extend(str(x) for x in c["tests"])
    defects = data.get("defects")
    if not isinstance(defects, list):
        errs.append("defects must be a list (possibly empty)")
        defects = []
    clean_def = []
    for i, d in enumerate(defects):
        if not isinstance(d, dict) or not all(isinstance(d.get(k), str) for k in ("title", "description", "reproduction")):
            errs.append(f"defects[{i}] needs title, description, reproduction strings")
            continue
        sev = str(d.get("severity") or "major").lower()
        clean_def.append({**d, "severity": sev if sev in SEVERITIES else "major"})
    if errs:
        return None, "; ".join(errs)
    return {"summary": data["summary"], "criteria_tests": mapping, "defects": clean_def}, None
