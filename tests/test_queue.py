import subprocess
from pathlib import Path

import pytest

from swarmforge import runqueue as rq
from swarmforge.db import Store

from . import fake_team
from .conftest import ISSUE, ROOT, FakeProvider

pytestmark = pytest.mark.slow


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


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE queue_items (id INTEGER PRIMARY KEY AUTOINCREMENT, position REAL NOT NULL,
        title TEXT NOT NULL, body TEXT NOT NULL, repo TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'swarm',
        budget REAL, max_cycles INTEGER, chain INTEGER NOT NULL DEFAULT 1, approval TEXT NOT NULL DEFAULT 'yes',
        status TEXT NOT NULL DEFAULT 'queued', run_id TEXT, base_branch TEXT, result_branch TEXT, note TEXT,
        created_at REAL, started_at REAL, finished_at REAL);
        INSERT INTO queue_items (position, title, body, repo) VALUES (1, 'old', 'b', 'r');""")
    con.commit()
    con.close()
    s = Store(db)
    item = s.queue_items()[0]
    assert item["title"] == "old" and "max_runtime_s" in item and "seed_run_id" in item
    s.queue_update(item["id"], max_runtime_s=7200, seed_run_id="x")
    assert s.queue_item(item["id"])["max_runtime_s"] == 7200


async def test_queue_item_continue_uses_seed_and_runtime(cfg, sample_repo):
    store = Store(cfg.db_path)
    body = ISSUE.read_text(encoding="utf-8").split("\n", 2)[2]
    cfg.limits.max_repair_cycles = 0
    first = store.queue_add("Export", body, str(sample_repo.resolve()), max_cycles=0)
    await rq.work(cfg, store, echo=None, llm_transport=_provider().transport)
    i1 = store.queue_item(first)
    assert i1["status"] == "failed"
    store.kv_set(rq.PAUSED_KEY, "0")
    again = store.queue_add("Export", body, str(sample_repo.resolve()), max_cycles=2,
                            seed_run_id=i1["run_id"], max_runtime_s=7200)
    await rq.work(cfg, store, echo=None, llm_transport=_provider().transport)
    i2 = store.queue_item(again)
    assert i2["status"] == "done", i2["note"]
    r2 = store.get_run(i2["run_id"])
    assert r2["seed_run_id"] == i1["run_id"]
    assert __import__("json").loads(r2["config_json"])["limits"]["max_runtime_s"] == 7200


async def test_queue_item_heals_itself(cfg, sample_repo):
    store = Store(cfg.db_path)
    body = ISSUE.read_text(encoding="utf-8").split("\n", 2)[2]
    iid = store.queue_add("Export", body, str(sample_repo.resolve()), max_cycles=0, heal_attempts=1)
    await rq.work(cfg, store, echo=None, llm_transport=_provider().transport)
    item = store.queue_item(iid)
    assert item["status"] == "done", item["note"]
    run = store.get_run(item["run_id"])
    assert run["heal_attempt"] == 1 and run["state"] == "COMPLETED"
    assert store.failure(run["heal_of"])["outcome"] == "fixed itself on attempt 1"
    assert "after self-healing attempt 1" in item["note"]
