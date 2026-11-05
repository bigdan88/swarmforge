"""Visual evidence: decide when a plan warrants dashboard screenshots and capture them.

Pure helpers (no browser at import time) used by the orchestrator at run end:
detect whether a plan touched UI files, pick the affected tabs, capture
screenshots via :mod:`swarmforge.visual`, and persist a small manifest (under
``<run_dir>/evidence/manifest.json``) so the report and PR publication steps
can reference the shots.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from . import visual

UI_MARKERS = ("dashboard.py", "dashboard_pages/", ".css")

# Additional suffixes/paths that plausibly affect the dashboard UI (frontend
# assets, pages, streamlit code) even when the filename itself is generic.
_UI_SUFFIXES = (".css", ".scss", ".html", ".js", ".ts", ".tsx", ".jsx")
_UI_NAME_HINTS = ("dashboard", "streamlit", "/ui/", "/pages/", "/frontend/")

#: Hard cap (seconds) for a single evidence capture; callers use it to bail out.
MAX_CAPTURE_S = 300

MANIFEST_NAME = "manifest.json"


def touches_ui(files: Iterable[str]) -> bool:
    """True when any file in the plan's file set is a dashboard/UI file."""
    for f in files or []:
        text = str(f)
        if any(marker in text for marker in UI_MARKERS):
            return True
        p = text.replace("\\", "/").lower()
        if p.endswith(_UI_SUFFIXES):
            return True
        if any(hint in p for hint in _UI_NAME_HINTS):
            return True
    return False


def plan_file_set(tasks: Iterable) -> list[str]:
    """Flatten the expected file set of plan task objects (or dicts)."""
    files: list[str] = []
    for task in tasks or []:
        expected = getattr(task, "expected_files", None)
        if expected is None and isinstance(task, dict):
            expected = task.get("expected_files")
        files.extend(str(f) for f in (expected or []))
    return files


def tabs_for(files: Iterable[str], *, failed: bool = False) -> list[str]:
    """Dashboard tabs (slugs) worth screenshotting for this file set."""
    tabs: list[str] = []
    for f in files:
        f = str(f)
        if "dashboard_pages/" in f or f.endswith("dashboard.py"):
            tabs.extend(visual.DEFAULT_TABS)
        elif f.endswith(".css"):
            tabs.extend(["work", "settings"])
    if failed:
        # failed runs always show the Review tab (unresolved findings) ...
        tabs.append("failures")
        # ... and the work tab so the change itself is visible
        tabs.append("work")
    # preserve DEFAULT_TABS order, dedupe, slugify
    ordered = [visual.slugify(t) for t in visual.DEFAULT_TABS]
    seen = {visual.slugify(t) for t in tabs}
    return [t for t in ordered if t in seen] or (["work"] if failed else [])


def _shot_from_path(path: Path) -> dict:
    stem = path.stem
    tab, _, theme = stem.rpartition("-")
    if theme not in ("light", "dark"):
        theme = "light"
    if not tab:
        tab = "work"
    return {"tab": tab, "theme": theme, "file": str(path)}


def collect(
    repo_dir, out_dir, *, tabs, themes=("light", "dark"), capture=None
) -> list[dict]:
    """Capture screenshots for `tabs` and write a manifest into `out_dir`.

    Propagates exceptions; the caller (orchestrator) swallows them so a
    capture failure never fails the run.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fn = capture or visual.capture_tabs
    result = fn(repo_dir, out_dir, tabs=tabs, themes=themes)
    if isinstance(result, dict):
        paths = list(result.values())
    else:
        paths = list(result)
    shots = [_shot_from_path(Path(p)) for p in paths]
    write_manifest(out_dir, shots)
    return shots


def write_manifest(out_dir: Path, shots: list[dict]) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / MANIFEST_NAME
    path.write_text(json.dumps(shots, indent=2), encoding="utf-8")
    return path


def read_manifest(run_dir) -> list[dict]:
    """Read the evidence manifest of a run directory; [] when absent/corrupt.

    Looks both directly in `run_dir` (legacy/delivery layout) and in the
    `evidence/` sub-directory where the orchestrator's collector writes it.
    """
    base = Path(run_dir) if run_dir else None
    if base is None:
        return []
    for candidate in (base / MANIFEST_NAME, base / "evidence" / MANIFEST_NAME):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, list):
            return [s for s in data if isinstance(s, dict)]
    return []
