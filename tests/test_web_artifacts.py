"""Retained artifact access uses disposable files, never live runs or repositories."""
import os
import struct
import zlib

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from swarmforge.db import Store
from swarmforge.webapp.app import create_app
from swarmforge.webapp.artifacts import ArtifactReader, ArtifactUnavailable, MAX_REPORT_BYTES


def png(width=1, height=1):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\xff\x80\x3c")) + chunk(b"IEND", b""))


@pytest.fixture
def retained(tmp_path):
    db = tmp_path / "history.db"
    root = tmp_path / "retained"
    folder = root / "run-a"
    shots = folder / "artifacts" / "visual"
    shots.mkdir(parents=True)
    (folder / "report.md").write_text("# Retained report\n\n<script>plain text</script>\n", encoding="utf-8")
    (shots / "c1-home-dark.png").write_bytes(png())
    store = Store(db)
    store.create_run("run-a", repo="fixture/app", run_dir=str(tmp_path / "private"))
    task = store.queue_add("Inspect evidence", "", "fixture/app", run_id="run-a", status="running")
    store.close()
    return db, root, folder, f"task-{task}"


def test_lists_and_reads_only_retained_report_and_visual_png(retained, tmp_path):
    db, root, folder, item = retained
    private = tmp_path / "private"
    private.mkdir()
    (private / "report.md").write_text("must not follow database paths", encoding="utf-8")
    before = db.read_bytes()
    with TestClient(create_app(db, tmp_path, runs_root=root), base_url="http://127.0.0.1") as client:
        listing = client.get(f"/api/v1/work/{item}/artifacts?run_id=run-a")
        assert listing.status_code == 200
        assert listing.json()["report_status"] == "available"
        assert listing.json()["screenshots"][0]["name"] == "c1-home-dark.png"
        assert str(root) not in listing.text and str(private) not in listing.text
        report = client.get(f"/api/v1/work/{item}/report?run_id=run-a")
        assert report.json()["text"] == (folder / "report.md").read_bytes().decode("utf-8")
        assert report.json()["item_id"] == item and report.json()["run_id"] == "run-a"
        image = client.get(f"/api/v1/work/{item}/screenshots/c1-home-dark.png?run_id=run-a")
        assert image.content == png()
        assert image.headers["content-type"] == "image/png"
        assert image.headers["cross-origin-resource-policy"] == "same-origin"
        for response in (listing, report, image):
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
        assert client.get(f"/api/v1/work/{item}/report?run_id=other-run").status_code == 409
        assert client.get(f"/api/v1/work/{item}/screenshots/c1-home-dark.png?run_id=other-run").status_code == 409
        assert client.get("/api/v1/work/run-run-a/report").status_code == 404  # Attached run has one public ID.
        assert client.post(f"/api/v1/work/{item}/report").status_code == 405
        assert client.get(f"/api/v1/work/{item}/report", headers={"origin": "https://elsewhere.test"}).status_code == 403
        assert client.get(f"/api/v1/work/{item}/screenshots/c1-home-dark.png", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert db.read_bytes() == before


def test_report_and_directory_reads_are_bounded(retained):
    _, root, folder, _ = retained
    (folder / "report.md").write_bytes(b"x" * (MAX_REPORT_BYTES + 500))
    shots = folder / "artifacts" / "visual"
    for cycle in range(2, 40):
        (shots / f"c{cycle}-home-dark.png").write_bytes(png())
    (shots / "c40-invalid-light.png").write_text("<html>not a PNG</html>", encoding="utf-8")
    (shots / "c41-large-light.png").write_bytes(png(100000, 100000))
    (shots / "c42-tall-light.png").write_bytes(png(1, 50001))
    reader = ArtifactReader(root)
    report = reader.report("run-a")
    assert report["truncated"] and len(report["text"]) == MAX_REPORT_BYTES
    listing = reader.listing("run-a")
    assert listing["report_truncated"] and listing["screenshots_truncated"]
    assert listing["screenshots_unavailable"]
    assert len(listing["screenshots"]) <= 24
    for invalid in ("c40-invalid-light.png", "c41-large-light.png", "c42-tall-light.png", "../report.md", "c1-home-dark.svg", "c1-home-dark.png:stream"):
        with pytest.raises(ArtifactUnavailable):
            reader.screenshot("run-a", invalid)
    with pytest.raises(ArtifactUnavailable):
        reader.report("../private")


def test_missing_runs_and_files_are_explicit_and_never_created(retained, tmp_path):
    db, root, folder, item = retained
    (folder / "report.md").unlink()
    (folder / "artifacts" / "visual" / "c1-home-dark.png").unlink()
    with TestClient(create_app(db, tmp_path, runs_root=root), base_url="http://127.0.0.1") as client:
        listing = client.get(f"/api/v1/work/{item}/artifacts").json()
        assert listing["report_status"] == "missing" and listing["screenshots"] == []
        assert not listing["screenshots_unavailable"]
        assert client.get(f"/api/v1/work/{item}/report").status_code == 404
    missing = tmp_path / "not-created"
    assert ArtifactReader(missing).listing("run-a")["report_status"] == "missing"
    assert not missing.exists()


def test_symlinks_do_not_escape_the_retained_run(retained, tmp_path):
    db, root, folder, item = retained
    secret = tmp_path / "secret.md"
    secret.write_text("outside private evidence", encoding="utf-8")
    (folder / "report.md").unlink()
    try:
        (folder / "report.md").symlink_to(secret)
    except OSError:
        pytest.skip("Host does not permit creating test symlinks")
    with TestClient(create_app(db, tmp_path, runs_root=root), base_url="http://127.0.0.1") as client:
        assert client.get(f"/api/v1/work/{item}/artifacts").json()["report_status"] == "unavailable"
        response = client.get(f"/api/v1/work/{item}/report")
        assert response.status_code == 404
        assert str(secret) not in response.text and "outside private" not in response.text
    # Also reject a linked directory even when its target is inside the root.
    link = root / "linked-run"
    link.symlink_to(folder, target_is_directory=True)
    assert ArtifactReader(root).listing("linked-run")["report_status"] == "unavailable"


def test_hard_links_and_permission_errors_do_not_disclose_files(retained, tmp_path, monkeypatch):
    db, root, folder, item = retained
    secret = tmp_path / "secret.md"
    secret.write_text("private linked bytes", encoding="utf-8")
    report = folder / "report.md"
    report.unlink()
    os.link(secret, report)
    reader = ArtifactReader(root)
    assert reader.listing("run-a")["report_status"] == "unavailable"
    with pytest.raises(ArtifactUnavailable):
        reader.report("run-a")
    report.unlink()
    report.write_text("retained", encoding="utf-8")
    original = os.open
    def denied(path, *args, **kwargs):
        if path == report:
            raise PermissionError("private filesystem detail")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", denied)
    with TestClient(create_app(db, tmp_path, runs_root=root), base_url="http://127.0.0.1") as client:
        response = client.get(f"/api/v1/work/{item}/report")
        assert response.status_code == 404
        assert "private filesystem detail" not in response.text


def test_windows_reparse_points_are_rejected_before_file_open(retained, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    _, root, folder, _ = retained
    original = Path.lstat
    def junction(path):
        if path == folder:
            info = original(path)
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return original(path)
    monkeypatch.setattr(Path, "lstat", junction)
    reader = ArtifactReader(root)
    assert reader.listing("run-a")["report_status"] == "unavailable"
    with pytest.raises(ArtifactUnavailable):
        reader.report("run-a")
