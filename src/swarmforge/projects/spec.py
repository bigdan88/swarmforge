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
    # Change rounds requested after a project finished are added as extra milestones;
    # the 3–6 rule applies to the originally planned ones.
    if not isinstance(ms, list) or not 3 <= len([m for m in ms if not (isinstance(m, dict) and m.get("change_request"))]) <= 6:
        raise ProjectError("Define 3–6 milestones")
    if len(ms) > 40:
        raise ProjectError("Too many change rounds; start a new project for a larger redesign")
    for index, m in enumerate(ms):
        if not isinstance(m, dict) or not m.get("name") or not m.get("goal"):
            raise ProjectError("Every milestone needs a name and goal")
        checks = m.get("checks")
        if not isinstance(checks, list) or not 2 <= len(checks) <= 5 or not all(isinstance(c, str) and c.strip() for c in checks):
            raise ProjectError("Every milestone needs 2–5 checkable done-when statements")
        levels = m.get("evidence_requirements", {})
        expected_ids = {f"M{index + 1}C{ci + 1}" for ci in range(len(checks))}
        if not isinstance(levels, dict) or set(levels) - expected_ids or any(v not in ("unit", "integration", "product") for v in levels.values()):
            raise ProjectError("Evidence requirements must map this milestone's stable check IDs to unit, integration or product")
        for k in ("estimate_usd", "estimate_hours"):
            if not isinstance(m.get(k), (int, float)) or not math.isfinite(m[k]) or m[k] <= 0:
                raise ProjectError(f"Every milestone needs positive {k}")
    if sum(m["estimate_usd"] for m in ms if not m.get("change_request")) > d["budget_usd"]:
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
    for i, milestone in enumerate(d["milestones"], 1):
        if milestone.get("evidence_requirements"):
            lines.insert(lines.index("## Living"), "Verification for milestone " + str(i) + ": " + ", ".join(f"{key}={value}" for key, value in milestone["evidence_requirements"].items()) + "\n")
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


LOG_SECTIONS = ("Decisions made", "Plan changes")
LOG_LIMIT_CHARS = 12000      # newest log entries kept per log section
SECTION_LIMIT_CHARS = 8000   # other living sections (current state, plan, open questions)


def compact_living(spec: str) -> str:
    """Bound the living plan without touching the fixed part.

    The decision and plan-change logs are append-only, and a lead that paraphrases them used to
    get the old text re-added every round: one project's spec grew past 256 KB until every planning
    call timed out. Exact duplicate entries are dropped and only the newest entries are kept; older
    ones remain in the spec file's git history and the project's event log."""
    head = fixed(spec)
    living = spec[len(head):]
    parts = re.split(r"(?m)^(### .+)$", living)
    out = [parts[0]]
    for i in range(1, len(parts), 2):
        title, body = parts[i], parts[i + 1] if i + 1 < len(parts) else ""
        name = title[4:].strip()
        if name in LOG_SECTIONS:
            seen, entries, earlier = set(), [], 0
            for line in body.splitlines():
                key = line.strip()
                trimmed = re.match(r"- \((\d+) older entries trimmed", key)
                if trimmed:
                    earlier += int(trimmed.group(1))
                    continue
                if key and key in seen:
                    continue
                seen.add(key)
                entries.append(line)
            while entries and not entries[0].strip():
                entries.pop(0)
            kept, size = [], 0
            for line in reversed(entries):
                if size + len(line) + 1 > LOG_LIMIT_CHARS and kept:
                    break
                kept.append(line)
                size += len(line) + 1
            kept.reverse()
            dropped = earlier + sum(1 for line in entries[:len(entries) - len(kept)] if line.strip())
            changed = dropped != earlier or len([e for e in entries if e.strip()]) != len([b for b in body.splitlines()
                                                                                      if b.strip() and "older entries trimmed" not in b])
            if changed or dropped:
                note = [f"- ({dropped} older entries trimmed; see this file's git history and the project's event log)"] if dropped else []
                body = "\n" + "\n".join(note + kept).strip("\n") + "\n\n"
        elif len(body) > SECTION_LIMIT_CHARS:
            body = body[:SECTION_LIMIT_CHARS].rstrip() + "\n(trimmed)\n\n"
        out += [title, body]
    return head + "".join(out)


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
