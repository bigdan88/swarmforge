import subprocess
from pathlib import Path

import pytest

from swarmforge import runqueue as rq
from swarmforge.db import Store

from . import fake_team
from .conftest import ISSUE, ROOT, FakeProvider


def _provider():
    return FakeProvider({
        "qwen-fake": fake_team.planner, "deepseek-fake": fake_team.developer,
        "kimi-fake": fake_team.developer, "minimax-fake": fake_team.tester,
        "glm-fake": fake_team.Reviewer(),
    })


def test_parse_backlog_file():
    items = rq.parse_backlog((ROOT / "docs" / "SELF_IMPROVEMENT_BACKLOG.md").read_text(encoding="utf-8"))
    assert len(items) == 7
    assert items[0].title == "Make every dashboard section fail-safe"
    assert items[4].title == "Show live progress during long model calls"
    assert all("Acceptance criteria" in i.body for i in items)
    assert all("---" not in i.body.splitlines() for i in items)


def test_queue_order_and_moves(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    a = s.queue_add("A", "a", "r")
    b = s.queue_add("B", "b", "r")
    c = s.queue_add("C", "c", "r")
    s.queue_move(c, -1)
    assert [i["title"] for i in s.queue_items()] == ["A", "C", "B"]
    s.queue_update(a, status="done")
    s.queue_move(c, -1)  # cannot move above a finished item
    assert [i["title"] for i in s.queue_items()] == ["A", "C", "B"]
    assert s.queue_clear_finished() == 1 and len(s.queue_items()) == 2


async def test_chained_queue_builds_on_previous_result(cfg, sample_repo):
    store = Store(cfg.db_path)
    body = ISSUE.read_text(encoding="utf-8").split("\n", 2)[2]
    first = store.queue_add("Add CSV and JSON export", body, str(sample_repo.resolve()))
    second = store.queue_add("Polish the export", body, str(sample_repo.resolve()))
    fake = _provider()
    n = await rq.work(cfg, store, echo=None, llm_transport=fake.transport)
    assert n == 2
    i1, i2 = store.queue_item(first), store.queue_item(second)
    assert i1["status"] == "done" and i1["result_branch"], i1
    assert i2["base_branch"] == i1["result_branch"] and "continues from" in i2["note"]
    r2 = store.get_run(i2["run_id"])
    assert r2["base_branch"] == i1["result_branch"]
    assert i2["status"] == "done", i2["note"]
    # the second result contains the first one's work; the user's main branch is untouched
    rc = subprocess.run(["git", "merge-base", "--is-ancestor", i1["result_branch"], i2["result_branch"]],
                        cwd=sample_repo).returncode
    assert rc == 0
    head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=sample_repo,
                          capture_output=True, text=True).stdout.strip()
    assert head == "main" and not (sample_repo / "inventory" / "exporters.py").exists()


async def test_queue_pauses_on_failure(cfg, sample_repo, tmp_path):
    store = Store(cfg.db_path)
    bad = store.queue_add("Broken", "x", str(tmp_path / "missing-repo"))
    nxt = store.queue_add("Next", "y", str(sample_repo))
    n = await rq.work(cfg, store, echo=None, llm_transport=_provider().transport)
    assert n == 1
    assert store.queue_item(bad)["status"] == "failed"
    assert store.queue_item(nxt)["status"] == "queued"
    assert store.kv_get(rq.PAUSED_KEY) == "1"
    # with policy=continue the next item would run
    store.kv_set(rq.POLICY_KEY, "continue")
    store.kv_set(rq.PAUSED_KEY, "0")
    store.queue_update(nxt, status="skipped")
    assert await rq.work(cfg, store, echo=None, llm_transport=_provider().transport) == 0


def test_resolve_base_without_chain_or_result(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    a = s.queue_add("A", "a", "repo1")
    b = s.queue_add("B", "b", "repo1", chain=0)
    c = s.queue_add("C", "c", "repo1")
    d = s.queue_add("D", "d", "repo2")
    s.queue_update(a, status="done", result_branch="swarmforge/task-aaa")
    assert rq.resolve_base(s, s.queue_item(b)) == (None, None)
    s.queue_update(b, status="done")  # finished without a reachable result
    base, note = rq.resolve_base(s, s.queue_item(c))
    assert base is None and "not reachable" in note
    assert rq.resolve_base(s, s.queue_item(d)) == (None, None)  # different repository
