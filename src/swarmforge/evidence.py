"""Visual evidence: decide when a plan warrants dashboard screenshots and capture them.

The orchestrator calls :func:`collect` once a run reaches a terminal state; the resulting
manifest (``evidence/evidence.json``) is consumed by the report and PR publication steps.
"""

from __future__ import annotations

import json
from pathlib import Path

#: Hard cap for a single evidence capture (the orchestrator enforces it with asyncio.wait_for).
MAX_CAPTURE_S = 300.0

MANIFEST_NAME = "evidence.json"

# Dashboard tab slugs (see visual.slugify / visual.DEFAULT_TABS).
_TAB_HINTS = (
    ("settings", "settings"),
    ("speed", "speed"),
    ("benchmark", "speed"),
    ("failures", "failures"),
    ("history", "failures"),
    ("knowledge", "knowledge"),
    ("chat", "chat"),
    ("new-task", "new-task"),
    ("new_task", "new-task"),
    ("projects", "projects"),
    ("prs", "prs"),
)

_UI_SUFFIXES = (".css", ".scss", ".html", ".js", ".ts", ".tsx", ".jsx")


def plan_file_set(tasks) -> list[str]:
    """Union of every task's expected_files, de-duplicated, order preserved."""
    files: list[str] = []
    for t in tasks or []:
        for f in getattr(t, "expected_files", None) or []:
            if f not in files:
                files.append(f)
    return files


def touches_ui(files) -> bool:
    """True when any changed/expected file plausibly affects the dashboard UI."""
    for f in files or []:
        p = str(f).replace("\\", "/").lower()
        if p.endswith(_UI_SUFFIXES):
            return True
        name = p.rsplit("/", 1)[-1]
        if (
            name.startswith("dashboard")
            or "/dashboard" in p
            or "streamlit" in p
            or "/ui/" in p
            or "/pages/" in p
            or "/frontend/" in p
        ):
            return True
    return False


def tabs_for(files, *, failed: bool = False) -> list[str]:
    """Dashboard tabs worth screenshotting given the touched files.

    ``work`` is always included; a failed run always gets the Review/failures tab so
    unresolved findings are visible.
    """
    tabs = ["work"]
    for f in files or []:
        p = str(f).replace("\\", "/").lower()
        for hint, tab in _TAB_HINTS:
            if hint in p and tab not in tabs:
                tabs.append(tab)
    if failed and "failures" not in tabs:
        tabs.append("failures")
    return tabs


def write_manifest(out_dir, shots: list[dict]) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / MANIFEST_NAME
    path.write_text(json.dumps(shots, indent=2), encoding="utf-8")
    return path


def read_manifest(out_dir) -> list[dict]:
    path = Path(out_dir) / MANIFEST_NAME
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def collect(repo_dir, out_dir, *, tabs=None) -> list[dict]:
    """Screenshot the dashboard tabs and write the evidence manifest.

    Returns the list of shot records (``{"tab": ..., "theme": ..., "path": ...}``).
    """
    from . import visual

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    captured = visual.capture_tabs(repo_dir, out, tabs=tabs)
    shots = [
        {
            "tab": key.rsplit("-", 1)[0],
            "theme": key.rsplit("-", 1)[1],
            "path": str(path),
        }
        for key, path in captured.items()
    ]
    write_manifest(out, shots)
    return shots
