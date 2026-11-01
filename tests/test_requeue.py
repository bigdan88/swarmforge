"""Restarting queue tasks: no duplicate copies, the old row says where it went, crashed runs are closed."""
from swarmforge import runqueue as rq
from swarmforge.db import Store


def test_requeue_once_goes_to_front_and_marks_the_old_item(tmp_path):
    st = Store(tmp_path / "db")
    old = st.queue_add("Filter runs", "body", "bigdan88/swarmforge", issue_url="https://github.com/o/r/issues/1")
    st.queue_update(old, status="failed", note="worker stopped while this item was running")
    other = st.queue_add("Chatbot", "b", "bigdan88/swarmforge")
    new, created = rq.requeue(st, st.queue_item(old), mode="swarm")
    assert created and new != old
    waiting = [q["id"] for q in st.queue_items() if q["status"] == "queued"]
    assert waiting == [new, other]                                     # front of the line
    assert st.queue_item(new)["issue_url"] == "https://github.com/o/r/issues/1"
    assert st.queue_item(old)["note"].endswith(f"{rq.RESTARTED}{new}")
    again, created2 = rq.requeue(st, st.queue_item(old), mode="swarm")  # double click
    assert (again, created2) == (new, False)
    assert len([q for q in st.queue_items() if q["title"] == "Filter runs"]) == 2


async def test_worker_closes_runs_it_left_behind(cfg, tmp_path):
    st = Store(cfg.db_path)
    st.create_run("r1", state="PLANNING", pid=999999)
    item = st.queue_add("T", "B", "repo")
    st.queue_update(item, status="running", run_id="r1")
    st.kv_set(rq.PAUSED_KEY, "1")              # recover, then stop immediately
    await rq.work(cfg, st, echo=None)
    assert st.queue_item(item)["status"] == "failed"
    run = st.get_run("r1")
    assert run["state"] == "FAILED" and "queue worker stopped" in run["state_reason"]
