"""React parity: failures (post-mortems + diagnose), knowledge editing with conflict protection."""
import hashlib
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from swarmforge.db import Store
from swarmforge.services.dashboard import DashboardActions
from swarmforge.webapp.app import create_app


@pytest.fixture
def ws(cfg, tmp_path):
    store = Store(cfg.db_path)
    root = tmp_path / "vault"
    (root / "nested" / "deeper").mkdir(parents=True)
    (root / ".obsidian").mkdir()
    (root / ".obsidian" / "hidden.md").write_text("# hidden", encoding="utf-8")
    (root / "Top.md").write_text("# Top note\n\nPlain note without front matter.", encoding="utf-8")
    (root / "nested" / "deeper" / "Pitfall.md").write_text(
        "---\ntitle: Empty repos return lists\ntype: pitfall\nrepo: owner/app\ntags: [api]\n---\n\nReturn [] not None.\n", encoding="utf-8")
    (root / "Hub.md").write_text("---\ntitle: Hub\ntype: hub\n---\n\nGenerated.\n", encoding="utf-8")
    cfg.knowledge.resolved_vault = root
    yield cfg, store, root
    store.close()


def test_failures_list_counts_filter_and_detail_link_to_work(ws, tmp_path):
    cfg, store, _ = ws
    store.create_run("r-fail", state="FAILED", issue_title="Broken export")
    store.create_run("r-heal", state="COMPLETED")
    store.create_run("r-old", state="BLOCKED")
    qid = store.queue_add("Broken export", "b", "owner/app", run_id="r-fail", status="failed")
    store.failure_save("r-fail", title="Broken export", category="tests_failing", root_cause="Assertion on CSV header",
                       fix="Quote headers", lesson="Check headers", evidence="E" * 20000, heal_run_id="r-heal",
                       outcome="fixed itself on retry", cost_usd=0.4)
    with TestClient(create_app(cfg.db_path, tmp_path), base_url="http://127.0.0.1") as client:
        page = client.get("/api/v1/failures").json()
        assert page["counts"] == {"total": 1, "fixed": 1, "needs_you": 0} and page["undiagnosed"] == 1
        assert page["categories"][0]["label"] == "Tests kept failing"
        assert page["items"][0]["work_item"] == f"task-{qid}"
        assert client.get("/api/v1/failures?category=planning").json()["total"] == 0
        detail = client.get("/api/v1/failures/r-fail").json()
        assert detail["evidence_truncated"] and len(detail["evidence"]) == 16000
        assert detail["heal_work_item"] == "run-r-heal" and detail["fix"] == "Quote headers"
        assert client.get("/api/v1/failures/nope").status_code == 404


def test_diagnose_requires_authorization_and_finished_failed_run(ws):
    cfg, store, _ = ws
    calls = []
    async def fake(config, s, run_id):
        calls.append(run_id)
        s.failure_save(run_id, category="planning", root_cause="x")
        return True
    actions = DashboardActions(store, cfg, diagnose=fake)
    store.create_run("r1", state="FAILED")
    store.create_run("r2", state="RUNNING")
    assert not actions.execute("diag-request-000001", "diagnose_failure", "r1", {})["ok"]
    assert not actions.execute("diag-request-000002", "diagnose_failure", "r2", {"spending_authorized": True})["ok"]
    assert actions.execute("diag-request-000003", "diagnose_failure", "r1", {"spending_authorized": True})["ok"]
    assert actions.execute("diag-request-000003", "diagnose_failure", "r1", {"spending_authorized": True})["ok"]
    assert calls == ["r1"]


def test_knowledge_lists_nested_notes_with_facets_and_skips_hidden(ws, tmp_path):
    cfg, store, root = ws
    with TestClient(create_app(cfg.db_path, tmp_path, config=cfg), base_url="http://127.0.0.1") as client:
        page = client.get("/api/v1/knowledge").json()
        titles = {n["title"] for n in page["items"]}
        assert titles == {"Top note", "Empty repos return lists", "Hub"}
        assert page["types"] == ["hub", "pitfall", "user-note"] and page["repos"] == ["owner/app"]
        only = client.get("/api/v1/knowledge?type=pitfall&repo=owner/app").json()["items"]
        assert len(only) == 1 and only[0]["folder"] == "nested/deeper"
        note = client.get("/api/v1/knowledge/" + only[0]["id"]).json()
        assert note["editable"] and note["tags"] == ["api"] and str(root) not in json.dumps(note)
        hub = next(n for n in page["items"] if n["title"] == "Hub")
        assert client.get("/api/v1/knowledge/" + hub["id"]).json()["editable"] is False


def test_note_create_edit_conflict_and_delete(ws, tmp_path):
    cfg, store, root = ws
    actions = DashboardActions(store, cfg)
    with TestClient(create_app(cfg.db_path, tmp_path, config=cfg), base_url="http://127.0.0.1") as client:
        made = actions.execute("note-create-0000001", "create_note", "", {"title": "Queues stay independent", "body": "Because lanes.",
                                                                          "type": "decision", "tags": ["queue"], "repo": "owner/app"})
        assert made["ok"] and made["note_id"]
        note = client.get("/api/v1/knowledge/" + made["note_id"]).json()
        assert note["type"] == "decision" and note["repo"] == "owner/app"
        pit = client.get("/api/v1/knowledge?type=pitfall").json()["items"][0]
        detail = client.get("/api/v1/knowledge/" + pit["id"]).json()
        edit = {"title": "Empty repos return []", "body": "Updated body", "type": "pitfall", "tags": ["api", "lists"],
                "repo": "owner/app", "expected_sha": detail["sha"]}
        # Someone edits the file in their vault editor meanwhile: the stale save is refused.
        path = root / "nested" / "deeper" / "Pitfall.md"
        original = path.read_text(encoding="utf-8")
        path.write_text(original + "\nEdited in Obsidian.\n", encoding="utf-8")
        refused = actions.execute("note-save-000000001", "save_note", pit["id"], edit)
        assert not refused["ok"] and "changed since" in refused["message"]
        assert "Edited in Obsidian" in path.read_text(encoding="utf-8")
        fresh = client.get("/api/v1/knowledge/" + pit["id"]).json()
        assert actions.execute("note-save-000000002", "save_note", pit["id"], {**edit, "expected_sha": fresh["sha"]})["ok"]
        saved = client.get("/api/v1/knowledge/" + pit["id"]).json()
        assert saved["title"] == "Empty repos return []" and saved["tags"] == ["api", "lists"]
        hub = next(n for n in client.get("/api/v1/knowledge").json()["items"] if n["title"] == "Hub")
        hub_sha = hashlib.sha256((root / "Hub.md").read_bytes()).hexdigest()
        assert not actions.execute("note-save-000000003", "save_note", hub["id"], {**edit, "expected_sha": hub_sha})["ok"]
        assert not actions.execute("note-delete-0000001", "delete_note", pit["id"], {"expected_sha": saved["sha"], "type": "pitfall"})["ok"]
        assert actions.execute("note-delete-0000002", "delete_note", pit["id"], {"expected_sha": saved["sha"], "type": "pitfall", "confirm": True})["ok"]
        assert not path.exists() and (root / "Top.md").exists()


class FakeApi:
    has_token = True

    def __init__(self):
        from swarmforge.prs import PR
        self.merged, self.updated, self.calls = [], [], 0
        self.prs = [PR("owner/app", 1, "Ready", "u1", "swarmforge/t1", "main", False, "bot", "", "n1", "sha-ready-1", "clean", True, run_id="run-x"),
                    PR("owner/app", 2, "Behind", "u2", "swarmforge/t2", "main", False, "bot", "", "n2", "sha-behind-2", "behind", True),
                    PR("owner/app", 3, "Conflicts", "u3", "swarmforge/t3", "main", False, "bot", "", "n3", "sha-dirty-3", "dirty", False)]

    def list(self, repo, store):
        self.calls += 1
        import copy
        return [copy.copy(p) for p in self.prs if p.repo == repo]

    def merge(self, pr, method, delete_branch):
        self.merged.append((pr.number, method, delete_branch))

    def update(self, pr):
        self.updated.append(pr.number)


def test_pull_requests_list_cache_and_reviewed_commit_merge(ws, tmp_path):
    cfg, store, _ = ws
    store.create_run("run-x", state="COMPLETED", pr_url="https://github.com/owner/app/pull/1")
    api = FakeApi()
    actions = DashboardActions(store, cfg, pr_api=api)
    with TestClient(create_app(cfg.db_path, tmp_path, actions=actions), base_url="http://127.0.0.1") as client:
        view = client.get("/api/v1/prs").json()
        assert view["repos"] == ["owner/app"] and [p["ready"] for p in view["items"]] == [True, False, False]
        assert view["items"][0]["work_item"] == "run-run-x" and "node_id" not in view["items"][0]
        client.get("/api/v1/prs")
        assert api.calls == 1  # cached
        client.get("/api/v1/prs?refresh=true")
        assert api.calls == 2
    merge = {"method": "squash", "reviewed": True, "expected_sha": "sha-ready-1"}
    assert not actions.execute("pr-merge-000000001", "merge_pr", "owner/app#1", {**merge, "reviewed": False})["ok"]
    stale = actions.execute("pr-merge-000000002", "merge_pr", "owner/app#1", {**merge, "expected_sha": "sha-older-0"})
    assert not stale["ok"] and "New commits" in stale["message"]
    assert not actions.execute("pr-merge-000000003", "merge_pr", "owner/app#3", {**merge, "expected_sha": "sha-dirty-3"})["ok"]
    assert not actions.execute("pr-merge-000000004", "merge_pr", "owner/app#1", {**merge, "method": "octopus"})["ok"]
    assert actions.execute("pr-merge-000000005", "merge_pr", "owner/app#1", merge)["ok"]
    assert actions.execute("pr-merge-000000005", "merge_pr", "owner/app#1", merge)["ok"]
    assert api.merged == [(1, "squash", True)]
    assert not actions.execute("pr-update-00000001", "update_pr", "owner/app#3", {"expected_sha": "sha-dirty-3"})["ok"]
    assert actions.execute("pr-update-00000002", "update_pr", "owner/app#2", {"expected_sha": "sha-behind-2"})["ok"]
    assert api.updated == [2]
    api.has_token = False
    assert "token" in actions.execute("pr-merge-000000006", "merge_pr", "owner/app#2", {**merge, "expected_sha": "sha-behind-2"})["message"]


def test_features_unread_and_mark_read_share_the_streamlit_key(ws, tmp_path):
    cfg, store, _ = ws
    actions = DashboardActions(store, cfg)
    with TestClient(create_app(cfg.db_path, tmp_path, actions=actions), base_url="http://127.0.0.1") as client:
        before = client.get("/api/v1/features").json()
        assert before["unread"] == len(before["items"]) > 0 and before["categories"]
        assert client.get("/api/v1/badges").json()["features_unread"] == before["unread"]
        assert actions.execute("features-read-00001", "mark_features_read", "", {})["ok"]
        assert client.get("/api/v1/features").json()["unread"] == 0
        assert store.kv_get("feature_history_seen")


def test_archive_hides_finished_runs_and_fold_groups_attempts(ws, tmp_path):
    cfg, store, _ = ws
    actions = DashboardActions(store, cfg)
    for rid, state in (("a1", "FAILED"), ("a2", "FAILED"), ("a3", "RUNNING")):
        store.create_run(rid, state=state, repo="owner/app", issue_title="Fix login")
    store.update_run("a1", finished_at=1)
    store.update_run("a2", finished_at=2)
    store.create_run("solo", state="COMPLETED", repo="owner/app", issue_title="Other task")
    store.update_run("solo", finished_at=3)
    with TestClient(create_app(cfg.db_path, tmp_path), base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/work").json()["total"] == 4
        folded = client.get("/api/v1/work?fold=true").json()
        login = next(i for i in folded["items"] if i["title"] == "Fix login")
        assert folded["total"] == 2 and login["attempts"] == 3 and login["run_id"] == "a3"  # running attempt wins
        assert not actions.execute("archive-request-001", "set_archived", "run-a3", {"archived": True})["ok"]
        assert actions.execute("archive-request-002", "set_archived", "run-a1", {"archived": True})["ok"]
        assert client.get("/api/v1/work").json()["total"] == 3
        assert client.get("/api/v1/work?archived=true").json()["total"] == 4
        assert client.get("/api/v1/work/run-a1").json()["archived"] is True
        assert actions.execute("archive-request-003", "set_archived", "run-a1", {"archived": False})["ok"]
        assert client.get("/api/v1/work").json()["total"] == 4


def test_settings_save_validates_backs_up_and_refuses_concurrent_edits(cfg, tmp_path):
    import yaml
    from swarmforge.config import parse_config
    source = tmp_path / "swarmforge.yaml"
    raw = {"providers": {"p": {"base_url": "http://127.0.0.1:0", "api_key_env": "SF_TEST_KEY"}},
           "roles": {r: {"provider": "p", "model": f"m-{r}"} for r in ("planner", "developer", "tester", "reviewer", "librarian")},
           "developer_roles": ["developer"], "workdir": str(tmp_path / "work")}
    source.write_text("# keep this comment\n" + yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = parse_config(raw, source_path=source)
    store = Store(config.db_path)
    try:
        actions = DashboardActions(store, config, model_test=lambda c, roles=None: [{"role": "planner", "model": "m", "ok": True, "detail": "ok", "cost_usd": 0.0001}])
        with TestClient(create_app(config.db_path, tmp_path, actions=actions), base_url="http://127.0.0.1") as client:
            details = client.get("/api/v1/settings/details").json()
            assert details["editable"] and {p["key_env"] for p in details["providers"]} == {"SF_TEST_KEY"}
            assert "key_set" in details["providers"][0] and "http://127.0.0.1:0" not in json.dumps(details)
        base = {"expected_config_sha": details["config_sha"], "limits": {"budget_usd": 7.5, "max_repair_cycles": 3},
                "roles": {"planner": {"model": "m-new-planner", "temperature": 0.3, "max_steps": 40}}}
        bad = actions.execute("settings-save-00001", "save_settings", "", {**base, "limits": {"max_repair_cycles": 99}})
        assert not bad["ok"] and "between" in bad["message"]
        same = actions.execute("settings-save-00002", "save_settings", "", {**base, "roles": {"developer": {"model": "m-reviewer", "temperature": 0.2, "max_steps": 10}}})
        assert not same["ok"] and "approve its own work" in same["message"]  # parse_config's own rule applies
        assert actions.execute("settings-save-00003", "save_settings", "", base)["ok"]
        text = source.read_text(encoding="utf-8")
        assert "# keep this comment" in text and "m-new-planner" in text and (tmp_path / "swarmforge.yaml.bak").exists()
        with TestClient(create_app(config.db_path, tmp_path, actions=actions), base_url="http://127.0.0.1") as client:
            after = client.get("/api/v1/settings/details").json()
            planner = next(r for r in after["roles"] if r["role"] == "planner")
            assert after["restart_needed"] and planner["model"] == "m-new-planner" and after["limits"]["budget_usd"] == 7.5
        stale = actions.execute("settings-save-00004", "save_settings", "", base)
        assert not stale["ok"] and "changed since" in stale["message"]
        assert not actions.execute("model-test-0000001", "test_models", "", {})["ok"]
        tested = actions.execute("model-test-0000002", "test_models", "", {"spending_authorized": True})
        assert tested["ok"] and tested["details"][0]["ok"] is True
    finally:
        store.close()


def test_new_task_advanced_options_and_issue_source(ws):
    cfg, store, _ = ws
    actions = DashboardActions(store, cfg)
    base = {"title": "T", "body": "B", "repo": "owner/app", "budget": 3, "spending_authorized": True}
    made = actions.execute("task-advanced-00001", "queue_task", "", {**base, "max_cycles": 4, "hours": 1.5, "heal_attempts": 2})
    item = store.queue_item(int(made["item_id"][5:]))
    assert (item["max_cycles"], item["max_runtime_s"], item["heal_attempts"], item["approval"]) == (4, 5400, 2, "dashboard")
    assert not actions.execute("task-advanced-00002", "queue_task", "", {**base, "max_cycles": 11})["ok"]
    assert not actions.execute("task-advanced-00003", "queue_task", "", {**base, "hours": 100})["ok"]
    assert not actions.execute("task-advanced-00004", "queue_task", "", {**base, "team": "no-such-team"})["ok"]
    issue = actions.execute("task-advanced-00005", "queue_task", "", {"issue_url": "https://github.com/owner/app/issues/7", "budget": 2, "spending_authorized": True})
    queued = store.queue_item(int(issue["item_id"][5:]))
    assert queued["issue_url"].endswith("/issues/7") and queued["repo"] == "https://github.com/owner/app" and "#7" in queued["title"]
    assert not actions.execute("task-advanced-00006", "queue_task", "", {"issue_url": "https://example.com/x", "budget": 2, "spending_authorized": True})["ok"]


def test_speed_benchmarks_and_health_views_are_read_only_and_bounded(ws, tmp_path):
    cfg, store, _ = ws
    store.create_run("done-1", state="COMPLETED", issue_title="Quick")
    store.update_run("done-1", finished_at=store.get_run("done-1")["created_at"] + 120)
    store._exec("INSERT INTO benchmarks(created_at,label,issue,results_json) VALUES (1,'suite','pinned',?)",
                (json.dumps([{"case": "c1", "repeat": 0, "team": "chinese", "mode": "swarm", "final_state": "COMPLETED",
                              "hidden_acceptance": {"passed": 3, "total": 3}, "elapsed_s": 60, "cost_usd": 0.5, "repair_cycles": 1,
                              "human_interventions": 0, "calls_without_cost": 0}]),))
    store._exec("INSERT INTO benchmarks(created_at,label,issue,results_json) VALUES (2,'broken','pinned',?)", (json.dumps([{"case": "c1"}]),))
    actions = DashboardActions(store, cfg)
    actions.health_checks = lambda c: [{"name": "Docker running", "status": "fail", "message": "not running", "fix": "start Docker", "secret": object()}]
    with TestClient(create_app(cfg.db_path, tmp_path, actions=actions), base_url="http://127.0.0.1") as client:
        speed = client.get("/api/v1/speed").json()
        assert speed["phases"] and isinstance(speed["runs"], list)
        bench = {b["label"]: b for b in client.get("/api/v1/benchmarks").json()["items"]}
        assert bench["suite"]["rows"][0]["delivery_pct"] == 100 and bench["suite"]["rows"][0]["team"] == "chinese"
        assert "incomplete" in bench["broken"]["error"]
        health = client.get("/api/v1/settings/health").json()["checks"]
        assert health == [{"name": "Docker running", "status": "fail", "message": "not running", "fix": "start Docker"}]
