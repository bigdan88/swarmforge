"""Acceptance: dashboard screenshots wired end-to-end into report.md and the draft PR body.

The existing evidence tests cover each module in isolation; the delivery fixtures
write manifest.json directly into run_dir (legacy layout). These tests close the
loop using the layout the orchestrator actually produces
(<run_dir>/evidence/manifest.json): capture (AC1/AC2) -> report section (AC2) ->
PR asset upload + image links (AC1), plus a bullet-proof no-op case (AC4).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from swarmforge import evidence
from swarmforge.db import Store
from swarmforge.github import Issue
from swarmforge.orchestrator import Orchestrator
from swarmforge.report import build_report
from swarmforge.services.publication import PublicationService
from swarmforge.state import RunState


class FakeGH:
    """Minimal GitHubClient stand-in: records uploads and PR body updates."""

    def __init__(self, token="tok"):
        self.token = token
        self.uploads: list[tuple] = []
        self.bodies: list[tuple] = []

    def get_pr_body(self, owner, name, number):
        return "ORIGINAL_BODY"

    def upload_pr_asset(self, owner, name, branch, path):
        self.uploads.append((owner, name, branch, Path(path)))
        return f"https://raw.githubusercontent.com/o/r/{branch}/{Path(path).name}"

    def update_pr_body(self, owner, name, number, body):
        self.bodies.append((owner, name, number, body))


def _make_orch(cfg, tmp_path, monkeypatch, files):
    """Orchestrator with a UI plan and a stubbed capture that writes real PNGs
    into the orchestrator's own evidence/ directory (same layout as production)."""
    store = Store(cfg.db_path)
    orch = Orchestrator(cfg, store, mode="single", echo=None, llm_transport=None)
    orch.run_id = "r-e2e"
    orch.run_dir = tmp_path / "runs" / "r-e2e"
    orch.run_dir.mkdir(parents=True, exist_ok=True)
    orch.sm = SimpleNamespace(state=RunState.APPROVED)
    orch.ws = SimpleNamespace(
        integration=str(tmp_path / "repo"),
        repo=str(tmp_path / "repo"),
        changed_files=[],
    )
    orch.plan = SimpleNamespace(tasks=[SimpleNamespace(expected_files=files)])

    def fake_collect(repo_dir, out_dir, *, tabs=None):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        shots = []
        for t in tabs or []:
            p = out / f"{t}-light.png"
            p.write_bytes(b"\x89PNG fake")
            shots.append({"tab": t, "theme": "light", "file": str(p)})
        evidence.write_manifest(out, shots)
        return shots

    monkeypatch.setattr(evidence, "collect", fake_collect)
    return orch, store


def _delivery_store(tmp_path, run_dir) -> Store:
    s = Store(tmp_path / "delivery.db")
    s.create_run(
        "r-e2e",
        issue_title="T",
        state="COMPLETED",
        mode="swarm",
        run_dir=str(run_dir),
        base_branch="main",
        integration_branch="sf/result",
        config_json="{}",
    )
    return s


def _service(store, run_dir):
    logs: list[tuple] = []
    svc = PublicationService(
        cfg=None,
        store=store,
        run_id="r-e2e",
        issue=Issue("T", "body"),
        ws=None,
        llm=None,
        log=lambda kind, msg, level="info": logs.append((kind, msg, level)),
        update_base=None,
        push=lambda *a, **k: None,
    )
    svc._logs = logs
    return svc


async def test_ui_plan_evidence_flows_into_report_and_pr_body(
    cfg, tmp_path, monkeypatch
):
    """AC1 + AC2 end-to-end: dashboard.py plan -> captured PNGs -> report.md
    '## Visual evidence' lists tab names + file paths -> PublicationService
    uploads each PNG via upload_pr_asset and appends image links to the PR body."""
    orch, _ = _make_orch(
        cfg, tmp_path, monkeypatch, ["src/swarmforge/dashboard.py", "static/style.css"]
    )
    await orch._collect_evidence()
    assert orch._evidence_shots, "UI plan must produce screenshots"
    manifest = orch.run_dir / "evidence" / "manifest.json"
    assert manifest.exists(), "collector must use the evidence/ sub-directory layout"

    # AC2: the report reads the orchestrator's actual manifest location.
    dstore = _delivery_store(tmp_path, orch.run_dir)
    text = build_report(dstore, "r-e2e")
    assert "## Visual evidence" in text
    for s in orch._evidence_shots:
        assert f"- {s['tab']} (light): {s['file']}" in text

    # AC1: publication uploads the PNGs and attaches image links to the body.
    gh = FakeGH()
    svc = _service(dstore, orch.run_dir)
    await svc._attach_evidence(gh, "o", "r", "https://github.com/o/r/pull/7")
    assert gh.uploads, "each captured PNG must be uploaded as a PR asset"
    assert {p.name for _, _, _, p in gh.uploads} == {
        Path(s["file"]).name for s in orch._evidence_shots
    }
    assert {b for _, _, b, _ in gh.uploads} == {"sf/result"}
    assert gh.bodies, "PR body must be updated with the evidence section"
    body = gh.bodies[0][3]
    assert "ORIGINAL_BODY" in body  # appended to the live body, not rebuilt
    assert "📸 Visual evidence" in body
    for _, _, b, p in gh.uploads:
        assert f"(https://raw.githubusercontent.com/o/r/{b}/{p.name})" in body
    assert any(k == "evidence" and "attached" in m for k, m, lvl in svc._logs)
    dstore.close()


async def test_attach_evidence_skips_missing_files_without_error(tmp_path):
    """AC4 bullet-proof delivery: a manifest referencing deleted PNGs produces no
    upload, no body update and no exception (publication continues)."""
    run_dir = tmp_path / "run"
    (run_dir / "evidence").mkdir(parents=True)
    gone = run_dir / "evidence" / "work-light.png"
    evidence.write_manifest(
        run_dir / "evidence", [{"tab": "work", "theme": "light", "file": str(gone)}]
    )
    store = _delivery_store(tmp_path, run_dir)
    gh = FakeGH()
    svc = _service(store, run_dir)
    await svc._attach_evidence(
        gh, "o", "r", "https://github.com/o/r/pull/7"
    )  # must not raise
    assert not gh.uploads and not gh.bodies
    store.close()


async def test_attach_evidence_patches_live_pr_body_and_tolerates_bad_uploads(tmp_path):
    """Reviewer fix: the evidence section must be appended to the PR's *live* body
    (manual edits preserved, never rebuilt from scratch), and a single failed
    asset upload must not drop the remaining screenshots or the section."""
    run_dir = tmp_path / "run"
    out = run_dir / "evidence"
    out.mkdir(parents=True)
    shots = []
    for t in ("work", "review"):
        p = out / f"{t}-light.png"
        p.write_bytes(b"\x89PNG fake")
        shots.append({"tab": t, "theme": "light", "file": str(p)})
    evidence.write_manifest(out, shots)
    store = _delivery_store(tmp_path, run_dir)

    class LiveGH(FakeGH):
        def __init__(self):
            super().__init__()
            self.fetched = []

        def get_pr_body(self, owner, name, number):
            self.fetched.append((owner, name, number))
            return "MANUAL EDIT: please keep this line."

        def upload_pr_asset(self, owner, name, number, path):
            if Path(path).name == "work-light.png":
                raise RuntimeError("boom")
            return super().upload_pr_asset(owner, name, number, path)

    gh = LiveGH()
    svc = _service(store, run_dir)
    await svc._attach_evidence(gh, "o", "r", "https://github.com/o/r/pull/7")
    assert gh.fetched == [("o", "r", 7)], "the live PR body must be fetched before patching"
    assert gh.bodies, "the evidence section must still be appended"
    body = gh.bodies[0][3]
    assert "MANUAL EDIT: please keep this line." in body, "manual edits must be preserved"
    assert "📸 Visual evidence" in body
    assert "review" in body and "work-light.png" not in body
    assert any(k == "evidence" and lvl == "warn" for k, m, lvl in svc._logs), (
        "the failed upload must be logged as a warning, not raise"
    )
    store.close()
