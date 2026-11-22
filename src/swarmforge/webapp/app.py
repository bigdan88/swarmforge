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
from .contracts import ActionCommand, ActionResult
from .workspace import WorkspaceReader, feature_history, public_settings
from .notes import NotesReader
from .contracts import (ProjectPage, ProjectDetail, ConversationPage, ConversationDetail,
                        WorkControls, QueueSnapshot, NotesPage, NoteDetail)


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
            if actions is None or request.method != "POST" or request.url.path != "/api/v1/actions":
                return JSONResponse({"detail": "This preview is read-only"}, status_code=405)
            if origin != str(request.base_url).rstrip("/") or not session.authorized(request.cookies.get(session.cookie), request.headers.get("x-csrf-token")):
                return JSONResponse({"detail": "Refresh this page to authorize local controls"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "JSON requests are required"}, status_code=415)
            try:
                size = int(request.headers.get("content-length", "-1"))
            except ValueError:
                size = -1
            if not 0 <= size <= 128000:
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
                   "csrf_token": None, "settings": public_settings(config), "demo_mode": demo}
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
        return {"items": feature_history()[:100]}

    @app.get("/api/v1/knowledge", response_model=NotesPage)
    def knowledge(search: str = Query("", max_length=200), offset: int = Query(0, ge=0, le=100000)):
        return notes.list(search=search, offset=offset)

    @app.get("/api/v1/knowledge/{note_id}", response_model=NoteDetail)
    def knowledge_note(note_id: str):
        row = notes.detail(note_id)
        if row is None:
            raise HTTPException(404, "Note unavailable")
        return row

    @app.get("/api/v1/work", response_model=WorkPage)
    def work(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
             status: Literal["queued", "running", "tentative", "done", "failed", "cancelled", "skipped"] | None = None,
             search: str = Query("", max_length=200), view: Literal["all", "prs", "failures", "history"] = "all"):
        return reader.list(limit=limit, offset=offset, status=status, search=search, view=view)

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
