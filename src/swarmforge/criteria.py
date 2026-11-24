"""Sort acceptance criteria: what a test can prove versus what only a person can check.

A run can only prove things with tests and commands. A criterion like "looks right in the browser",
"the live site loads", or "the report says X is unverified" can never be proved by the swarm, so
treating it as a pass/fail gate fails good work over and over. Such criteria are sorted out when a
task is planned and when results are judged: they become checks for a person ("needs you"), shown
with the result, and never block delivery. Everything a test can prove stays a hard gate.
"""
from __future__ import annotations

import re

# Phrases that need human eyes, a real browser/device, or a live deployment - things no test in the
# sandbox can observe. Deliberately specific: "the modal has a close button" is testable and must stay
# a gate, while "visually confirm the modal looks right" is not.
_PERSON = [
    r"\b(in|using|with|from) (a|the|your|any) (real |web )?browser\b",
    r"\bbrowser (console|devtools)\b",
    r"\b(open|view|load|observe|check|confirm|verify|inspect)\w* (it |them |this |the \w+ )?(in|on) (a|the) (browser|phone|device|live site)\b",
    r"\bmanually\b|\bmanual (check|checks|verification|inspection|review|qa|sign-?off)\b",
    r"\bby hand\b",
    r"\bhuman\b|\ba person\b|\bthe user (confirms|checks|verifies|sees)\b",
    r"\bvisual(ly)? (check|inspect|inspection|confirm|confirmation|verif\w*|review)\b",
    r"\b(eyeball|eye-ball)\b",
    r"\blive (site|url|page|deployment|demo)\b",
    r"\b(deployed|published) (site|page|app)\b.*\b(loads?|works?|shows?|renders?|reachable)\b",
    r"\b(real|physical|mobile) (device|phone|tablet)\b|\bon (a|my|your) phone\b",
    r"\bdevtools\b",
    r"\b(task |run |final )?(report|summary|pr body|pull request (body|description))\b[^.]{0,60}\b(states?|says|mentions?|lists?|notes?|explains?|leaves?|records?)\b",
    r"\bunverified\b|\bcannot be verified\b|\bnot (been )?verified (by|in) (a|the) (browser|person)\b",
    r"\blooks? (good|nice|right|polished|cohesive|clean|professional|consistent)\b",
    r"\bfeels? (smooth|snappy|natural|responsive|right)\b",
    r"\b(screen ?reader|lighthouse score)\b",
]
PERSON_RE = re.compile("|".join(f"(?:{p})" for p in _PERSON), re.IGNORECASE)

NEEDS_YOU = "needs_you"
NEEDS_YOU_EVIDENCE = "Only a person can check this (browser, device, live site or a judgement call); it does not block delivery."


def needs_person(text: str) -> bool:
    """True when no automated test in the sandbox could prove this criterion."""
    return bool(PERSON_RE.search(text or ""))


def split(criteria) -> tuple[list[str], list[str]]:
    """(criteria a test can prove, checks for a person)."""
    gated: list[str] = []
    person: list[str] = []
    for c in criteria or []:
        (person if needs_person(str(c)) else gated).append(str(c))
    return gated, person


def mark_person_checks(results: list[dict]) -> list[dict]:
    """Criteria results from the test run: unproved person-only criteria become `needs_you` (not a
    failure). A criterion that a test actually failed stays failed, whatever it says."""
    out = []
    for r in results:
        if r.get("status") == "unverified" and needs_person(r.get("text", "")):
            r = {**r, "status": NEEDS_YOU, "evidence": NEEDS_YOU_EVIDENCE}
        out.append(r)
    return out
