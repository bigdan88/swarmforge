"""Visual evidence collection: decide when a plan touches the UI and capture dashboard tabs."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import visual

UI_MARKERS = ("dashboard.py", "dashboard_pages/", ".css")
EVIDENCE_KIND = "evidence"

PR_URL_RE = re.compile(r"/pulls?/(\d+)")


def touches_ui(files: list[str]) -> bool:
    """True when any planned file is a dashboard page or stylesheet."""
    return any(marker in f for f in files for marker in UI_MARKERS)


def tabs_for_files(files: list[str]) -> list[str]:
    """Map planned files to dashboard tab slugs (deduped, order preserved)."""
    tabs: list[str] = []
    for f in files:
        m = re.search(r"dashboard_pages/([^/]+)\.py$", f)
        if m:
            tab = m.group(1)
        elif f.endswith(("dashboard.py", ".css")):
            tabs.extend(t for t in visual.DEFAULT_TABS if t not in tabs)
            continue
        else:
            continue
        if tab not in tabs:
            tabs.append(tab)
    return tabs


def plan_files(plan: Any) -> list[str]:
    """Union of expected_files across all tasks of a Plan (dataclass or dict)."""
    tasks = plan.get("tasks", []) if isinstance(plan, dict) else plan.tasks
    files: list[str] = []
    for t in tasks:
        expected = (
            t.get("expected_files", []) if isinstance(t, dict) else t.expected_files
        )
        for f in expected:
            if f not in files:
                files.append(f)
    return files


def screenshot_tabs(
    repo_dir: str | Path,
    out_dir: str | Path,
    files: list[str],
    *,
    failed: bool = False,
    themes: tuple[str, ...] = ("light",),
    capture: Callable[..., dict[str, Path]] | None = None,
    port: int = 8699,
) -> list[dict]:
    """Capture screenshots for tabs affected by `files`; [] when nothing is UI-related."""
    if not touches_ui(files):
        return []
    tabs = tabs_for_files(files)
    if failed and "failures" not in tabs:
        tabs.append("failures")
    capture = capture or visual.capture_tabs
    result = capture(repo_dir, out_dir, tabs=tabs, themes=themes, port=port)
    shots: list[dict] = []
    for key, path in result.items():
        tab, _, theme = key.rpartition("-")
        shots.append({"tab": tab, "theme": theme, "file": str(Path(path))})
    return shots


def shots_from_events(events: list[dict]) -> list[dict]:
    """Shots recorded by the newest kind='evidence' event, or []."""
    for ev in reversed(events):
        if ev.get("kind") == EVIDENCE_KIND:
            data = ev.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    data = {}
            return list((data or {}).get("shots") or [])
    return []


def evidence_markdown(shots: list[dict], *, failed: bool = False) -> str:
    """Markdown '## Visual evidence' section listing captured screenshots."""
    lines = ["## Visual evidence"]
    for s in shots:
        lines.append(f"- **{s['tab']}** ({s['theme']}): `{s['file']}`")
    if failed:
        lines.append("")
        lines.append(
            "The failures/review page screenshot above shows the findings that are still unresolved."
        )
    return "\n".join(lines) + "\n"


def attach_to_pr(
    gh: Any, owner: str, name: str, pr_url: str, shots: list[dict]
) -> list[str] | None:
    """Upload shot PNGs as PR assets and append a visual-evidence section to the PR body.

    Returns the list of asset URLs, or None when there is no token or nothing to attach
    (the caller logs the skip).
    """
    if not gh.token or not shots:
        return None
    m = PR_URL_RE.search(pr_url)
    if not m:
        return None
    number = int(m.group(1))
    urls: list[str] = []
    for s in shots:
        urls.append(gh.upload_pr_asset(owner, name, number, s["file"]))
    section = ["## 📸 Visual evidence"]
    for s, url in zip(shots, urls):
        section.append(f"![{s['tab']} ({s['theme']})]({url})")
    body = gh.get_pr_body(owner, name, number) or ""
    gh.update_pr_body(
        owner, name, number, body.rstrip("\n") + "\n\n" + "\n".join(section) + "\n"
    )
    return urls
