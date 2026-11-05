"""Visual evidence: decide which dashboard tabs a run touched and capture them.

Pure helpers (no browser at import time) used by the orchestrator at run end:
detect whether a plan touched UI files, pick the affected tabs, capture
screenshots via :mod:`swarmforge.visual`, and persist a small manifest so the
report and PR body can reference the shots.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from . import visual

UI_MARKERS = ("dashboard.py", "dashboard_pages/", ".css")

#: Hard cap (seconds) for a single evidence capture; callers use it to bail out.
MAX_CAPTURE_S = 300

MANIFEST_NAME = "manifest.json"


def touches_ui(files: Iterable[str]) -> bool:
    """True when any file in the plan's file set is a dashboard/UI file."""
    return any(marker in str(f) for f in files for marker in UI_MARKERS)


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
    if failed and "work" not in tabs:
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
    """Read the evidence manifest of a run directory; [] when absent/corrupt."""
    path = Path(run_dir) / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [s for s in data if isinstance(s, dict)]
