"""Visual evidence delivery: report.md section and PR body screenshot attachment."""

from __future__ import annotations

from pathlib import Path

from swarmforge import evidence
from swarmforge.db import Store
from swarmforge.github import Issue
from swarmforge.report import build_report
from swarmforge.services.publication import PublicationService


class FakeGH:
    """Minimal GitHubClient stand-in: records uploads and PR body updates."""

    def __init__(self, token="tok", upload_error=None):
        self.token = token
        self.upload_error = upload_error
        self.uploads = []  # (owner, name, number, path)
        self.bodies = []  # (owner, name, number, body)
        self.upload_results = []  # asset url per upload call

    def create_draft_pr(self, owner, name, head, base, title, body):
        return "https://github.com/o/r/pull/7"

    def upload_pr_asset(self, owner, name, number, path):
        if self.upload_error:
            raise self.upload_error
        self.uploads.append((owner, name, number, Path(path)))
        url = f"https://camo.example.com/{path.name}"
        self.upload_results.append(url)
        return url

    def update_pr_body(self, owner, name, number, body):
        self.bodies.append((owner, name, number, body))


def make_service(tmp_path, gh, run_dir, with_evidence=True):
    s = Store(tmp_path / "state.db")
    s.create_run(
        "r",
        issue_title="T",
        state="COMPLETED",
        mode="swarm",
        run_dir=str(run_dir),
        base_branch="main",
        integration_branch="sf/result",
        config_json="{}",
        plan_json=None,
    )
    if with_evidence:
        (run_dir / "shots").mkdir(parents=True, exist_ok=True)
        png = run_dir / "shots" / "dashboard-light.png"
        png.write_bytes(b"\x89PNG fake")
        evidence.write_manifest(
            run_dir, [{"tab": "Dashboard", "theme": "light", "file": str(png)}]
        )
    logs = []
    svc = PublicationService(
        cfg=None,
        store=s,
        run_id="r",
        issue=Issue("T", "body"),
        ws=None,
        llm=None,
        log=lambda kind, msg, level="info": logs.append((kind, msg, level)),
        update_base=None,
        push=lambda *a, **k: None,
    )
    svc._gh = gh
    svc._logs = logs
    return svc


def test_report_lists_visual_evidence(tmp_path):
    run_dir = tmp_path / "run"
    s = Store(tmp_path / "state.db")
    s.create_run(
        "r",
        issue_title="T",
        state="COMPLETED",
        mode="swarm",
        run_dir=str(run_dir),
        config_json="{}",
    )
    (run_dir / "shots").mkdir(parents=True)
    png = run_dir / "shots" / "review-dark.png"
    png.write_bytes(b"png")
    evidence.write_manifest(
        run_dir, [{"tab": "Review", "theme": "dark", "file": str(png)}]
    )
    text = build_report(s, "r")
    assert "## Visual evidence" in text
    assert "- Review (dark): " + str(png) in text
    s.close()


def test_report_visual_evidence_none_when_manifest_empty(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    s = Store(tmp_path / "state.db")
    s.create_run(
        "r",
        issue_title="T",
        state="COMPLETED",
        mode="swarm",
        run_dir=str(run_dir),
        config_json="{}",
    )
    text = build_report(s, "r")
    assert "## Visual evidence" in text
    assert "none (no UI files in the plan)" in text
    s.close()


def test_report_never_crashes_without_run_dir(tmp_path):
    s = Store(tmp_path / "state.db")
    s.create_run(
        "r", issue_title="T", state="COMPLETED", mode="swarm", config_json="{}"
    )
    text = build_report(s, "r")
    assert "## Visual evidence" in text
    s.close()


async def test_publish_attaches_screenshots_to_pr_body(tmp_path):
    run_dir = tmp_path / "run"
    gh = FakeGH()
    svc = make_service(tmp_path, gh, run_dir)
    svc.ws = type(
        "W",
        (),
        {"repo": tmp_path, "integration_branch": "sf/result", "base_branch": "main"},
    )()
    svc.cfg = type(
        "C",
        (),
        {
            "github": type(
                "G", (), {"require_push_approval": False, "branch_prefix": "sf/"}
            )()
        },
    )()
    url = await svc.publish(gh, "o", "r")
    assert url == "https://github.com/o/r/pull/7"
    assert gh.uploads and gh.uploads[0][:3] == ("o", "r", 7)
    assert gh.bodies, "PR body must be updated with the evidence section"
    body = gh.bodies[0][3]
    assert "📸 Visual evidence" in body
    assert "![Dashboard — light](https://camo.example.com/dashboard-light.png)" in body
    assert svc.store.get_run("r")["pr_url"] == url


async def test_publish_without_token_warns_and_skips_upload(tmp_path):
    run_dir = tmp_path / "run"
    gh = FakeGH(token=None)
    svc = make_service(tmp_path, gh, run_dir)
    svc.ws = type(
        "W",
        (),
        {"repo": tmp_path, "integration_branch": "sf/result", "base_branch": "main"},
    )()
    svc.cfg = type(
        "C",
        (),
        {
            "github": type(
                "G", (), {"require_push_approval": False, "branch_prefix": "sf/"}
            )()
        },
    )()
    url = await svc.publish(gh, "o", "r")
    assert url == "https://github.com/o/r/pull/7"  # PR still created
    assert not gh.uploads and not gh.bodies  # no upload attempted
    assert any(k == "evidence" and lvl == "warn" for k, m, lvl in svc._logs)


async def test_publish_survives_upload_error(tmp_path):
    from swarmforge.github import GitHubError

    run_dir = tmp_path / "run"
    gh = FakeGH(upload_error=GitHubError("boom"))
    svc = make_service(tmp_path, gh, run_dir)
    svc.ws = type(
        "W",
        (),
        {"repo": tmp_path, "integration_branch": "sf/result", "base_branch": "main"},
    )()
    svc.cfg = type(
        "C",
        (),
        {
            "github": type(
                "G", (), {"require_push_approval": False, "branch_prefix": "sf/"}
            )()
        },
    )()
    url = await svc.publish(gh, "o", "r")
    assert url == "https://github.com/o/r/pull/7"  # AC4: run completes
    assert any(
        k == "evidence" and lvl == "warn" and "GitHubError" in m
        for k, m, lvl in svc._logs
    )


async def test_publish_without_evidence_makes_no_uploads(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    gh = FakeGH()
    svc = make_service(tmp_path, gh, run_dir, with_evidence=False)
    svc.ws = type(
        "W",
        (),
        {"repo": tmp_path, "integration_branch": "sf/result", "base_branch": "main"},
    )()
    svc.cfg = type(
        "C",
        (),
        {
            "github": type(
                "G", (), {"require_push_approval": False, "branch_prefix": "sf/"}
            )()
        },
    )()
    url = await svc.publish(gh, "o", "r")
    assert url == "https://github.com/o/r/pull/7"
    assert not gh.uploads and not gh.bodies
