"""End-to-end acceptance tests for auto-attached dashboard screenshots.

The per-module wiring is covered by ``test_evidence.py``,
``test_evidence_orchestrator.py`` and ``test_acceptance_visual_evidence.py``.
These tests close the remaining gaps by chaining the whole pipeline:

* AC1: UI-touching plan -> ``_collect_evidence`` records the shots ->
  ``PublicationService.publish`` uploads each PNG via ``upload_pr_asset`` and
  updates the draft PR body with at least one image link.
* AC2: the ``report.md`` file written by ``write_report`` contains the
  '## Visual evidence' section with tab names and screenshot file paths.
* AC3: a plan that only touches state.py/llm.py never calls
  ``screenshot_tabs`` and its report has no Visual evidence section.
* AC4: capture failures never change the run outcome (``_collect_evidence``
  must swallow them and log a warning) and asset-upload failures never break
  publication (the draft PR is still created).
* AC5: for FAILED runs the collected evidence contains the failures/review tab
  screenshot and report.md still carries the section with the unresolved
  findings note.
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from swarmforge import evidence
from swarmforge.db import Store
from swarmforge.github import GitHubError, Issue
from swarmforge.orchestrator import Orchestrator
from swarmforge.report import build_pr_body, build_report, write_report
from swarmforge.services.publication import PublicationService


@dataclass
class _Plan:
    summary: str = "touch the dashboard"
    tasks: list = field(default_factory=list)
    acceptance_criteria: list = field(default_factory=list)


class _FakeWorkspace:
    def __init__(self, repo, changed=("src/app/dashboard.py",)):
        self.repo = repo
        self._changed = list(changed)

    def changed_files(self):
        return list(self._changed)


class PlaywrightLikeError(Exception):
    """Mimics ``playwright._impl._errors.Error`` (raised when the browser
    binary is missing): a plain Exception, not OSError/RuntimeError."""


def _shot(tmp_path: Path, tab: str, theme: str = "light") -> dict:
    png = tmp_path / "screenshots" / f"{tab}-{theme}.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return {"tab": tab, "theme": theme, "file": str(png)}


def _orchestrator(
    tmp_path, cfg, *, state="APPROVED", changed=("src/app/dashboard.py",)
):
    store = Store(tmp_path / "db")
    orch = Orchestrator(cfg, store, mode="swarm", echo=None)
    orch.run_id = "r-pipe"
    orch.run_dir = tmp_path / "runs" / orch.run_id
    orch.run_dir.mkdir(parents=True)
    orch.ws = _FakeWorkspace(str(tmp_path), changed=changed)
    orch.plan = _Plan()
    store.create_run(orch.run_id, mode="swarm", run_dir=str(orch.run_dir))
    if state:
        store.update_run(orch.run_id, state=state)
    return orch, store


def _fake_tabs(shots, seen=None):
    def fake(repo_dir, out_dir, files, *, failed=False):
        if seen is not None:
            seen["failed"] = failed
        return list(shots)

    return fake


def _capture_respecting_failed(tmp_path, seen=None):
    """Stand-in for visual.capture_tabs: returns one PNG per requested tab."""

    def screenshot_tabs(repo_dir, out_dir, files, *, failed=False):
        if seen is not None:
            seen["failed"] = failed
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


class _StubGH:
    def __init__(self, token="tok", fail_upload=False):
        self.token = token
        self.calls = []
        self.fail_upload = fail_upload

    def create_draft_pr(self, owner, name, head, base, title, body):
        self.calls.append(("create_draft_pr", head, base, title, body))
        return "https://github.com/owner/repo/pull/7"

    def upload_pr_asset(self, owner, name, number, path):
        self.calls.append(("upload_pr_asset", number, str(path)))
        if self.fail_upload:
            raise GitHubError("uploading asset failed: HTTP 500 boom")
        Path(path).read_bytes()  # real uploads read the file
        return f"https://github.com/owner/repo/releases/download/tmp/{Path(path).name}"

    def get_pr_body(self, owner, name, number):
        self.calls.append(("get_pr_body", number))
        return "original body"

    def update_pr_body(self, owner, name, number, body):
        self.calls.append(("update_pr_body", number, body))

    def add_labels(self, *a):
        self.calls.append(("add_labels", *a))

    def comment(self, *a):
        self.calls.append(("comment", *a))


def _service(store, cfg, shots_gh=None):
    cfg.github.require_push_approval = False
    logs = []
    svc = PublicationService(
        cfg=cfg,
        store=store,
        run_id="r-pipe",
        issue=Issue("t", "b", url="https://github.com/o/r/issues/1"),
        ws=SimpleNamespace(
            repo=str(Path("/tmp/x")), integration_branch="sf/x", base_branch="main"
        ),
        llm=None,
        log=lambda kind, msg, **kw: logs.append((kind, msg, kw.get("level", "info"))),
        update_base=None,
        push=lambda *a: None,
    )
    return svc, logs


# ------------------------------------------------------------------ AC1


def test_ui_plan_evidence_reaches_draft_pr_body(tmp_path, cfg, monkeypatch):
    """plan touches dashboard.py -> assets uploaded -> PR body gets image links."""
    shots = [_shot(tmp_path, "work"), _shot(tmp_path, "chat")]
    monkeypatch.setattr(evidence, "screenshot_tabs", _fake_tabs(shots))
    orch, store = _orchestrator(tmp_path, cfg)
    asyncio.run(orch._collect_evidence())

    gh = _StubGH()
    svc, logs = _service(store, cfg)
    url = asyncio.run(svc.publish(gh, "owner", "repo"))
    assert url == "https://github.com/owner/repo/pull/7"

    uploads = [c for c in gh.calls if c[0] == "upload_pr_asset"]
    assert len(uploads) == len(shots), "every captured screenshot must be uploaded"
    assert {c[2] for c in uploads} == {s["file"] for s in shots}

    updates = [c for c in gh.calls if c[0] == "update_pr_body"]
    assert updates, "the draft PR body must be updated with the evidence"
    body = updates[-1][2]
    assert body.startswith("original body")
    assert "![work (light)](" in body and "![chat (light)](" in body
    assert "## 📸 Visual evidence" in body

    # the PR body built before the update also survives; nothing crashed the run
    assert not any("skipped" in m for _, m, _ in logs)


def test_pr_body_built_from_store_shots(tmp_path, cfg, monkeypatch):
    """build_pr_body keeps working with evidence events recorded (no crash)."""
    shots = [_shot(tmp_path, "work")]
    monkeypatch.setattr(evidence, "screenshot_tabs", _fake_tabs(shots))
    orch, store = _orchestrator(tmp_path, cfg)
    asyncio.run(orch._collect_evidence())
    body = build_pr_body(store, orch.run_id)
    assert isinstance(body, str) and body


# ------------------------------------------------------------------ AC2


def test_report_md_file_contains_visual_evidence_section(tmp_path, cfg, monkeypatch):
    """report.md gained a '## Visual evidence' section with tab names + paths."""
    shots = [_shot(tmp_path, "work"), _shot(tmp_path, "chat", "dark")]
    monkeypatch.setattr(evidence, "screenshot_tabs", _fake_tabs(shots))
    orch, store = _orchestrator(tmp_path, cfg, state="COMPLETED")
    asyncio.run(orch._collect_evidence())

    path = write_report(store, orch.run_id, orch.run_dir)
    text = path.read_text(encoding="utf-8")
    assert path.name == "report.md"
    assert "## Visual evidence" in text
    assert "**work** (light)" in text
    assert "**chat** (dark)" in text
    for s in shots:
        assert f"`{s['file']}`" in text, "screenshot file paths must be listed"


def test_report_without_evidence_has_no_visual_section(tmp_path, cfg, monkeypatch):
    def never(*a, **k):
        raise AssertionError("screenshot_tabs must not be called")

    monkeypatch.setattr(evidence, "screenshot_tabs", never)
    orch, store = _orchestrator(tmp_path, cfg, state="COMPLETED", changed=())
    asyncio.run(orch._collect_evidence())  # non-UI plan: short-circuits
    text = write_report(store, orch.run_id, orch.run_dir).read_text(encoding="utf-8")
    assert "## Visual evidence" not in text


# ------------------------------------------------------------------ AC3


def test_state_llm_only_plan_takes_no_screenshots(tmp_path, cfg, monkeypatch):
    """A plan touching only non-UI modules (state.py, llm.py) short-circuits."""
    called = []
    monkeypatch.setattr(
        evidence,
        "screenshot_tabs",
        lambda *a, **k: called.append(a) or [],
    )
    files = ["src/swarmforge/state.py", "src/swarmforge/llm.py"]
    orch, store = _orchestrator(tmp_path, cfg, changed=files)
    orch.plan = _Plan(tasks=[{"expected_files": files}])
    asyncio.run(orch._collect_evidence())
    assert called == [], "no screenshots may be taken for non-UI plans"
    assert evidence.shots_from_events(store.events(orch.run_id)) == []
    text = write_report(store, orch.run_id, orch.run_dir).read_text(encoding="utf-8")
    assert "## Visual evidence" not in text


# ------------------------------------------------------------------ AC4


def test_unexpected_capture_error_never_changes_run_outcome(tmp_path, cfg, monkeypatch):
    """AC4: *any* screenshot capture failure must be swallowed and logged, never
    propagate into the pipeline (an escaping exception flips the run to FAILED)."""

    def boom(*a, **k):
        raise PlaywrightLikeError(
            "Executable doesn't exist; run `playwright install chromium`"
        )

    monkeypatch.setattr(evidence, "screenshot_tabs", boom)
    orch, store = _orchestrator(tmp_path, cfg, state="APPROVED")
    # must not raise: on the success path _collect_evidence runs inside the
    # pipeline, so an escaping exception would abort the run as FAILED.
    asyncio.run(orch._collect_evidence())
    warns = [
        e
        for e in store.events(orch.run_id)
        if e["kind"] == "evidence" and e["level"] == "warn"
    ]
    assert warns, "the skipped capture must be logged as a warning"


def test_publish_survives_asset_upload_failure(tmp_path, cfg, monkeypatch):
    """A failing GitHub upload degrades to a warning; the draft PR still lands."""
    shots = [_shot(tmp_path, "work")]
    monkeypatch.setattr(evidence, "screenshot_tabs", _fake_tabs(shots))
    orch, store = _orchestrator(tmp_path, cfg)
    asyncio.run(orch._collect_evidence())

    gh = _StubGH(fail_upload=True)
    svc, logs = _service(store, cfg)
    url = asyncio.run(svc.publish(gh, "owner", "repo"))
    assert url == "https://github.com/owner/repo/pull/7"
    assert any("screenshot upload skipped" in m and lvl == "warn" for _, m, lvl in logs)
    assert not any(c[0] == "update_pr_body" for c in gh.calls)


def test_publish_without_token_warns_and_completes(tmp_path, cfg, monkeypatch):
    """No token: upload skipped with a warning; PR/run still complete."""
    shots = [_shot(tmp_path, "work")]
    monkeypatch.setattr(evidence, "screenshot_tabs", _fake_tabs(shots))
    orch, store = _orchestrator(tmp_path, cfg)
    asyncio.run(orch._collect_evidence())

    gh = _StubGH(token=None)
    svc, logs = _service(store, cfg)
    url = asyncio.run(svc.publish(gh, "owner", "repo"))
    assert url
    assert any("no GitHub token" in m and lvl == "warn" for _, m, lvl in logs)
    assert not any(c[0] == "upload_pr_asset" for c in gh.calls)


# ------------------------------------------------------------------ AC5


def test_failed_run_report_md_shows_failures_tab(tmp_path, cfg, monkeypatch):
    """FAILED run: evidence set includes the failures tab and report.md keeps
    the Visual evidence section with the unresolved-findings note."""
    seen = {}
    monkeypatch.setattr(
        evidence, "screenshot_tabs", _capture_respecting_failed(tmp_path, seen)
    )
    orch, store = _orchestrator(tmp_path, cfg, state="FAILED")
    asyncio.run(orch._collect_evidence())
    assert seen["failed"] is True

    shots = evidence.shots_from_events(store.events(orch.run_id))
    assert any(s["tab"] == "failures" for s in shots), (
        "failed runs must capture the failures/review tab"
    )

    text = write_report(store, orch.run_id, orch.run_dir).read_text(encoding="utf-8")
    assert "## Visual evidence" in text
    assert "**failures**" in text
    assert "unresolved" in text

    built = build_report(store, orch.run_id)
    assert "## Visual evidence" in built


def test_completed_run_does_not_add_failures_tab(tmp_path, cfg, monkeypatch):
    """The failures tab is only *added* for failed runs: a single-tab plan on a
    run that completed must not drag the failures/review page into the evidence."""
    seen = {}
    monkeypatch.setattr(
        evidence, "screenshot_tabs", _capture_respecting_failed(tmp_path, seen)
    )
    changed = ["src/app/dashboard_pages/work.py"]
    orch, store = _orchestrator(tmp_path, cfg, state="COMPLETED", changed=changed)
    asyncio.run(orch._collect_evidence())
    assert seen["failed"] is False
    shots = evidence.shots_from_events(store.events(orch.run_id))
    assert [s["tab"] for s in shots] == ["work"]

    # and the same plan on a FAILED run does include it (AC5, precise boundary)
    orch2, store2 = _orchestrator(tmp_path / "f", cfg, state="FAILED", changed=changed)
    asyncio.run(orch2._collect_evidence())
    assert seen["failed"] is True
    shots2 = evidence.shots_from_events(store2.events(orch2.run_id))
    assert [s["tab"] for s in shots2] == ["work", "failures"]
