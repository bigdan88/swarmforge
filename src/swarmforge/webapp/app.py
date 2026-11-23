"""Loopback React dashboard API; browsing by default, explicit protected controls."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .artifacts import ArtifactReader, ArtifactUnavailable
from .contracts import RetainedReport, WorkArtifacts, WorkEvidence, WorkItem, WorkPage
from .queries import WorkReader
from .auth import LocalSession
from .contracts import ActionCommand, ActionResult, Attachment, UploadCommand
from .workspace import (WorkspaceReader, benchmarks, failure, failures, feature_history, public_settings,
                        settings_details, speed)
from .notes import NotesReader
from .contracts import (ProjectPage, ProjectDetail, ConversationPage, ConversationDetail,
                        WorkControls, QueueSnapshot, NotesPage, NoteDetail, FailurePage, FailureDetail)


# Writable endpoints and their request-size limits. Actions stay small; staged chat uploads are
# base64 JSON for one file of at most 5 MB.
WRITE_LIMITS = {"/api/v1/actions": 128000, "/api/v1/chat/uploads": 7_100_000}


def create_app(database: Path, assets: Path | None = None, runs_root: Path | None = None, *, config=None, actions=None, demo=False) -> FastAPI:
    if actions is not None:
        config = config or actions.config
        if actions.store.path.resolve() != database.resolve() or (config is not None and config.db_path.resolve() != database.resolve()):
            raise ValueError("Controls, configuration and read projections must use the same database")
    app = FastAPI(title="SwarmForge local API", version="0.1", docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
    reader = WorkReader(database)
    workspace = WorkspaceReader(database)
    session = LocalSession()
    # Opaque identities: which workspace (database) this server serves, and this particular start.
    # Writes must name the workspace the page displayed, so a stale tab cannot act on a replacement.
    import hashlib
    import uuid
    workspace_id = hashlib.sha256(("swarmforge-workspace\0" + str(database.resolve()) + ("\0demo" if demo else "")).encode()).hexdigest()[:20]
    instance_id = uuid.uuid4().hex[:12]
    notes = NotesReader(getattr(config.knowledge, "resolved_vault", None) if config is not None else None)
    artifacts = ArtifactReader(runs_root or database.resolve().parent / "runs")
    assets = assets or Path(__file__).parents[1] / "resources" / "web"

    @app.middleware("http")
    async def local_read_only(request: Request, call_next):
        origin = request.headers.get("origin")
        cross_site_api = request.url.path.startswith("/api/") and request.headers.get("sec-fetch-site") == "cross-site"
        if cross_site_api or (origin and origin != str(request.base_url).rstrip("/")):
            return JSONResponse({"detail": "Cross-origin access is disabled"}, status_code=403)
        if request.method not in ("GET", "HEAD"):
            if actions is None or request.method != "POST" or request.url.path not in WRITE_LIMITS:
                return JSONResponse({"detail": "This preview is read-only"}, status_code=405)
            if origin != str(request.base_url).rstrip("/") or not session.authorized(request.cookies.get(session.cookie), request.headers.get("x-csrf-token")):
                return JSONResponse({"detail": "Refresh this page to authorize local controls"}, status_code=403)
            if request.headers.get("x-swarmforge-workspace") != workspace_id:
                return JSONResponse({"detail": "This page shows a different SwarmForge workspace than the one now running here. "
                                               "Reload the page and review it before taking actions.", "code": "workspace_changed"}, status_code=409)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "JSON requests are required"}, status_code=415)
            try:
                size = int(request.headers.get("content-length", "-1"))
            except ValueError:
                size = -1
            if not 0 <= size <= WRITE_LIMITS[request.url.path]:
                return JSONResponse({"detail": "Request is too large or has no length"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        return response

    @app.exception_handler(sqlite3.Error)
    async def database_unavailable(request: Request, exc: sqlite3.Error):
        return JSONResponse({"detail": "Work history is unavailable. Check the database path and version."}, status_code=503)

    @app.exception_handler(OSError)
    async def workspace_files_unavailable(request: Request, exc: OSError):
        return JSONResponse({"detail": "Retained workspace files are unavailable."}, status_code=503)

    @app.get("/api/v1/session")
    def browser_session(request: Request):
        payload = {"actions_enabled": actions is not None, "models_enabled": actions is not None and config is not None,
                   "csrf_token": None, "settings": public_settings(config), "demo_mode": demo,
                   "workspace_id": workspace_id, "instance_id": instance_id}
        response = JSONResponse(payload)
        if actions is not None:
            cookie, token = session.issue(request.cookies.get(session.cookie))
            payload["csrf_token"] = token
            response = JSONResponse(payload)
            response.set_cookie(session.cookie, cookie, httponly=True, samesite="strict", max_age=session.lifetime, path="/api/v1")
        return response

    @app.post("/api/v1/actions", response_model=ActionResult)
    def act(command: ActionCommand, request: Request):
        from ..services.dashboard import ActionConflict
        try:
            return actions.execute(request.headers.get("idempotency-key", ""), command.operation, command.target, command.args)
        except ActionConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/v1/chat/uploads", response_model=Attachment)
    def chat_upload(upload: UploadCommand):
        from ..services.chat_requests import stage_upload
        try:
            return stage_upload(actions.store, config, upload.name, upload.data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/v1/conversations/{cid}/attachments/{upload_id}")
    def chat_attachment(cid: str, upload_id: str):
        from ..attachments import IMAGE_MIME, RASTER_SIGNATURES
        from ..services.chat_requests import upload_root
        row = workspace.chat_upload(cid, upload_id) if len(upload_id) == 32 else None
        if row is None or row["kind"] != "image" or config is None:
            raise HTTPException(404, "Attachment unavailable")
        path, root = Path(row["path"]).resolve(), upload_root(config)
        media = IMAGE_MIME.get(path.suffix.lower()) if path.suffix.lower() in RASTER_SIGNATURES else None
        if root not in path.parents or media is None or not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
            raise HTTPException(404, "Attachment unavailable")
        return Response(path.read_bytes(), media_type=media, headers={"Content-Disposition": 'inline; filename="attachment"'})

    @app.get("/api/v1/projects", response_model=ProjectPage)
    def projects(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=100000), search: str = Query("", max_length=200)):
        return workspace.projects(limit=limit, offset=offset, search=search)

    @app.get("/api/v1/projects/{pid}", response_model=ProjectDetail)
    def project(pid: str):
        row = workspace.project(pid)
        if row is None:
            raise HTTPException(404, "Project not found")
        return row

    @app.get("/api/v1/conversations", response_model=ConversationPage)
    def conversations(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        return workspace.conversations(limit=limit, offset=offset)

    @app.get("/api/v1/conversations/{cid}", response_model=ConversationDetail)
    def conversation(cid: str):
        row = workspace.conversation(cid)
        if row is None:
            raise HTTPException(404, "Conversation not found")
        return row

    @app.get("/api/v1/work/{item_id}/controls", response_model=WorkControls)
    def controls(item_id: str):
        row = workspace.controls(item_id)
        if row is None:
            raise HTTPException(404, "Work item not found")
        return row

    @app.get("/api/v1/queue", response_model=QueueSnapshot)
    def queue_status():
        return workspace.queue_status()

    @app.get("/api/v1/outcomes")
    def outcomes():
        return workspace.outcomes()

    @app.get("/api/v1/features")
    def features():
        seen = set((workspace.kv("feature_history_seen") or "").split(","))
        items = [{**e, "unread": e["id"] not in seen} for e in feature_history()[:100]]
        return {"items": items, "unread": sum(e["unread"] for e in items), "categories": sorted({str(e.get("category", "")) for e in items})}

    @app.get("/api/v1/badges")
    def badges():
        seen = set((workspace.kv("feature_history_seen") or "").split(","))
        counts = failures(workspace, limit=1)["counts"]
        return {"failures_need_you": counts["needs_you"], "features_unread": sum(e["id"] not in seen for e in feature_history()[:100])}

    pull_requests = actions.pull_requests if actions is not None else None
    if pull_requests is None and config is not None:
        from ..db import Store
        from ..services.pull_requests import PullRequests

        class _ReadOnlyRuns:
            """Run/queue lookups for PR listing without opening a writable Store."""
            RECENT_REPOS_KEY = Store.RECENT_REPOS_KEY
            recent_repos = Store.recent_repos

            def _all(self, sql, args=()):
                with workspace.connection() as conn:
                    return [dict(r) for r in conn.execute(sql, args)]

            def _one(self, sql, args=()):
                found = self._all(sql, args)
                return found[0] if found else None

            def list_runs(self, limit):
                return self._all("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))

            def kv_get(self, key):
                return workspace.kv(key)
        pull_requests = PullRequests(_ReadOnlyRuns(), config)

    @app.get("/api/v1/settings/details")
    def settings_view():
        return settings_details(config)

    @app.get("/api/v1/settings/health")
    def settings_health():
        if config is None:
            raise HTTPException(404, "Configuration not loaded")
        check = getattr(actions, "health_checks", None)
        if check is None:
            from ..settings import get_health_checks as check
        safe = lambda v: v if isinstance(v, (str, bool, int, float)) or v is None else str(v)
        from .frontend_build import health_row
        rows = [*check(config), health_row()]  # the page itself may be older than its code
        return {"checks": [{k: safe(v) for k, v in row.items() if k in ("name", "status", "message", "fix", "optional")} for row in rows][:30]}

    @app.get("/api/v1/settings/models")
    def settings_models(provider: str = Query(..., max_length=100)):
        if config is None or provider not in config.providers:
            raise HTTPException(404, "Provider not configured")
        lister = getattr(actions, "provider_models", None)
        if lister is None:
            from ..settings import provider_models as lister
        found = lister(config, provider)
        return {"available": found is not None, "models": (found or [])[:2000]}

    @app.get("/api/v1/speed")
    def speed_view(n: int = Query(30, ge=5, le=200)):
        return speed(workspace, n)

    @app.get("/api/v1/benchmarks")
    def benchmark_view():
        return benchmarks(workspace)

    @app.get("/api/v1/prs")
    def pull_request_list(refresh: bool = False):
        if pull_requests is None:
            return {"configured": False, "token": False, "repos": [], "items": [], "errors": {}, "fetched_at": None}
        return pull_requests.listing(refresh=refresh)

    @app.get("/api/v1/knowledge", response_model=NotesPage)
    def knowledge(search: str = Query("", max_length=200), offset: int = Query(0, ge=0, le=100000),
                  type: str = Query("", max_length=40), repo: str = Query("", max_length=300)):
        return notes.list(search=search, offset=offset, type=type, repo=repo)

    @app.get("/api/v1/failures", response_model=FailurePage)
    def failure_list(limit: int = Query(25, ge=1, le=25), offset: int = Query(0, ge=0, le=100000),
                     category: str = Query("", max_length=60)):
        return failures(workspace, limit=limit, offset=offset, category=category)

    @app.get("/api/v1/failures/{run_id}", response_model=FailureDetail)
    def failure_detail(run_id: str):
        row = failure(workspace, run_id)
        if row is None:
            raise HTTPException(404, "Failure not recorded")
        return row

    @app.get("/api/v1/knowledge/{note_id}", response_model=NoteDetail)
    def knowledge_note(note_id: str):
        row = notes.detail(note_id)
        if row is None:
            raise HTTPException(404, "Note unavailable")
        return row

    @app.get("/api/v1/work", response_model=WorkPage)
    def work(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
             status: Literal["queued", "running", "tentative", "done", "failed", "cancelled", "skipped"] | None = None,
             search: str = Query("", max_length=200), view: Literal["all", "prs", "failures", "history"] = "all",
             archived: bool = False, fold: bool = False):
        return reader.list(limit=limit, offset=offset, status=status, search=search, view=view, archived=archived, fold=fold)

    @app.get("/api/v1/work/{item_id}", response_model=WorkItem)
    def detail(item_id: str):
        row = reader.detail(item_id)
        if row is None:
            raise HTTPException(404, "Work item not found")
        return row

    @app.get("/api/v1/work/{item_id}/evidence", response_model=WorkEvidence)
    def evidence(item_id: str, after: int = Query(0, ge=0, le=9223372036854775807),
                 limit: int = Query(50, ge=1, le=100)):
        row = reader.evidence(item_id, after=after, limit=limit)
        if row is None:
            raise HTTPException(404, "Work item not found")
        return row

    def artifact_item(item_id, expected_run=None):
        item = reader.detail(item_id)
        if item is None:
            raise HTTPException(404, "Work item not found")
        if expected_run is not None and item["run_id"] != expected_run:
            raise HTTPException(409, "Task changed attempts. Refresh task details.")
        return item

    @app.get("/api/v1/work/{item_id}/artifacts", response_model=WorkArtifacts)
    def artifact_listing(item_id: str, run_id: str | None = Query(None, max_length=128)):
        item = artifact_item(item_id, run_id)
        return {"item_id": item_id, "run_id": item["run_id"], **artifacts.listing(item["run_id"])}

    @app.get("/api/v1/work/{item_id}/report", response_model=RetainedReport)
    def retained_report(item_id: str, run_id: str | None = Query(None, max_length=128)):
        item = artifact_item(item_id, run_id)
        try:
            if item["run_id"] is None:
                raise ArtifactUnavailable("Report unavailable")
            return {"item_id": item_id, "run_id": item["run_id"], **artifacts.report(item["run_id"])}
        except ArtifactUnavailable:
            raise HTTPException(404, "Retained report unavailable") from None

    @app.get("/api/v1/work/{item_id}/screenshots/{name}")
    def screenshot(item_id: str, name: str, run_id: str | None = Query(None, max_length=128)):
        item = artifact_item(item_id, run_id)
        try:
            if item["run_id"] is None:
                raise ArtifactUnavailable("Screenshot unavailable")
            content = artifacts.screenshot(item["run_id"], name)
            return Response(content, media_type="image/png", headers={"Content-Disposition": 'inline; filename="screenshot.png"'})
        except ArtifactUnavailable:
            raise HTTPException(404, "Screenshot unavailable") from None

    brand = Path(__file__).parents[1] / "resources" / "brand"
    app.mount("/brand", StaticFiles(directory=brand), name="brand")
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets / "assets", check_dir=False), name="assets")

    @app.get("/")
    def index():
        if not (assets / "index.html").is_file():
            return JSONResponse({"detail": "Web assets are not built. Run npm ci and npm run build in frontend/. The Streamlit dashboard remains available."}, status_code=503)
        return FileResponse(assets / "index.html", headers={"Cache-Control": "no-cache"})

    return app
