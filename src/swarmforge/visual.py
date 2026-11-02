"""Screenshots and visual review of UI changes.

After a cycle's tests, SwarmForge itself (not an agent) starts the app the way the repository's
`swarmforge.project.yml` says (`preview:`), opens the listed pages in the sandbox's headless Chromium
in light and dark, and saves full-page screenshots to the run's artifacts. A vision-capable model
(the `designer` role) then looks at them next to the task, and reports what a user would see wrong.

Only obvious breakage blocks the run (severity "critical": an error on the page, a blank or broken
page, unreadable text); everything else is listed in the pull request for a human to judge.

    preview:                       # in the repository's swarmforge.project.yml
      command: streamlit run app.py --server.headless true --server.port 8501
      port: 8501
      pages: ["/", {path: "/?page=settings", name: settings}]
      themes: [light, dark]        # default: both
      when_changed: ["*.py", "static/*"]   # only when the change touches these (default: always)
      viewport: {width: 1400, height: 1800}
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

THEMES = ("light", "dark")
SEVERITIES = ("critical", "major", "minor", "info")
MAX_SHOTS = 12


class PreviewError(ValueError):
    pass


@dataclass
class Page:
    path: str
    name: str
    actions: list = field(default_factory=list)


@dataclass
class Preview:
    command: str
    port: int
    pages: list[Page]
    themes: tuple[str, ...] = THEMES
    when_changed: list[str] = field(default_factory=list)
    ready_text: str | None = None
    wait_s: int = 90
    viewport: dict = field(default_factory=lambda: {"width": 1400, "height": 1000})
    max_shots: int = MAX_SHOTS


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "page"


def parse_preview(raw) -> Preview:
    """The `preview:` section of swarmforge.project.yml (raises PreviewError with a readable reason)."""
    if not isinstance(raw, dict):
        raise PreviewError("preview must be a mapping with at least `command` and `port`")
    unknown = set(raw) - {"command", "port", "pages", "themes", "when_changed", "ready_text", "wait_s",
                          "viewport", "max_shots"}
    if unknown:
        raise PreviewError(f"unknown preview keys: {sorted(unknown)}")
    cmd = raw.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        raise PreviewError("preview.command must be the command that starts the app")
    port = raw.get("port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise PreviewError("preview.port must be the port the app listens on")
    pages = []
    for p in raw.get("pages") or ["/"]:
        if isinstance(p, str):
            p = {"path": p}
        if not isinstance(p, dict) or not isinstance(p.get("path"), str) or not p["path"].startswith("/"):
            raise PreviewError("each preview page is a path like '/?page=work' or {path, name, actions}")
        acts = p.get("actions") or []
        if not isinstance(acts, list) or not all(isinstance(a, dict) for a in acts):
            raise PreviewError("preview page actions must be a list of objects like {click: 'text=Save'}")
        m = re.search(r"[?&]page=([^&#]+)", p["path"])
        default = m.group(1) if m else (p["path"].strip("/?") or "home")
        pages.append(Page(p["path"], str(p.get("name") or _slug(default)), acts))
    themes = tuple(raw.get("themes") or THEMES)
    if not themes or not set(themes) <= set(THEMES):
        raise PreviewError("preview.themes must be a list of light/dark")
    when = raw.get("when_changed") or []
    if not isinstance(when, list) or not all(isinstance(w, str) for w in when):
        raise PreviewError("preview.when_changed must be a list of file patterns")
    vp = raw.get("viewport") or {}
    if not isinstance(vp, dict):
        raise PreviewError("preview.viewport must be {width, height}")
    viewport = {"width": int(vp.get("width") or 1400), "height": int(vp.get("height") or 1000)}
    if not (320 <= viewport["width"] <= 3000 and 300 <= viewport["height"] <= 6000):
        raise PreviewError("preview.viewport is out of range (width 320-3000, height 300-6000)")
    return Preview(cmd.strip(), port, pages, themes, when, raw.get("ready_text"),
                   max(10, min(int(raw.get("wait_s") or 90), 300)), viewport,
                   max(1, min(int(raw.get("max_shots") or MAX_SHOTS), 24)))


def should_capture(pv: Preview, changed_files: list[str]) -> bool:
    """True when the change touches files the preview watches (or it watches everything)."""
    if not pv.when_changed:
        return True
    return any(fnmatch.fnmatch(f, pat) for f in changed_files for pat in pv.when_changed)


async def capture(ctx, pv: Preview, cycle: int, log=None) -> list[dict]:
    """Start the app in the sandbox, screenshot every page in every theme, stop it.

    Returns one dict per shot: page, theme, url, file (PNG on the host, or None), problems (errors
    shown on the page, uncaught JS errors, console errors, failed requests), text (start of the page)."""
    from .agent.devtools import browse_raw, open_session, start_process, stop_process
    from .agent.tools import ToolError
    status = await start_process(ctx, "preview", pv.command, ready_port=pv.port, ready_text=pv.ready_text,
                                 wait_s=pv.wait_s)
    if "RUNNING and ready" not in status:
        raise ToolError("the app did not come up for screenshots:\n" + status[-1500:])
    # The port being open does not mean the server answers HTTP yet (cold start, port race);
    # poll briefly so an unusable preview becomes a PreviewError (skipped) instead of
    # critical "could not load the page" findings for pages that are actually fine.
    session = open_session(ctx)
    probe = (f"{session.python} -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen("
             f"'http://localhost:{pv.port}{pv.pages[0].path}', timeout=5).status < 500 else 1)\"")
    for _ in range(10):
        r = await session.exec(probe, timeout_s=15)
        if r.exit_code == 0:
            break
        await asyncio.sleep(0.5)
    else:
        try:
            await stop_process(ctx, "preview")
        except ToolError:
            pass
        raise PreviewError(f"preview server on port {pv.port} is not answering HTTP requests")
    shots: list[dict] = []
    try:
        for page in pv.pages:
            for theme in pv.themes:
                if len(shots) >= pv.max_shots:
                    break
                url = f"http://localhost:{pv.port}{page.path}"
                name = f"visual/c{cycle}-{_slug(page.name)}-{theme}.png"
                shot: dict = {"page": page.name, "path": page.path, "theme": theme, "url": url, "file": None,
                        "problems": [], "text": ""}
                try:
                    data = await browse_raw(ctx, url, actions=page.actions, screenshot_name=name,
                                            color_scheme=theme, viewport=pv.viewport, timeout_s=90)
                    shot["file"] = data.get("screenshot_file")
                    crashes = data.get("exceptions") or []
                    shot["problems"] = ([f"exception on the page: {e}" for e in crashes]
                                        + [f"error message on the page: {e}" for e in data.get("app_errors") or []
                                           if e not in crashes]
                                        + [f"uncaught page error: {e}" for e in data.get("page_errors") or []]
                                        + [f"action failed: {e}" for e in data.get("action_errors") or []]
                                        + [f"console {e}" for e in data.get("console") or [] if e.startswith("error")])
                    shot["text"] = (data.get("text") or "")[:1500]
                except ToolError as e:
                    shot["problems"] = [f"could not load the page: {e}"]
                shots.append(shot)
                if log:
                    log(f"screenshot {page.name} ({theme}): "
                        + ("saved" if shot["file"] else "not saved")
                        + (f"; {len(shot['problems'])} problem(s) on the page" if shot["problems"] else ""))
    finally:
        try:
            await stop_process(ctx, "preview")
        except ToolError:
            pass
    return shots


def page_error_findings(shots: list[dict]) -> list[dict]:
    """Objective breakage seen by the browser itself: no model needed, always critical."""
    out = []
    for s in shots:
        bad = [p for p in s["problems"] if p.startswith(("exception on the page", "uncaught page error",
                                                          "could not load the page"))]
        if bad:
            out.append({"severity": "critical", "page": s["page"], "theme": s["theme"],
                        "title": f"{s['page']} ({s['theme']}) shows an error",
                        "description": "\n".join(bad)[:1500],
                        "recommendation": f"Open {s['path']} and fix the error shown there."})
    return out


def _data_url(path: str) -> str:
    return "data:image/png;base64," + base64.b64encode(Path(path).read_bytes()).decode()


def review_messages(system: str, title: str, body: str, changed: list[str], shots: list[dict]) -> list[dict]:
    blocks: list[dict] = [{"type": "text", "text": (
        f"Task that was just implemented: {title}\n\n{(body or '')[:3000]}\n\n"
        f"Files changed: {', '.join(changed[:40]) or '(none)'}\n\n"
        f"Screenshots follow ({sum(1 for s in shots if s['file'])}), each preceded by its page and theme.")}]
    for s in shots:
        if not s["file"]:
            continue
        caption = f"Page '{s['page']}' ({s['path']}), {s['theme']} theme."
        if s["problems"]:
            caption += " The browser reported: " + "; ".join(s["problems"])[:600]
        blocks.append({"type": "text", "text": caption})
        blocks.append({"type": "image_url", "image_url": {"url": _data_url(s["file"])}})
    return [{"role": "system", "content": system}, {"role": "user", "content": blocks}]


def parse_review(text: str) -> tuple[str, list[dict]]:
    """(summary, findings) from the model's JSON answer (tolerates code fences and chatter around it)."""
    m = re.search(r"\{.*\}", text or "", flags=re.DOTALL)
    if not m:
        raise ValueError("the visual reviewer did not answer with JSON")
    data = json.loads(m.group(0))
    findings = []
    for f in data.get("findings") or []:
        if not isinstance(f, dict) or not f.get("title"):
            continue
        sev = str(f.get("severity") or "minor").lower()
        findings.append({"severity": sev if sev in SEVERITIES else "minor", "page": str(f.get("page") or ""),
                         "theme": str(f.get("theme") or ""), "title": str(f["title"])[:200],
                         "description": str(f.get("description") or "")[:1500],
                         "recommendation": str(f.get("recommendation") or "")[:800]})
    return str(data.get("summary") or "")[:1000], findings


def is_blocking(severity: str) -> bool:
    """Visual findings block only on obvious breakage, whatever review.blocking_severities says."""
    return severity == "critical"
