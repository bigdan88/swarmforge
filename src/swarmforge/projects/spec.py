"""A human-owned fixed specification and a byte-protected living plan."""
from __future__ import annotations

import json
import math
import re


class ProjectError(ValueError):
    pass


def validate(d: dict) -> dict:
    if not isinstance(d, dict):
        raise ProjectError("The project definition must be an object")
    for k in ("name", "goal", "users", "repo", "out_of_scope", "stack", "hosting", "deadline", "latitude", "ask_before"):
        if not isinstance(d.get(k), str) or not d[k].strip():
            raise ProjectError(f"Missing {k}; use 'None' when explicitly unconstrained")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", d["repo"]):
        raise ProjectError("Repository must be owner/name")
    if d.get("repository_kind") not in ("new", "existing"):
        raise ProjectError("Choose new or existing repository")
    if not isinstance(d.get("budget_usd"), (int, float)) or not math.isfinite(d["budget_usd"]) or d["budget_usd"] <= 0:
        raise ProjectError("A positive project budget is required")
    ms = d.get("milestones")
    if not isinstance(ms, list) or not 3 <= len(ms) <= 6:
        raise ProjectError("Define 3–6 milestones")
    for m in ms:
        if not isinstance(m, dict) or not m.get("name") or not m.get("goal"):
            raise ProjectError("Every milestone needs a name and goal")
        checks = m.get("checks")
        if not isinstance(checks, list) or not 2 <= len(checks) <= 5 or not all(isinstance(c, str) and c.strip() for c in checks):
            raise ProjectError("Every milestone needs 2–5 checkable done-when statements")
        for k in ("estimate_usd", "estimate_hours"):
            if not isinstance(m.get(k), (int, float)) or not math.isfinite(m[k]) or m[k] <= 0:
                raise ProjectError(f"Every milestone needs positive {k}")
    if sum(m["estimate_usd"] for m in ms) > d["budget_usd"]:
        raise ProjectError("Milestone estimates exceed the project budget")
    return d


def render(d: dict, living: str = "### Current state\nAwaiting launch.\n\n### Decisions made\n\n### Plan\n\n### Plan changes\n\n### Open questions\n") -> str:
    validate(d)
    lines = [f"# {d['name']}", "", "## Fixed", "", "### Goal", d["goal"], "", "### Users", d["users"], "", "### Milestones"]
    for i, m in enumerate(d["milestones"], 1):
        lines += [f"#### {i}. {m['name']}", m["goal"], *[f"- [ ] {c}" for c in m["checks"]],
                  f"Estimate: ${m['estimate_usd']} · {m['estimate_hours']} hours", ""]
    lines += ["### Out of scope", d["out_of_scope"], "", "### Constraints",
              f"Repository: {d['repo']} ({d['repository_kind']})", f"Stack: {d['stack']}", f"Hosting: {d['hosting']}",
              f"Budget: ${d['budget_usd']}", f"Deadline: {d['deadline']}", "", "### Latitude", d["latitude"],
              "Ask before: " + d["ask_before"], "", "## Living", "", living]
    text = "\n".join(lines)
    fixed(text)
    return text


def fixed(text: str) -> str:
    if len(re.findall(r"(?m)^## Fixed$", text)) != 1 or len(re.findall(r"(?m)^## Living$", text)) != 1:
        raise ProjectError("Spec must contain exactly one ## Fixed and ## Living heading")
    boundary = re.search(r"(?m)^## Living\n", text)
    if boundary is None:
        raise ProjectError("Living heading must end with a newline")
    before = text[:boundary.start()]
    if re.search(r"(?m)^## Fixed\n", before) is None:
        raise ProjectError("Fixed must precede Living")
    return before


def replace_living(original: str, proposed: str) -> str:
    if fixed(original) != fixed(proposed):
        raise ProjectError("The lead cannot change any byte of the fixed specification")
    return proposed


def decode(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        obj = json.loads(text)
    except (TypeError, json.JSONDecodeError) as e:
        raise ProjectError("Expected structured JSON from the model") from e
    if not isinstance(obj, dict):
        raise ProjectError("Expected a JSON object")
    return obj
