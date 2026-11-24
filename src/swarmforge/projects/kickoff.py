"""Short project interviews: question forms and editable, unapproved defaults."""
from __future__ import annotations

import math
import re
from urllib.parse import urlparse

from .spec import ProjectError, validate


OPTIONAL_DEFAULTS = {
    "out_of_scope": "Features beyond the approved milestones.",
    "stack": "The team may choose suitable tools.",
    "hosting": "Local preview; public deployment requires approval.",
    "deadline": "No fixed deadline.",
    "latitude": "Implementation choices within the approved scope.",
    "ask_before": "Changes to the approved scope, paid external services, credentials or public deployment.",
}


def defaults(cfg) -> dict:
    return {"repo_owner": cfg.projects.default_repo_owner, "budget_usd": cfg.projects.default_budget_usd,
            **OPTIONAL_DEFAULTS}


def initial_questions(choices: dict) -> list[dict]:
    fields = [
        {"id": "goal", "label": "What would you like to build?", "kind": "textarea", "required": True,
         "placeholder": "Describe the app and what you want it to do."},
        {"id": "users", "label": "Who is it for?", "kind": "text", "required": False,
         "placeholder": "Optional — I can infer this from your idea."},
        {"id": "capabilities", "label": "What must users be able to do?", "kind": "textarea", "required": False,
         "placeholder": "Optional — list the key actions, or let the lead propose them."},
        {"id": "constraints", "label": "Anything to include or avoid?", "kind": "textarea", "required": False,
         "placeholder": "Optional — technology preferences, exclusions or other constraints."},
    ]
    if not choices.get("repo_owner"):
        fields.append({"id": "repo_owner", "label": "GitHub account or organization", "kind": "text", "required": True})
    return fields


def questions(raw) -> list[dict]:
    """Only bounded, presentational fields. Setup trivia never blocks the interview."""
    result, seen = [], set()
    for item in raw[:3] if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        key, label = str(item.get("id") or ""), str(item.get("label") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key) or not label or key in seen:
            continue
        if key in {"budget", "budget_usd", *OPTIONAL_DEFAULTS}:
            continue
        field = {"id": key, "label": label[:300], "kind": "textarea", "required": item.get("required") is True}
        for name in ("help", "placeholder", "default"):
            if isinstance(item.get(name), str):
                field[name] = item[name][:1000]
        opts = [s[:200] for s in item.get("options", [])[:6] if isinstance(s, str) and s.strip()] if isinstance(item.get("options"), list) else []
        if item.get("kind") == "select" and len(opts) >= 2:
            field.update(kind="select", options=opts)
        elif item.get("kind") == "text":
            field["kind"] = "text"
        seen.add(key)
        result.append(field)
    return result


def recovery_questions(has_brief: bool, choices: dict) -> list[dict]:
    if has_brief:
        return [{"id": "notes", "label": "Anything to add before I draft your proposal?", "kind": "textarea", "required": False,
                 "placeholder": "Optional — leave blank to use the conversation so far and the proposed defaults."}]
    return initial_questions(choices)


def latest_questions(store, p, choices=None) -> tuple[int | None, list[dict]]:
    import json
    row = store._one("SELECT id,actions FROM chat_messages WHERE conversation_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
                     (p["conversation_id"],))
    actions = json.loads(row["actions"] or "{}") if row else {}
    fields = actions.get("project_questions", [])
    if "project_questions" not in actions and not p["definition"]:
        has_brief = bool(store._one("SELECT id FROM chat_messages WHERE conversation_id=? AND role='user' LIMIT 1", (p["conversation_id"],)))
        fields = recovery_questions(has_brief, choices or {})
    return (row["id"] if row else None, fields)


def answer_message(store, p, args, choices=None) -> str:
    """Turn a submitted question form into the same conversational input as a typed reply."""
    message_id, fields = latest_questions(store, p, choices)
    if args.get("expected_question") != message_id:
        raise ProjectError("The questions changed. Reload the form before submitting your answers.")
    answers = args.get("answers")
    if not isinstance(answers, dict) or set(answers) - {f["id"] for f in fields}:
        raise ProjectError("Submit answers to the displayed questions.")
    lines = []
    for field in fields:
        value = answers.get(field["id"], "")
        if not isinstance(value, str) or len(value) > 4000:
            raise ProjectError("Keep each answer within 4,000 characters.")
        value = value.strip()
        if field.get("required") and not value:
            raise ProjectError(f"Please answer: {field['label']}")
        if value and field.get("kind") == "select" and value not in field.get("options", []):
            raise ProjectError(f"Choose an option for: {field['label']}")
        if value:
            lines.append(f"{field['label']}\n{value}")
    message = "\n\n".join(lines)
    if not message and fields and not any(f.get("required") for f in fields):
        return "Use your recommendations for these choices and draft the project proposal."
    if not message or len(message) > 16000:
        raise ProjectError("Describe your idea or answer at least one question (up to 16,000 characters).")
    return message


def positive(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def normalize(raw: dict, choices: dict, previous: dict | None = None) -> dict:
    """Fill proposal details, while the approval-time validator stays strict."""
    if not isinstance(raw, dict):
        raise ProjectError("The lead must draft a project definition.")
    d = {**(previous or {}), **raw}
    for key, value in OPTIONAL_DEFAULTS.items():
        if not isinstance(d.get(key), str) or not d[key].strip():
            d[key] = value
    if not positive(d.get("budget_usd")):
        d["budget_usd"] = choices["budget_usd"]
    repo = str(d.get("repo") or "").strip().removesuffix(".git").rstrip("/")
    if repo.startswith("https://github.com/"):
        repo = urlparse(repo).path.strip("/")
    owner = choices.get("repo_owner") or ""
    slug = re.sub(r"[^a-z0-9]+", "-", str(d.get("name") or "project").lower()).strip("-")[:80] or "project"
    if not repo or repo == owner:
        repo = f"{owner}/{slug}" if owner else ""
        d["repository_kind"] = "new"
    elif "/" not in repo and owner:
        repo = f"{owner}/{repo}"
    d["repo"] = repo
    d.setdefault("repository_kind", "new")
    milestones = d.get("milestones")
    if isinstance(milestones, list) and 3 <= len(milestones) <= 6:
        milestones = [dict(m) if isinstance(m, dict) else m for m in milestones]
        d["milestones"] = milestones
        for m in milestones:
            if not isinstance(m, dict):
                continue
            if not positive(m.get("estimate_usd")):
                m["estimate_usd"] = round(d["budget_usd"] / (len(milestones) + 1), 2)
            if not positive(m.get("estimate_hours")):
                m["estimate_hours"] = 1
        if all(isinstance(m, dict) and positive(m.get("estimate_usd")) for m in milestones):
            total = sum(m["estimate_usd"] for m in milestones)
            if total > d["budget_usd"]:
                # Keep a reserve instead of rejecting a model's rough cost estimate back to the user.
                allowance = d["budget_usd"] * .8
                for m in milestones:
                    m["estimate_usd"] = max(.01, math.floor(m["estimate_usd"] / total * allowance * 100) / 100)
    return validate(d)


def fallback(context: dict, choices: dict) -> dict:
    """A reviewable starter proposal if the lead repeats questions instead of drafting."""
    previous = context.get("draft") or {}
    if previous:
        return previous  # Never replace an existing proposal with generic milestones.
    brief = next((m["content"] for m in context["messages"] if m["role"] == "user"), "")[:4000]
    title = next((s.strip() for s in brief.splitlines() if s.strip() and "?" not in s), "New project")[:80]
    if title == "New project":
        title = "Project " + context["project_id"]
    raw = {"name": title, "goal": brief, "users": "The users described in the project brief; confirm during proposal review.", **context.get("repository_choice", {}),
           "milestones": [
               {"name": "Working preview", "goal": "Create a runnable preview for the project brief.", "checks": ["The README startup command opens the app locally.", "The main screen explains the app's purpose and provides an entry point to its main workflow."]},
               {"name": "Core workflows", "goal": "Implement the capabilities in the project brief.", "checks": ["A user can complete each core workflow described in the brief using the preview.", "Invalid input and empty results display an explanatory message without crashing."]},
               {"name": "Verification and handoff", "goal": "Verify the brief and document the finished preview.", "checks": ["Automated tests cover the core workflows and pass with the documented command.", "The README documents setup, usage and any limitations remaining from the brief."]},
           ]}
    return normalize(raw, choices)
