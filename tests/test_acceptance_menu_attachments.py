"""Acceptance tests for the menu-layout + attachments issue.

Complements the developer tests (tests/test_attachments.py, tests/test_dashboard_menu.py)
with the criteria points those files do not directly assert:

* AC1 - the ☰/pill sidebar must not render before the password gate is passed; per-page
  icons, full-width pill CSS (hover + active-page rules) and reachability of every page.
* AC2/AC4 - Orchestrator.execute(..., attachments=...) really appends the rendered
  '## Attached files' section to the issue for *every* issue source (GitHub URL,
  --issue-file, title+body as written by the launcher) and tolerates missing files;
  with no attachments the run starts identically (body untouched).
* AC4 - `swarmforge run --attachment PATH` (repeatable) is expanded to absolute paths
  and forwarded to Orchestrator.execute by cmd_run.
"""
import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

import swarmforge.orchestrator as orch_mod  # noqa: E402
from swarmforge.attachments import MAX_INLINE_BYTES  # noqa: E402
from swarmforge.cli import build_parser, cmd_run  # noqa: E402
from swarmforge.config import parse_config  # noqa: E402
from swarmforge.db import Store  # noqa: E402
from swarmforge.github import Issue  # noqa: E402
from swarmforge.repos import RepoError  # noqa: E402

from tests.conftest import base_raw  # noqa: E402
from tests.test_dashboard_menu import PAGES, _app, _slug, _write_cfg  # noqa: E402

ALL_NAV = {_slug(p) for p in PAGES}


# ------------------------------------------------------------------ AC1: menu
def test_ac1_nav_hidden_until_password_gate_passed(tmp_path, monkeypatch):
    """No nav_* button may render while the password gate is showing; after the
    correct password the full menu appears."""
    _write_cfg(tmp_path, monkeypatch)
    monkeypatch.setenv("SWARMFORGE_DASH_PASSWORD", "s3cret")
    at = _app().run()
    # gate is up: password input shown, script stopped before the sidebar
    assert at.text_input(key="_pw") is not None
    assert not [b for b in at.button if (b.key or "").startswith("nav_")]
    # wrong password: error, still no navigation
    at.text_input(key="_pw").set_value("wrong").run()
    assert any("Wrong password" in e.value for e in at.error)
    assert not [b for b in at.button if (b.key or "").startswith("nav_")]
    # right password: menu renders with the preserved nav_<slug> keys
    at.text_input(key="_pw").set_value("s3cret").run()
    assert not at.exception, [e.value for e in at.exception]
    keys = {(b.key or "") for b in at.button}
    assert ALL_NAV <= keys


def test_ac1_pill_css_icons_and_active_page(tmp_path, monkeypatch):
    """Grouped captions, per-page icons, full-width pill styling with hover, and
    active-page highlighting for the current section."""
    _write_cfg(tmp_path, monkeypatch)
    at = _app().run()
    css = " ".join(m.value for m in at.markdown if "<style>" in m.value)
    assert "st-key-nav_" in css
    assert ":hover" in css                      # hover feedback
    assert "border-radius" in css               # pill shape
    assert "width: 100%" in css                 # full-width buttons
    assert ".st-key-nav_work button" in css     # active-page rule for the Work page
    # every page has its own icon (material icon string on the button proto)
    icons = {}
    for p in PAGES:
        b = at.button(key=_slug(p))
        assert b is not None, f"missing nav button for {p}"
        assert b.proto.icon.startswith(":material/"), (p, b.proto.icon)
        icons[p] = b.proto.icon
    assert len(set(icons.values())) == len(PAGES)  # per-page, not one shared icon
    # section captions (grouping) are present
    caps = {c.value for c in at.caption}
    assert {"WORKFLOW", "INSIGHTS", "SYSTEM"} <= caps


@pytest.mark.parametrize("page", PAGES)
def test_ac1_every_page_reachable_via_nav_keys(tmp_path, monkeypatch, page):
    _write_cfg(tmp_path, monkeypatch)
    at = _app().run()
    at.button(key=_slug(page)).click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["page"] == page


# ----------------------------------------- AC2/AC4: orchestrator issue bodies
class _FakeGH:
    def __init__(self, gh_cfg):
        pass

    def fetch_issue(self, url):
        return Issue(title="Fix CSV export", body="Please fix the CSV export.",
                     number=7, owner="o", repo="r", url=url)


def _prep_orchestrator(tmp_path, monkeypatch):
    """Config + store with everything after issue-loading stubbed out, so execute()
    loads the issue (with the attachment section), persists it and then aborts at the
    (fake) repository preflight - no network, no git clone."""
    monkeypatch.setenv("FAKE_KEY", "test-key")
    cfg = parse_config(base_raw(tmp_path / "work"))
    store = Store(cfg.db_path)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr("swarmforge.postmortem.diagnose", _noop)
    monkeypatch.setattr(orch_mod, "write_report", lambda *a, **k: "report.md")
    monkeypatch.setattr(orch_mod, "write_summary", lambda *a, **k: "summary.md")
    monkeypatch.setattr(orch_mod.Orchestrator, "_capture_knowledge", _noop)

    def _boom(ref, token=None):
        raise RepoError("offline test: stop before cloning")

    monkeypatch.setattr(orch_mod, "preflight", _boom)
    return cfg, store


def _make_attachments(tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_bytes(b"x" * (MAX_INLINE_BYTES + 100))  # too big to inline fully
    small = tmp_path / "small.log"
    small.write_text("hello attachment", encoding="utf-8")
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n tiny")
    missing = tmp_path / "gone.bin"  # deliberately never created
    return txt, small, img, missing


@pytest.mark.parametrize("source", ["github_url", "issue_file", "title_body"])
async def test_ac4_execute_appends_attachment_section_for_every_issue_source(
        tmp_path, monkeypatch, source):
    cfg, store = _prep_orchestrator(tmp_path, monkeypatch)
    txt, small, img, missing = _make_attachments(tmp_path)
    attachments = [str(txt), str(small), str(img), str(missing)]
    if source == "github_url":
        monkeypatch.setattr(orch_mod, "GitHubClient", _FakeGH)
        kwargs = dict(issue_url="https://github.com/o/r/issues/7")
        original = "Please fix the CSV export."
    else:
        f = tmp_path / "issue.md"
        if source == "issue_file":
            f.write_text("# Local task\n\nbody from the issue file\n", encoding="utf-8")
        else:  # exactly what launcher.start_run writes for title+body from the dashboard
            f.write_text("# Launcher task\n\nbody typed in the dashboard\n", encoding="utf-8")
        kwargs = dict(issue_file=str(f), repo="o/r")
        original = f.read_text(encoding="utf-8").split("\n\n", 1)[1].strip()

    o = orch_mod.Orchestrator(cfg, store, echo=None)
    run_id = await o.execute(**kwargs, attachments=attachments)
    row = store.get_run(run_id)
    body = row["issue_body"]
    # the section is *appended* to the original issue text, as markdown
    assert "## Attached files" in body
    assert "hello attachment" in body                      # small log file inlined
    assert str(img) in body                                # image listed with saved path
    assert "gone.bin" in body                              # missing file tolerated
    assert len(body) >= len(original) and original.rstrip() in body
    # the run was aborted at the preflight stub, not by an attachment crash
    assert row["state"] == "BLOCKED"


@pytest.mark.parametrize("source", ["github_url", "issue_file", "title_body"])
async def test_ac2_run_starts_identically_without_attachments(tmp_path, monkeypatch, source):
    cfg, store = _prep_orchestrator(tmp_path, monkeypatch)
    if source == "github_url":
        monkeypatch.setattr(orch_mod, "GitHubClient", _FakeGH)
        kwargs = dict(issue_url="https://github.com/o/r/issues/7")
    else:
        f = tmp_path / "issue.md"
        f.write_text("# Plain task\n\nno files here\n", encoding="utf-8")
        kwargs = dict(issue_file=str(f), repo="o/r")
    o = orch_mod.Orchestrator(cfg, store, echo=None)
    run_id = await o.execute(**kwargs)  # no attachments argument at all
    row = store.get_run(run_id)
    assert "## Attached files" not in row["issue_body"]
    assert "no files here" in row["issue_body"] or "Please fix" in row["issue_body"]
    assert row["state"] == "BLOCKED"


# --------------------------------------------------------- AC4: CLI plumbing
def test_ac4_cli_attachment_flag_is_repeatable_and_absolute():
    parser = build_parser()
    args = parser.parse_args(["run", "issue.md", "--repo", "o/r",
                              "--attachment", "a.txt", "--attachment", "b.png"])
    assert args.attachment == ["a.txt", "b.png"]


def test_ac4_cmd_run_forwards_resolved_attachments_to_execute(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    cfg = parse_config(base_raw(tmp_path / "work"))
    store = Store(cfg.db_path)
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    captured = {}

    class FakeOrch:
        def __init__(self, c, st, **kw):
            pass

        async def execute(self, *, run_id=None, **kw):
            captured.update(kw)
            rid = run_id or "cli-rid"
            store.create_run(rid, state="COMPLETED", run_dir=str(tmp_path))
            return rid

    monkeypatch.setattr(orch_mod, "Orchestrator", FakeOrch)

    async def fake_heal(c, s, rid, launch, attempts=None, echo=None):
        return rid

    monkeypatch.setattr("swarmforge.postmortem.heal", fake_heal)
    monkeypatch.chdir(tmp_path)  # so the relative --attachment a.txt must become absolute

    parser = build_parser()
    args = parser.parse_args(["run", "https://github.com/o/r/issues/5", "--approval", "yes",
                              "--attachment", "a.txt", "--attachment", str(tmp_path / "b.png")])
    assert cmd_run(args, cfg) == 0
    assert captured["issue_url"] == "https://github.com/o/r/issues/5"
    assert captured["attachments"] == [str((tmp_path / "a.txt").resolve()),
                                       str(tmp_path / "b.png")]
