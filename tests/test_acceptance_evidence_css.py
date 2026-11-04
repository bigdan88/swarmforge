"""AC1 completion: a plan that touches only a CSS file must also flow through the
visual-evidence pipeline end to end.

``test_acceptance_evidence_pipeline.py`` proves the dashboard.py path;
``test_evidence.py`` classifies ``.css`` at the unit level. This file closes the
remaining gap: a CSS-only plan triggers ``_collect_evidence`` (the real
``evidence.touches_ui`` gate), records shots, gains the '## Visual evidence'
section in report.md (AC2) and reaches the draft PR body with at least one
image link via ``upload_pr_asset`` + body PATCH (AC1).
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from swarmforge import evidence
from swarmforge.db import Store
from swarmforge.github import Issue
from swarmforge.orchestrator import Orchestrator
from swarmforge.report import write_report
from swarmforge.services.publication import PublicationService


@dataclass
class _Plan:
    summary: str = "restyle the dashboard"
    tasks: list = field(default_factory=list)
    acceptance_criteria: list = field(default_factory=list)


class _FakeWorkspace:
    def __init__(self, repo, changed):
        self.repo = repo
        self._changed = list(changed)

    def changed_files(self):
        return list(self._changed)


class _StubGH:
    def __init__(self):
        self.token = "tok"
        self.calls = []

    def create_draft_pr(self, owner, name, head, base, title, body):
        self.calls.append(("create_draft_pr", title, body))
        return f"https://github.com/{owner}/{name}/pull/9"

    def upload_pr_asset(self, owner, name, number, path):
        self.calls.append(("upload_pr_asset", number, str(path)))
        Path(path).read_bytes()  # a real upload reads the PNG
        return f"https://github.com/owner/repo/releases/download/tmp/{Path(path).name}"

    def get_pr_body(self, owner, name, number):
        return "original body"

    def update_pr_body(self, owner, name, number, body):
        self.calls.append(("update_pr_body", number, body))

    def add_labels(self, *a):
        pass

    def comment(self, *a):
        pass


def _css_capture(tmp_path):
    """Stand-in for the playwright-backed screenshot_tabs.

    Keeps the *real* tab mapping (css -> every default tab) and writes fake PNGs,
    so the test exercises the orchestrator's UI gate rather than the browser.
    """

    def screenshot_tabs(repo_dir, out_dir, files, *, failed=False, **kw):
        assert evidence.touches_ui(files), "test is only valid for UI-touching plans"
        tabs = evidence.tabs_for_files(files)
        if failed and "failures" not in tabs:
            tabs.append("failures")
        shots = []
        for tab in tabs:
            png = Path(out_dir) / f"{tab}-light.png"
            png.parent.mkdir(parents=True, exist_ok=True)
            png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            shots.append({"tab": tab, "theme": "light", "file": str(png)})
        return shots

    return screenshot_tabs


def _run(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(evidence, "screenshot_tabs", _css_capture(tmp_path))
    store = Store(tmp_path / "db")
    orch = Orchestrator(cfg, store, mode="swarm", echo=None)
    orch.run_id = "r-css"
    orch.run_dir = tmp_path / "runs" / "r-css"
    orch.run_dir.mkdir(parents=True)
    orch.ws = _FakeWorkspace(str(tmp_path), ["assets/theme.css"])
    orch.plan = _Plan(tasks=[{"expected_files": ["assets/theme.css"]}])
    store.create_run(orch.run_id, mode="swarm", run_dir=str(orch.run_dir))
    store.update_run(orch.run_id, state="COMPLETED")
    asyncio.run(orch._collect_evidence())
    return orch, store


def _publish(store, cfg):
    cfg.github.require_push_approval = False
    logs = []
    gh = _StubGH()
    svc = PublicationService(
        cfg=cfg,
        store=store,
        run_id="r-css",
        issue=Issue("t", "b", url="https://github.com/o/r/issues/1"),
        ws=SimpleNamespace(
            repo=str(Path("/tmp/x")), integration_branch="sf/x", base_branch="main"
        ),
        llm=None,
        log=lambda kind, msg, **kw: logs.append((kind, msg, kw.get("level", "info"))),
        update_base=None,
        push=lambda *a: None,
    )
    return svc, gh, logs


def test_css_plan_records_screenshots_for_all_tabs(tmp_path, cfg, monkeypatch):
    """A CSS-only plan passes the UI gate and captures every default tab."""
    orch, store = _run(tmp_path, cfg, monkeypatch)
    shots = evidence.shots_from_events(store.events(orch.run_id))
    assert shots, "a CSS-only plan must still produce screenshots"
    assert {s["tab"] for s in shots} == set(evidence.visual.DEFAULT_TABS)
    evs = [e for e in store.events(orch.run_id) if e["kind"] == "evidence"]
    assert len(evs) == 1 and evs[0]["level"] == "info"


def test_css_plan_reaches_draft_pr_body_with_image_link(tmp_path, cfg, monkeypatch):
    """AC1 for the CSS branch: every PNG is uploaded and the PR body gains an
    image link under '## 📸 Visual evidence'."""
    _, store = _run(tmp_path, cfg, monkeypatch)
    shots = evidence.shots_from_events(store.events("r-css"))
    svc, gh, logs = _publish(store, cfg)

    url = asyncio.run(svc.publish(gh, "owner", "repo"))
    assert url == "https://github.com/owner/repo/pull/9"

    uploads = [c for c in gh.calls if c[0] == "upload_pr_asset"]
    assert len(uploads) == len(shots), "every CSS-triggered screenshot must be uploaded"

    updates = [c for c in gh.calls if c[0] == "update_pr_body"]
    assert updates, "the draft PR body must carry the visual evidence"
    body = updates[-1][2]
    assert "## 📸 Visual evidence" in body
    assert "](" in body and body.count("![") >= 1, "at least one image link required"
    assert body.startswith("original body")
    assert not any(lvl == "warn" for _, _, lvl in logs)


def test_css_plan_report_md_has_visual_evidence_section(tmp_path, cfg, monkeypatch):
    """AC2 for the CSS branch: report.md lists tab names and file paths."""
    orch, store = _run(tmp_path, cfg, monkeypatch)
    text = write_report(store, orch.run_id, orch.run_dir).read_text(encoding="utf-8")
    assert "## Visual evidence" in text
    for tab in evidence.visual.DEFAULT_TABS:
        assert f"**{tab}**" in text
    shots = evidence.shots_from_events(store.events(orch.run_id))
    for s in shots:
        assert f"`{s['file']}`" in text
