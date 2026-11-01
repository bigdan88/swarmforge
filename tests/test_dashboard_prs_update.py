"""Dashboard: Pull requests page (merge one, merge all) and the Update & restart button."""
import httpx
import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from swarmforge.db import Store  # noqa: E402

from .test_dashboard import APP, _go, _write_cfg  # noqa: E402
from .test_prs import FakeGitHub, pr  # noqa: E402


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    monkeypatch.setenv(cfg.github.token_env, "tok")
    fake = FakeGitHub([pr(7), pr(8, "dirty")])
    import swarmforge.github as G
    real = G.GitHubClient
    monkeypatch.setattr(G, "GitHubClient", lambda c, transport=None: real(c, transport=httpx.MockTransport(fake)))
    Store(cfg.db_path).create_run("r7", integration_branch="swarmforge/task-7", issue_title="Task 7",
                                  issue_body="Make it so.", pr_url="https://github.com/o/r/pull/7", state="COMPLETED")
    return cfg, fake


def test_pull_requests_page_lists_and_merges(fake_gh):
    cfg, fake = fake_gh
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _go(at, "Pull requests")
    assert not at.exception, [e.value for e in at.exception]
    text = " ".join(m.value for m in at.markdown)
    assert "#7 Task 7" in text and "Conflicts with main" in text and "Ready to merge" in text
    assert at.button(key="pr_m_o/r_7").disabled                     # must tick "I've reviewed" first
    at.checkbox(key="pr_sure").check().run()
    at.button(key="pr_merge_all").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("#7 Task 7: merged" in s.value for s in at.success)
    assert ("PUT", "/repos/o/r/pulls/7/merge") in [(m, p) for m, p, _ in fake.calls]
    assert ("PUT", "/repos/o/r/pulls/8/merge") not in [(m, p) for m, p, _ in fake.calls]


def test_update_and_restart_button(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    monkeypatch.setenv("SWARMFORGE_DASH_UPDATE_CHECK", "1")
    from swarmforge import selfupdate as su
    restarted = []
    monkeypatch.setattr(su, "status", lambda fetch=True: {"ok": True, "behind": 2, "ahead": 0, "head": "abc1234",
                                                         "incoming": ["f00 Dark mode (#5)", "b4r Chatbot (#4)"],
                                                         "dirty": []})
    monkeypatch.setattr(su, "update", lambda: {"message": "Updated abc → def (2 new commit(s)).", "behind": 2,
                                               "commits": ["f00 Dark mode (#5)"], "deps": None,
                                               "images_stale": True, "ahead": 0, "merged": False})
    monkeypatch.setattr(su, "restart_dashboard", lambda cfgp, logs: restarted.append(cfgp) or 1)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert any("2 update(s) available" in i.value for i in at.info)
    at.button(key="upd_go").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert restarted and any("restarting with the new code" in s.value for s in at.success)
    assert any("sandbox images changed" in w.value for w in at.warning)
