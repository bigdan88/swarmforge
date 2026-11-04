"""Acceptance tests for visual evidence in report.md and PR bodies."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarmforge.db import Store
from swarmforge.github import Issue
from swarmforge.report import build_report
from swarmforge.services.publication import PublicationService


def make_store(tmp_path: Path) -> Store:
    return Store(tmp_path / "swarmforge.db")


def make_run(store: Store, run_id: str = "r1", state: str = "COMPLETED") -> None:
    store.create_run(
        run_id,
        state=state,
        issue_title="Add dashboard tab",
        issue_body="Please add a tab",
        issue_number=42,
        repo="owner/repo",
        mode="issue",
        config_json=json.dumps({"roles": {"planner": {"model": "m", "provider": "p"}}}),
        plan_json=json.dumps({"summary": "do it", "tasks": []}),
        base_branch="main",
        integration_branch="sf/1",
        repair_cycle=0,
    )


def record_shots(store: Store, run_id: str, shots: list[dict]) -> None:
    store.event(run_id, "evidence", "captured screenshots", data={"shots": shots})


def test_report_contains_visual_evidence_section(tmp_path):
    s = make_store(tmp_path)
    make_run(s)
    record_shots(
        s,
        "r1",
        [{"tab": "failures", "theme": "light", "file": "/tmp/failures-light.png"}],
    )
    text = build_report(s, "r1")
    assert "## Visual evidence" in text
    assert "failures" in text
    assert "/tmp/failures-light.png" in text


def test_report_has_no_visual_evidence_section_without_event(tmp_path):
    s = make_store(tmp_path)
    make_run(s)
    assert "## Visual evidence" not in build_report(s, "r1")


def test_report_marks_failed_runs(tmp_path):
    s = make_store(tmp_path)
    make_run(s, state="FAILED")
    record_shots(s, "r1", [{"tab": "failures", "theme": "light", "file": "/tmp/f.png"}])
    text = build_report(s, "r1")
    assert "## Visual evidence" in text
    assert "unresolved" in text


class StubGH:
    """Fake GitHubClient recording asset uploads and body updates."""

    def __init__(self, token: str = "tok", fail_upload: bool = False):
        self.token = token
        self.fail_upload = fail_upload
        self.calls: list[tuple] = []
        self.body = "original body"

    def create_draft_pr(self, owner, name, head, base, title, body):
        self.calls.append(("create_draft_pr", title))
        return f"https://github.com/{owner}/{name}/pull/7"

    def upload_pr_asset(self, owner, name, number, path):
        if self.fail_upload:
            raise OSError("boom")
        self.calls.append(("upload_pr_asset", path))
        return f"https://github.com/{owner}/{name}/uploads/{Path(path).name}"

    def get_pr_body(self, owner, name, number):
        return self.body

    def update_pr_body(self, owner, name, number, body):
        self.calls.append(("update_pr_body", body))
        self.body = body


def make_service(store: Store, run_id: str = "r1") -> SimpleNamespace:
    logs: list[tuple[str, str, str]] = []

    def log(kind, message, level="info"):
        logs.append((kind, message, level))

    obj = SimpleNamespace(
        store=store,
        run_id=run_id,
        issue=Issue("Add dashboard tab", "body", 42),
        ws=SimpleNamespace(
            repo=Path("/tmp/repo"), integration_branch="sf/1", base_branch="main"
        ),
        cfg=SimpleNamespace(
            github=SimpleNamespace(require_push_approval=False, branch_prefix="sf/")
        ),
        push=lambda *a, **k: None,
        log=log,
        logs=logs,
    )
    return obj


@pytest.mark.asyncio
async def test_publish_uploads_screenshots_and_updates_pr_body(tmp_path):
    s = make_store(tmp_path)
    make_run(s)
    record_shots(
        s,
        "r1",
        [{"tab": "failures", "theme": "light", "file": str(tmp_path / "f.png")}],
    )
    gh = StubGH()
    obj = make_service(s)
    url = await PublicationService.publish(obj, gh, "owner", "repo")
    assert url == "https://github.com/owner/repo/pull/7"
    assert any(c[0] == "upload_pr_asset" for c in gh.calls)
    bodies = [c[1] for c in gh.calls if c[0] == "update_pr_body"]
    assert bodies and "![failures (light)](" in bodies[-1]
    assert "## 📸 Visual evidence" in bodies[-1]
    assert s.get_run("r1")["pr_url"] == url


@pytest.mark.asyncio
async def test_publish_skips_upload_without_token(tmp_path):
    s = make_store(tmp_path)
    make_run(s)
    record_shots(
        s,
        "r1",
        [{"tab": "failures", "theme": "light", "file": str(tmp_path / "f.png")}],
    )
    gh = StubGH(token="")
    obj = make_service(s)
    url = await PublicationService.publish(obj, gh, "owner", "repo")
    assert url  # run still completes with a PR
    assert not any(c[0] == "upload_pr_asset" for c in gh.calls)
    assert any("no GitHub token" in m for _, m, _ in obj.logs)


@pytest.mark.asyncio
async def test_publish_logs_warning_when_upload_fails(tmp_path):
    s = make_store(tmp_path)
    make_run(s)
    record_shots(
        s,
        "r1",
        [{"tab": "failures", "theme": "light", "file": str(tmp_path / "f.png")}],
    )
    gh = StubGH(fail_upload=True)
    obj = make_service(s)
    url = await PublicationService.publish(obj, gh, "owner", "repo")
    assert url
    assert any(
        "screenshot upload skipped" in m and lvl == "warn" for _, m, lvl in obj.logs
    )


@pytest.mark.asyncio
async def test_publish_without_shots_creates_pr_normally(tmp_path):
    s = make_store(tmp_path)
    make_run(s)
    gh = StubGH()
    obj = make_service(s)
    url = await PublicationService.publish(obj, gh, "owner", "repo")
    assert url
    assert not any(c[0] == "upload_pr_asset" for c in gh.calls)
