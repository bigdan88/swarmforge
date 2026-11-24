"""Read-only API contracts against disposable runtime databases, without models."""
import sqlite3
import runpy
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from swarmforge.db import Store
from swarmforge.webapp.app import create_app
from swarmforge.webapp.contracts import WorkItem, WorkPage
from swarmforge.webapp.queries import WorkReader


@pytest.fixture
def history(tmp_path):
    path = tmp_path / "history.db"
    store = Store(path)
    store.create_run("attached", issue_title="Private run title", repo="owner/app", config_json='{"api_key":"must-not-leak"}')
    store.queue_add("Build horse profiles", "Private task body", "owner/app", run_id="attached", status="running")
    store.create_run("direct", issue_title="Direct CLI work", repo="owner/other")
    for i in range(38):
        store.queue_add(f"Completed {i}", "", "owner/app", status="done")
    store.close()
    return path


def test_bounded_projection_includes_direct_runs_once(history):
    reader = WorkReader(history)
    page = reader.list(limit=10)
    assert len(page["items"]) == 10
    assert page["total"] == 40
    assert page["counts"]["running"] == 2
    all_rows = reader.list(limit=100)["items"]
    assert sum(r["run_id"] == "attached" for r in all_rows) == 1
    assert len({r["id"] for r in all_rows}) == 40
    assert set(r["id"] for r in reader.list(limit=10, offset=10)["items"]).isdisjoint(r["id"] for r in page["items"])


def test_search_is_literal_and_status_filter_is_parameterized(history):
    reader = WorkReader(history)
    assert reader.list(search="horse")["total"] == 1
    assert reader.list(search="% OR 1=1")["total"] == 0
    assert reader.list(status="done")["total"] == 38
    with pytest.raises(ValueError):
        reader.list(limit=1000)


def test_terminal_direct_run_uses_actual_workflow_states(history):
    store = Store(history)
    store.update_run("direct", state="COMPLETED")
    store.close()
    assert WorkReader(history).detail("run-direct")["status"] == "done"
    assert WorkReader(history).list(limit=1)["items"][0]["status"] == "running"


def test_detail_matches_page_without_alternate_attached_or_numeric_ids(history):
    reader = WorkReader(history)
    for item in reader.list(limit=100)["items"]:
        assert reader.detail(item["id"]) == item
    for invalid in ("run-attached", "task-01", "task-0", "task--1", "run-", "task-" + "9" * 500):
        assert reader.detail(invalid) is None


def test_mixed_query_plan_builds_attachment_set_once_and_detail_uses_primary_key(history):
    reader = WorkReader(history)
    with reader.connection() as conn:
        plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + reader.projection)]
        assert not any("CORRELATED" in step for step in plan), plan
        task_plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + reader.queue_projection + " WHERE q.id=?", (1,))]
        assert any("SEARCH q USING INTEGER PRIMARY KEY" in step for step in task_plan), task_plan


def test_typed_contract_accepts_legacy_null_dates_and_skipped_work(history, tmp_path):
    store = Store(history)
    store.queue_add("Skipped work", "", "owner/app", status="skipped")
    with store.conn:
        store.conn.execute("UPDATE queue_items SET created_at=NULL WHERE status='skipped'")
    store.close()
    with TestClient(create_app(history, tmp_path), base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/work?status=skipped")
        assert response.status_code == 200
        page = WorkPage.model_validate(response.json())
        assert len(page.items) == 1
        assert page.items[0].created_at is None
        schemas = client.get("/openapi.json").json()["components"]["schemas"]
        assert set(schemas["WorkItem"]["properties"]) == set(WorkItem.model_fields)
        assert schemas["WorkItem"]["additionalProperties"] is False


def test_api_contract_and_no_private_columns(history, tmp_path):
    with TestClient(create_app(history, tmp_path), base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/work")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "config_json" not in response.text
        assert "must-not-leak" not in response.text
        assert "Private task body" not in response.text
        item = client.get("/api/v1/work?search=horse").json()["items"][0]
        assert client.get(f"/api/v1/work/{item['id']}").json() == item
        assert client.get("/api/v1/work/not-found").status_code == 404
        assert client.get("/api/v1/work?limit=10000").status_code == 422
        assert client.get("/api/v1/work?offset=-1").status_code == 422
        assert client.get("/api/v1/work?status=arbitrary").status_code == 422
        assert client.get("/api/v1/work?search=" + "x" * 201).status_code == 422
        assert client.post("/api/v1/work").status_code == 405
        assert client.get("/api/v1/work", headers={"origin": "https://evil.example"}).status_code == 403
        assert client.get("/api/v1/work", headers={"host": "evil.example"}).status_code == 400
        assert client.get("/").status_code == 503
        assert "not built" in client.get("/").text


def test_missing_database_not_created_and_error_does_not_leak_path(tmp_path):
    path = tmp_path / "secret-path.db"
    with TestClient(create_app(path, tmp_path), base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/work")
        assert response.status_code == 503
        assert str(path) not in response.text
    assert not path.exists()


def test_database_remains_read_only(history):
    reader = WorkReader(history)
    before = history.read_bytes()
    with reader.connection() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM queue_items")
    reader.list()
    assert history.read_bytes() == before


def test_serves_built_assets_without_spa_fallback_on_unknown_api(history, tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>Work</html>", encoding="utf-8")
    (tmp_path / "assets" / "app.js").write_text("const x=1", encoding="utf-8")
    with TestClient(create_app(history, tmp_path), base_url="http://127.0.0.1") as client:
        assert client.get("/").status_code == 200
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/api/v1/no-such-route").status_code == 404


def test_evidence_bounds_private_fields_and_reconnect_cursor(history, tmp_path):
    store = Store(history)
    with store.conn:
        store.conn.executemany("INSERT INTO events(run_id,ts,level,kind,message,data) VALUES (?,?,?,?,?,?)",
                               [("attached" if i % 2 == 0 else "direct", i, "info", "progress", "x" * 5000, "secret payload")
                                for i in range(240)])
        store.conn.executemany("INSERT INTO acceptance(run_id,cycle,criterion_id,text,status,evidence) VALUES (?,?,?,?,?,?)",
                               [("attached", i, "check", "y" * 5000, "passed", "recorded evidence") for i in range(105)])
        store.conn.executemany("INSERT INTO test_runs(run_id,ts,label,command,log_path) VALUES (?,?,?,?,?)",
                               [("attached", i, "Unit tests", "private command", "C:/secret/log") for i in range(30)])
    store.record_llm_call("attached", cost_source="provider_reported", cost_usd=0.25)
    store.record_llm_call("attached", cost_source="estimated", cost_usd=0.1)
    store.record_llm_call("attached", cost_source="unavailable")
    store.close()
    reader = WorkReader(history)
    item = reader.list(search="horse")["items"][0]["id"]
    recent = reader.evidence(item, limit=10)
    assert len(recent["events"]) == 10
    assert recent["events"][0]["id"] == 221
    assert recent["next_event_id"] == 239
    assert recent["events_has_more"] is False  # Initial snapshot intentionally shows recent events.
    first = reader.evidence(item, after=1, limit=10)
    second = reader.evidence(item, after=first["next_event_id"], limit=100)
    assert first["events_has_more"] is True
    ids = [e["id"] for e in first["events"] + second["events"]]
    assert ids == sorted(set(ids))
    assert all(i % 2 == 1 for i in ids)  # Other runs' IDs may occur between these events.
    assert all(len(e["message"]) == 4000 and e["truncated"] for e in first["events"])
    assert len(recent["checks"]) == 100 and recent["checks_has_more"]
    assert len(recent["tests"]) == 25 and recent["tests_has_more"]
    assert recent["checks"][0]["truncated"]
    assert recent["costs"] == {"calls": 3, "reported_usd": 0.25, "estimated_usd": 0.1, "unavailable_calls": 1}
    with TestClient(create_app(history, tmp_path), base_url="http://127.0.0.1") as client:
        response = client.get(f"/api/v1/work/{item}/evidence")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        for private in ("secret payload", "private command", "C:/secret/log", "config_json"):
            assert private not in response.text
        assert client.get(f"/api/v1/work/{item}/evidence?after=-1").status_code == 422
        assert client.get(f"/api/v1/work/{item}/evidence?after=9223372036854775808").status_code == 422
        assert client.get(f"/api/v1/work/{item}/evidence?limit=101").status_code == 422
        assert client.get("/api/v1/work/run-attached/evidence").status_code == 404


def test_evidence_shows_shared_persisted_allowance_without_exposing_owners(history):
    store = Store(history)
    # The web preview must also work independently of the unmerged execution
    # implementation. Seed its optional persisted contract as fixture data.
    store.conn.executescript("""
        CREATE TABLE IF NOT EXISTS execution_allowances (
            id TEXT PRIMARY KEY,created_at REAL,deadline REAL,budget_usd REAL,spent_usd REAL,
            max_recoveries INTEGER,no_progress_s REAL,repeat_limit INTEGER,owner_token TEXT);
        CREATE TABLE IF NOT EXISTS execution_attempts (
            run_id TEXT PRIMARY KEY,execution_id TEXT,number INTEGER,started_at REAL,last_progress_at REAL,
            last_result TEXT,next_action TEXT,current_action TEXT,stop_reason TEXT);
        CREATE TABLE IF NOT EXISTS execution_calls (
            id TEXT PRIMARY KEY,execution_id TEXT,run_id TEXT,reserved_usd REAL,status TEXT);
    """)
    with store.conn:
        store.conn.execute("""INSERT INTO execution_allowances
            (id,created_at,deadline,budget_usd,spent_usd,max_recoveries,no_progress_s,repeat_limit,owner_token)
            VALUES ('shared',1,100,10,3,1,60,3,'private-claim')""")
        store.conn.executemany("""INSERT INTO execution_attempts
            (run_id,execution_id,number,started_at,last_progress_at,last_result,next_action)
            VALUES (?,'shared',?,1,5,'Tests passed','Review changes')""", [("attached", 1), ("direct", 2)])
        store.conn.execute("""INSERT INTO execution_calls(id,execution_id,run_id,reserved_usd,status)
            VALUES ('pending','shared','direct',2,'reserved')""")
    store.close()
    reader = WorkReader(history)
    before = history.read_bytes()
    first = reader.evidence(reader.list(search="horse")["items"][0]["id"])["allowance"]
    retry = reader.evidence("run-direct")["allowance"]
    assert first["attempt"] == 1 and retry["attempt"] == 2
    assert first["budget_usd"] == retry["budget_usd"] == 10
    assert first["spent_usd"] == retry["spent_usd"] == 3
    assert retry["reserved_usd"] == 2 and retry["available_usd"] == 5
    assert retry["deadline"] == 100 and retry["last_result"] == "Tests passed"
    assert "owner_token" not in retry
    assert history.read_bytes() == before


def test_evidence_supports_undispatched_and_legacy_history_without_migrating(history):
    store = Store(history)
    with store.conn:
        for table in ("execution_attempts", "execution_calls", "execution_allowances"):
            store.conn.execute(f"DROP TABLE IF EXISTS {table}")
    store.close()
    reader = WorkReader(history)
    before = history.read_bytes()
    pending = reader.evidence(reader.list(search="Completed 0")["items"][0]["id"])
    assert pending["run_id"] is None and pending["costs"] is None
    assert pending["events"] == pending["checks"] == pending["tests"] == []
    legacy = reader.evidence("run-direct")
    assert legacy["allowance"] is None
    assert legacy["costs"]["calls"] == 0
    assert reader.evidence("task-999999") is None
    assert history.read_bytes() == before


def test_benchmark_uses_disposable_mixed_and_legacy_work_assembly():
    benchmark = runpy.run_path(str(Path(__file__).parents[1] / "scripts/benchmark_web_queries.py"))["benchmark"]
    queue = benchmark(20, 1, False, True)
    mixed = benchmark(20, 1, True, True)
    assert queue["legacy"]["assembled_rows"] == queue["web"]["total"] == 20
    assert mixed["legacy"]["assembled_rows"] == mixed["web"]["total"] == 30
    assert mixed["web"]["returned_rows"] == 25
    assert len(mixed["legacy"]["dashboard_sha256"]) == 64
    assert not any("CORRELATED" in step for step in mixed["query_plans"]["projection"])
