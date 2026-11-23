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
    a11y: bool = False


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "page"


def parse_preview(raw) -> Preview:
    """The `preview:` section of swarmforge.project.yml (raises PreviewError with a readable reason)."""
    if not isinstance(raw, dict):
        raise PreviewError("preview must be a mapping with at least `command` and `port`")
    unknown = set(raw) - {"command", "port", "pages", "themes", "when_changed", "ready_text", "wait_s",
                          "viewport", "max_shots", "a11y"}
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
    a11y = raw.get("a11y", False)
    if not isinstance(a11y, bool):
        raise PreviewError("preview.a11y must be true or false")
    return Preview(cmd.strip(), port, pages, themes, when, raw.get("ready_text"),
                   max(10, min(int(raw.get("wait_s") or 90), 300)), viewport,
                   max(1, min(int(raw.get("max_shots") or MAX_SHOTS), 24)), a11y)


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
                        "problems": [], "text": "", "a11y_findings": []}
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
                if pv.a11y:
                    try:
                        adata = await _browse_probe(ctx, url, actions=page.actions, color_scheme=theme,
                                                    viewport=pv.viewport, a11y=True, timeout_s=90)
                        shot["a11y_findings"] = _a11y_findings_for(page.name, theme, adata)
                    except ToolError as e:
                        if log:
                            log(f"a11y audit skipped for {page.name} ({theme}): {e}")
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
    if pv.a11y and log:
        total = sum(len(s["a11y_findings"]) for s in shots)
        if total:
            log(f"a11y audit: {total} finding(s)")
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
        out.extend(s.get("a11y_findings") or [])
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


# ---------------------------------------------------------------------------
# Visual-regression snapshot gate
# ---------------------------------------------------------------------------

GOLDEN_SUBDIR = "tests/golden"
DEFAULT_TABS = ["work", "new-task", "failures", "speed", "chat", "knowledge", "settings"]
DIFF_THRESHOLD = 0.02


class RegressError(RuntimeError):
    """The visual-regression gate could not run (bad branch, no browser, ...)."""


def slugify(page: str) -> str:
    """Same slug the dashboard uses for its tabs (dashboard.SLUG)."""
    return str(page).lower().replace(" ", "-")


def golden_dir(repo: str | Path = ".") -> Path:
    return Path(repo) / GOLDEN_SUBDIR


def _png_bytes(x) -> bytes:
    if isinstance(x, (bytes, bytearray)):
        return bytes(x)
    return Path(x).read_bytes()


def pixel_diff_score(a, b, *, tol: int = 16) -> float:
    """Fraction of pixels whose grayscale difference exceeds `tol` (0.0 identical, 1.0 opposite).

    `a` and `b` are PNG file paths or PNG bytes. Images of different sizes are maximally
    different (1.0). Pillow is imported lazily so the module works without it installed.
    """
    from PIL import Image, ImageChops

    def load(x):
        return Image.open(__import__("io").BytesIO(_png_bytes(x))).convert("L")

    ia, ib = load(a), load(b)
    if ia.size != ib.size:
        return 1.0
    diff = ImageChops.difference(ia, ib)
    hist = diff.histogram()  # 256 bins of per-pixel difference
    total = ia.size[0] * ia.size[1]
    changed = sum(hist[tol + 1:])
    if total == 0:
        return 0.0
    return changed / total


def _run(cmd: list[str], *, cwd: str | Path | None = None, env: dict | None = None) -> None:
    import subprocess

    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RegressError(f"command failed ({' '.join(cmd)}):\n{r.stderr[-1500:] or r.stdout[-1500:]}")


def _streamlit_env(repo_dir: Path, db: Path) -> dict:
    import os

    env = dict(os.environ)
    env.update({
        "SWARMFORGE_CONFIG": str(repo_dir / "swarmforge.project.yml"),
        "SWARMFORGE_DB": str(db),
        "SWARMFORGE_DASH_UPDATE_CHECK": "0",
        "SWARMFORGE_DASH_AUTOREFRESH": "0",
        "PYTHONPATH": str(repo_dir / "src") + os.pathsep + env.get("PYTHONPATH", ""),
    })
    return env


def _wait_http(port: int, timeout_s: float = 60.0) -> None:
    import time
    import urllib.request

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
            return
        except OSError:
            time.sleep(0.5)
    raise RegressError(f"dashboard on port {port} did not answer HTTP within {timeout_s:.0f}s")


def capture_tabs(repo_dir: str | Path, out_dir: str | Path, *, tabs=None, themes=("light", "dark"),
                 port: int = 8699, timeout_s: int = 180, viewport=None) -> dict[str, Path]:
    """Screenshot every dashboard tab in every theme from a real headless Chromium.

    Seeds demo data, starts the Streamlit dashboard from `repo_dir`, and saves one full-page
    PNG per tab+theme to `out_dir/{slug}-{theme}.png`. Returns {"slug-theme": Path}.
    """
    import os
    import signal
    import subprocess
    import sys
    import tempfile

    repo_dir = Path(repo_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tabs = [slugify(t) for t in (tabs if tabs is not None else DEFAULT_TABS)]
    themes = tuple(themes) or ("light", "dark")
    tmp_db_dir = Path(tempfile.mkdtemp(prefix="sf-visual-demo-"))
    db = tmp_db_dir / "visual-demo.sqlite"
    env = _streamlit_env(repo_dir, db)
    try:
        _run([sys.executable, "-m", "swarmforge.cli", "seed-demo", str(db)], cwd=repo_dir, env=env)
    except BaseException:
        import shutil

        shutil.rmtree(tmp_db_dir, ignore_errors=True)
        raise
    cmd = [sys.executable, "-m", "streamlit", "run", "src/swarmforge/dashboard.py",
           "--server.headless", "true", "--server.port", str(port), "--server.address", "127.0.0.1",
           "--client.toolbarMode", "viewer", "--server.fileWatcherType", "none",
           "--browser.gatherUsageStats", "false"]
    proc = subprocess.Popen(cmd, cwd=repo_dir, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    shots: dict[str, Path] = {}
    try:
        _wait_http(port, timeout_s=min(timeout_s, 60))
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover - depends on the sandbox
            raise RegressError("playwright is not installed; run `playwright install chromium`") from e
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for theme in themes:
                    for tab in tabs:
                        page = browser.new_page(color_scheme=theme,
                                                viewport=viewport or {"width": 1400, "height": 1000})  # type: ignore[arg-type]
                        try:
                            page.goto(f"http://127.0.0.1:{port}/?page={tab}",
                                      wait_until="networkidle", timeout=timeout_s * 1000)
                            page.wait_for_timeout(1500)  # let Streamlit settle
                            path = out_dir / f"{tab}-{theme}.png"
                            page.screenshot(path=str(path), full_page=True)
                            shots[f"{tab}-{theme}"] = path
                        finally:
                            page.close()
            finally:
                browser.close()
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.wait()
        import shutil

        shutil.rmtree(tmp_db_dir, ignore_errors=True)
    return shots


def _default_capture(repo_dir, out_dir, *, tabs, themes, port=None):
    return capture_tabs(repo_dir, out_dir, tabs=tabs, themes=themes, port=port or 8699)


def _worktree(repo: str | Path, branch: str, name: str) -> Path:
    import subprocess
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix=f"sf-visual-{name}-"))
    tmp.rmdir()  # git wants to create it
    r = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(tmp), branch],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RegressError(f"cannot check out branch '{branch}' for visual regression:\n{r.stderr[-1500:]}")
    return tmp


def _remove_worktree(repo: str | Path, wt: Path) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                   capture_output=True, text=True, check=False)


def _finding(slug: str, theme: str, score: float, threshold: float) -> dict:
    pct = score * 100
    return {
        "severity": "major", "page": slug, "theme": theme, "blocking": True,
        "golden_regression": True,
        "title": (f"visual regression on tab '{slug}' ({theme}): {pct:.1f}% of pixels differ "
                  f"(> {threshold * 100:.0f}%)"),
        "description": (f"Tab '{slug}' in the {theme} theme changed by {pct:.1f}% of pixels, above the "
                        f"{threshold * 100:.0f}% visual-regression threshold."),
        "recommendation": ("if the change is intended run `swarmforge visual update-golden`; "
                           "otherwise fix the UI"),
    }


def regress(pr_branch: str, base_branch: str, *, repo: str | Path = ".", golden_path=None,
            tabs=None, themes=("light", "dark"), threshold: float = DIFF_THRESHOLD,
            port=None, capture=None, keep_dirs: bool = False) -> dict:
    """Screenshot every tab on both branches and diff the pixels.

    Returns {ok, threshold, max_score, tabs, findings, shots}. Any tab whose diff (vs the base
    branch, and vs the golden PNG when one exists) exceeds `threshold` gets a blocking [major]
    finding. `capture` overrides the real browser capture (tests use it to stay offline).
    """
    repo = Path(repo)
    capture = capture or _default_capture
    tabs = [slugify(t) for t in (tabs if tabs is not None else DEFAULT_TABS)]
    themes = tuple(themes) or ("light", "dark")
    pr_wt = _worktree(repo, pr_branch, "pr")
    base_wt = _worktree(repo, base_branch, "base")
    try:
        import shutil
        import tempfile

        pr_dir = Path(tempfile.mkdtemp(prefix="sf-visual-pr-shots-"))
        base_dir = Path(tempfile.mkdtemp(prefix="sf-visual-base-shots-"))
        try:
            pr_shots = capture(pr_wt, pr_dir, tabs=tabs, themes=themes, port=port)
            base_shots = capture(base_wt, base_dir, tabs=tabs, themes=themes, port=port)
            findings: list[dict] = []
            rows: list[dict] = []
            scores: list[float] = []
            shots_out: dict[str, str] = {}
            gdir = Path(golden_path) if golden_path else golden_dir(repo)
            for theme in themes:
                for tab in tabs:
                    key = f"{tab}-{theme}"
                    pr_shot = pr_shots.get(key)
                    base_shot = base_shots.get(key)
                    if pr_shot is None:
                        continue
                    shots_out[key] = str(pr_shot)
                    if base_shot is None:
                        score, status = 1.0, "missing"
                    else:
                        score = pixel_diff_score(pr_shot, base_shot)
                        status = "clean" if score <= threshold else "diff"
                    golden = gdir / f"{key}.png"
                    if golden.exists():
                        score = max(score, pixel_diff_score(pr_shot, golden))
                        if score > threshold:
                            status = "diff"
                    scores.append(score)
                    rows.append({"tab": tab, "theme": theme, "score": score, "status": status})
                    if score > threshold:
                        findings.append(_finding(tab, theme, score, threshold))
            result = {"ok": not findings, "threshold": threshold,
                      "max_score": max(scores or [0.0]), "tabs": rows, "findings": findings,
                      "shots": shots_out}
        finally:
            if not keep_dirs:
                shutil.rmtree(pr_dir, ignore_errors=True)
                shutil.rmtree(base_dir, ignore_errors=True)
        return result
    finally:
        if not keep_dirs:
            _remove_worktree(repo, pr_wt)
            _remove_worktree(repo, base_wt)


def update_golden(*, repo: str | Path = ".", branch: str | None = None, tabs=None,
                  themes=("light", "dark"), port=None, capture=None) -> list[Path]:
    """Refresh the golden PNGs under tests/golden from `branch` (or the current checkout)."""
    import shutil

    repo = Path(repo)
    capture = capture or _default_capture
    tabs = [slugify(t) for t in (tabs if tabs is not None else DEFAULT_TABS)]
    themes = tuple(themes) or ("light", "dark")
    gdir = golden_dir(repo)
    gdir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if branch:
        src_wt = _worktree(repo, branch, "golden")
        try:
            import tempfile

            shots_dir = Path(tempfile.mkdtemp(prefix="sf-visual-golden-"))
            try:
                shots = capture(src_wt, shots_dir, tabs=tabs, themes=themes, port=port)
                for key, src_png in shots.items():
                    dst = gdir / f"{key}.png"
                    shutil.copyfile(src_png, dst)
                    written.append(dst)
            finally:
                shutil.rmtree(shots_dir, ignore_errors=True)
        finally:
            _remove_worktree(repo, src_wt)
    else:
        import tempfile

        shots_dir = Path(tempfile.mkdtemp(prefix="sf-visual-golden-"))
        try:
            shots = capture(repo, shots_dir, tabs=tabs, themes=themes, port=port)
            for key, src_png in shots.items():
                dst = gdir / f"{key}.png"
                shutil.copyfile(src_png, dst)
                written.append(dst)
        finally:
            shutil.rmtree(shots_dir, ignore_errors=True)
    return written


def golden_gate(shots: list[dict], repo: str | Path = ".", *, threshold: float = DIFF_THRESHOLD) -> list[dict]:
    """Orchestrator hook: compare capture() shots against the golden PNGs, if any.

    `shots` are the dicts `capture()` returns (keys page/theme/file). With no golden images
    the gate is a no-op; a shot whose golden is missing is skipped silently.
    """
    repo = Path(repo)
    gdir = golden_dir(repo)
    if not gdir.is_dir() or not any(gdir.glob("*.png")):
        return []
    findings: list[dict] = []
    for s in shots or []:
        slug = slugify(s.get("page") or "")
        theme = str(s.get("theme") or "light")
        golden = gdir / f"{slug}-{theme}.png"
        shot_file = s.get("file")
        if not golden.exists() or not shot_file or not Path(shot_file).exists():
            continue
        score = pixel_diff_score(shot_file, golden)
        if score > threshold:
            findings.append(_finding(slug, theme, score, threshold))
    return findings


def maybe_refresh_golden_after_update(repo: str | Path = ".", *, capture=None) -> dict | None:
    """After merged main arrives: refresh the goldens, but only if the repo already has some.

    Returns None when there is nothing to refresh, {'refreshed': True, 'files': [...]} on
    success, or {'refreshed': False, 'reason': ...} when the refresh failed.
    """
    gdir = golden_dir(repo)
    if not gdir.is_dir() or not any(gdir.glob("*.png")):
        return None
    try:
        files = update_golden(repo=repo, capture=capture)
        return {"refreshed": True, "files": [str(f) for f in files]}
    except (RegressError, OSError) as e:
        return {"refreshed": False, "reason": str(e)}


# ------------------------------------------------------------- layout checks
# Responsive-layout checks: screenshot a tab at phone/tablet/desktop widths and
# look for DOM overflow/clipping, so mobile regressions are caught by tests
# instead of manual inspection.

BREAKPOINT_WIDTHS = (375, 768, 1280)

OVERFLOW_JS = """
(width) => {
    const doc = document.documentElement || null;
    const overflowing = [];
    const clipped = [];
    const selector = (el) => {
        let s = (el.tagName || "*").toLowerCase();
        if (el.id) s += "#" + el.id;
        if (el.classList && el.classList.length) s += "." + el.classList[0];
        return s.length > 60 ? s.slice(0, 57) + "..." : s;
    };
    let els = [];
    try { els = document.querySelectorAll("body *"); } catch (e) { els = []; }
    for (const el of els) {
        try {
            const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
            if (!rect || !(rect.width > 0) || !(rect.height > 0)) continue;
            let style = null;
            try { style = getComputedStyle(el); } catch (e) { style = null; }
            if (style && (style.display === "none" || style.visibility === "hidden")) continue;
            if (rect.right > width + 2 || rect.left < -2) {
                overflowing.push({selector: selector(el), width: rect.width,
                                   left: rect.left, right: rect.right});
            }
            const sw = el.scrollWidth || 0, cw = el.clientWidth || 0;
            if (sw - cw > 2 && style &&
                (style.overflowX === "hidden" || style.overflowX === "clip")) {
                clipped.push({selector: selector(el), scroll_width: sw, client_width: cw});
            }
        } catch (e) { /* skip unreadable elements */ }
    }
    return {viewport: width, doc_scroll_width: doc ? (doc.scrollWidth || 0) : 0,
            overflowing: overflowing, clipped: clipped};
}
"""


def breakpoint_findings(
    tab: str, width: int, dom: dict, *, theme: str = "light"
) -> list[dict]:
    """Convert one OVERFLOW_JS payload into review findings (empty list when clean)."""
    page = slugify(tab)
    overflowing = list(dom.get("overflowing") or [])
    clipped = list(dom.get("clipped") or [])
    doc_scroll = int(dom.get("doc_scroll_width") or 0)
    if doc_scroll > width + 2 or overflowing:
        parts = []
        if doc_scroll > width + 2:
            parts.append(f"the document is {doc_scroll}px wide at a {width}px viewport")
        for o in overflowing[:5]:
            parts.append(
                f"{o.get('selector')} (width {o.get('width'):.0f}px, "
                f"right edge {o.get('right'):.0f}px)"
            )
        return [
            {
                "severity": "major",
                "page": page,
                "file": f"{page} ({int(width)}px)",
                "theme": theme,
                "blocking": True,
                "layout_overflow": True,
                "width": int(width),
                "title": f"layout overflow at {width}px viewport",
                "description": (
                    f"Tab '{tab}' overflows a {width}px viewport: " + "; ".join(parts)
                ),
                "recommendation": (
                    f"make the {tab} tab responsive at {width}px "
                    f"(flex-wrap, min-width:0, overflow handling)"
                ),
            }
        ]
    if clipped:
        parts = [
            f"{c.get('selector')} (content {c.get('scroll_width')}px in a "
            f"{c.get('client_width')}px box)"
            for c in clipped[:5]
        ]
        return [
            {
                "severity": "minor",
                "page": page,
                "file": f"{page} ({int(width)}px)",
                "theme": theme,
                "blocking": False,
                "layout_overflow": True,
                "width": int(width),
                "title": f"content clipped at {width}px viewport",
                "description": (
                    f"Tab '{tab}' clips content at a {width}px viewport: "
                    + "; ".join(parts)
                ),
                "recommendation": (
                    f"let the {tab} tab's clipped content wrap or scroll "
                    f"at {width}px instead of hiding it"
                ),
            }
        ]
    return []


def _default_open_page():
    """Real-browser page factory: one headless Chromium page per viewport width."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - depends on the sandbox
        raise RegressError(
            "playwright is not installed; run `playwright install chromium`"
        ) from e

    def open_page(width: int, theme: str):
        pw = sync_playwright().start()
        browser = pw.chromium.launch()
        page = browser.new_page(
            color_scheme=theme,  # type: ignore[arg-type]
            viewport={"width": int(width), "height": 900},
        )

        class _Page:
            def goto(self, url, **kw):
                page.goto(url, **kw)

            def wait_for_timeout(self, ms):
                page.wait_for_timeout(ms)

            def screenshot(self, path, **kw):
                page.screenshot(path=str(path), **kw)

            def evaluate(self, js, arg=None):
                return page.evaluate(js, arg)

            def close(self):
                try:
                    page.close()
                finally:
                    browser.close()
                    pw.stop()

        return _Page()

    return open_page


def render_breakpoints(
    tab: str,
    widths=BREAKPOINT_WIDTHS,
    *,
    repo: str | Path = ".",
    url: str | None = None,
    out_dir: str | Path | None = None,
    port: int | None = None,
    theme: str = "light",
    timeout_s: int = 180,
    open_page=None,
) -> dict:
    """Screenshot `tab` at each viewport width and check the DOM for overflow/clipping.

    Returns a JSON-safe report {ok, tab, widths, results, findings, error}. `url` skips the
    local dashboard launch (tests inject their own page this way); `open_page(width, theme)`
    overrides the real browser (offline tests pass a fake).
    """
    import os
    import shutil
    import signal
    import socket
    import subprocess
    import sys
    import tempfile

    if not str(tab or "").strip():
        raise RegressError("tab must not be empty")
    widths = list(widths)
    if not widths:
        raise RegressError("widths must not be empty")
    for w in widths:
        if not isinstance(w, int) or isinstance(w, bool) or not 240 <= w <= 3000:
            raise RegressError(
                f"invalid viewport width {w!r}: use integers between 240 and 3000"
            )
    if theme not in ("light", "dark"):
        raise RegressError(f"unknown theme {theme!r}: expected 'light' or 'dark'")

    slug = slugify(tab)
    auto_out = not out_dir
    out_dir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="layout-"))
    out_dir.mkdir(parents=True, exist_ok=True)

    proc = None
    tmp_db_dir = None
    try:
        if url is None:
            if port is None:
                with socket.socket() as s:
                    s.bind(("127.0.0.1", 0))
                    port = s.getsockname()[1]
            repo_dir = Path(repo)
            tmp_db_dir = Path(tempfile.mkdtemp(prefix="sf-layout-demo-"))
            db = tmp_db_dir / "layout-demo.sqlite"
            env = _streamlit_env(repo_dir, db)
            _run(
                [sys.executable, "-m", "swarmforge.cli", "seed-demo", str(db)],
                cwd=repo_dir,
                env=env,
            )
            cmd = [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                "src/swarmforge/dashboard.py",
                "--server.headless",
                "true",
                "--server.port",
                str(port),
                "--server.address",
                "127.0.0.1",
                "--client.toolbarMode",
                "viewer",
                "--server.fileWatcherType",
                "none",
                "--browser.gatherUsageStats",
                "false",
            ]
            proc = subprocess.Popen(
                cmd,
                cwd=repo_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            _wait_http(port, timeout_s=min(timeout_s, 60))
            url = f"http://127.0.0.1:{port}/?page={slug}"

        if open_page is None:
            open_page = _default_open_page()

        results = []
        findings: list[dict] = []
        error: str | None = None
        for w in widths:
            shot = out_dir / f"{slug}-{w}px-{theme}.png"
            try:
                page = open_page(w, theme)
                try:
                    page.goto(url, wait_until="networkidle", timeout=timeout_s * 1000)
                    page.wait_for_timeout(350)
                    page.screenshot(shot, full_page=True)
                    dom = page.evaluate(OVERFLOW_JS, w) or {}
                finally:
                    page.close()
                fs = breakpoint_findings(tab, w, dom, theme=theme)
                status = "overflow" if any(f["blocking"] for f in fs) else "clean"
                findings.extend(fs)
                results.append(
                    {
                        "width": w,
                        "shot": str(shot),
                        "status": status,
                        "doc_scroll_width": int(dom.get("doc_scroll_width") or 0),
                        "overflowing": list(dom.get("overflowing") or []),
                        "clipped": list(dom.get("clipped") or []),
                    }
                )
            except Exception as e:  # noqa: BLE001 - any browser failure becomes a finding
                error = str(e)
                findings.append(
                    {
                        "severity": "critical",
                        "page": slug,
                        "file": f"{slug} ({w}px)",
                        "theme": theme,
                        "blocking": True,
                        "layout_overflow": True,
                        "width": w,
                        "title": f"layout check failed at {w}px viewport",
                        "description": f"could not render the {tab} tab at {w}px: {str(e)[:500]}",
                        "recommendation": f"check the {tab} tab renders at {w}px and rerun the layout check",
                    }
                )
                results.append(
                    {
                        "width": w,
                        "shot": str(shot),
                        "status": "error",
                        "doc_scroll_width": 0,
                        "overflowing": [],
                        "clipped": [],
                    }
                )
        return {
            "ok": not findings,
            "tab": slug,
            "widths": list(widths),
            "results": results,
            "findings": findings,
            "error": error,
        }
    finally:
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()
        if tmp_db_dir is not None:
            shutil.rmtree(tmp_db_dir, ignore_errors=True)
        if auto_out:
            shutil.rmtree(out_dir, ignore_errors=True)


def record_breakpoint_findings(
    store,
    run_id: str,
    report: dict,
    *,
    cycle: int = 0,
    source: str = "layout",
    line: int | None = None,
) -> int:
    """Persist a render_breakpoints report's findings; returns how many were recorded."""
    n = 0
    for f in report.get("findings") or []:
        store.record_finding(
            run_id,
            cycle=cycle,
            source=source,
            severity=f["severity"],
            file=f"{f['page']} ({f.get('width', '')}px)".strip(),
            line=line,
            title=f["title"],
            description=f["description"],
            recommendation=f["recommendation"],
        )
        n += 1
    return n


# ------------------------------------------------------------- a11y audit
async def _browse_probe(ctx, url, *, actions=None, screenshot_name=None, color_scheme=None,
                        viewport=None, full_page=True, timeout_s=60, a11y=False) -> dict:
    """Local copy of the sandbox browser probe (devtools.browse_raw) with an opt-in a11y flag.

    With `a11y=True` the probe also returns `a11y` (the Playwright accessibility snapshot, or
    None) and `contrast` (a list of {selector, text, color, background} pairs for visible text)."""
    from .agent.devtools import (
        BROWSE_MARK,
        LOCAL_URL_RE,
        NO_BROWSER,
        _browse_script,
        open_session,
    )
    from .agent.tools import ToolError
    if not LOCAL_URL_RE.match(url or ""):
        raise ToolError("browse only opens local apps, e.g. http://localhost:8501 (start the app with "
                        "start_process first). The sandbox has no internet access.")
    if actions is not None and not isinstance(actions, list):
        raise ToolError("actions must be a list of objects, e.g. [{\"click\": \"text=Save\"}]")
    if screenshot_name is not None and not re.fullmatch(r"[\w.-]+/?[\w.-]*\.png", screenshot_name):
        raise ToolError(f"bad screenshot name {screenshot_name!r}")
    s = open_session(ctx)
    args = {"url": url, "actions": actions or [], "timeout_ms": max(5, min(int(timeout_s or 60), 180)) * 1000,
            "screenshot": (f"{s.artifacts_path}/{screenshot_name}" if screenshot_name and s.artifacts_path else None),
            "color_scheme": color_scheme if color_scheme in ("light", "dark") else None,
            "viewport": viewport or None, "full_page": full_page}
    if a11y:
        args["a11y"] = True
    r = await s.exec(f"{s.python} -", stdin=_browse_script(), env={"SF_BROWSE_ARGS": json.dumps(args)},
                     timeout_s=args["timeout_ms"] // 1000 + 60)
    ctx.commands_run.append({"command": f"[browse] {url}", "exit_code": r.exit_code,
                             "timed_out": r.timed_out, "duration_s": round(r.duration_s, 2)})
    if r.timed_out:
        raise ToolError(f"the browser timed out after {r.duration_s:.0f}s")
    line = next((ln for ln in reversed(r.output.splitlines()) if ln.startswith(BROWSE_MARK)), None)
    if line is None:
        if "No module named 'playwright'" in r.output or "Executable doesn't exist" in r.output:
            raise ToolError(NO_BROWSER)
        raise ToolError(f"the browser failed: {r.output[-1500:]}")
    data = json.loads(line[len(BROWSE_MARK):])
    if data.get("fatal"):
        msg = data["fatal"]
        if "Executable doesn't exist" in msg:
            raise ToolError(NO_BROWSER)
        raise ToolError(f"could not load {url}: {msg}")
    data["screenshot_file"] = None
    if data.get("screenshot") and ctx.artifacts_dir is not None and screenshot_name:
        f = Path(ctx.artifacts_dir) / screenshot_name
        data["screenshot_file"] = str(f) if f.is_file() else None
    return data


def audit(tab_url, **kwargs):
    """Accessibility audit of a served page (Playwright a11y snapshot + WCAG AA contrast).
    See swarmforge.a11y.audit for options; returns {ok, url, tab, themes, results, findings, error}."""
    from . import a11y
    return a11y.audit(tab_url, **kwargs)


def _a11y_findings_for(tab, theme, data, *, min_ratio=4.5) -> list[dict]:
    """Finding dicts from one probe result. F1: an EMPTY-but-successful a11y snapshot
    ({} or '') is still processed; only None (probe failure) skips label findings."""
    from . import a11y
    out: list[dict] = []
    if data.get("a11y") is not None:
        out.extend(a11y.label_findings(tab, theme, data["a11y"]))
    out.extend(a11y.contrast_findings(tab, theme, data.get("contrast") or [], min_ratio=min_ratio))
    return out


async def audit_pages(ctx, pv, *, log=None, min_ratio=4.5) -> list[dict]:
    """Run the accessibility audit against every page/theme of a preview config.

    Returns a list of Finding dicts (missing accessible names [major], low contrast [minor])
    consumable by state.repair_tasks_from_findings. Never raises on per-page failures;
    the preview process is always stopped."""
    from .agent.devtools import start_process, stop_process
    from .agent.tools import ToolError

    findings: list[dict] = []
    await start_process(ctx, "preview", pv.command, ready_port=pv.port, ready_text=pv.ready_text,
                        wait_s=pv.wait_s)
    try:
        for page in pv.pages:
            for theme in pv.themes:
                try:
                    data = await _browse_probe(ctx, f"http://localhost:{pv.port}{page.path}",
                                               actions=page.actions, color_scheme=theme,
                                               viewport=pv.viewport, a11y=True, timeout_s=90)
                except ToolError as e:
                    if log:
                        log(f"a11y audit skipped for {page.name} ({theme}): {e}")
                    continue
                findings.extend(_a11y_findings_for(page.name, theme, data, min_ratio=min_ratio))
    finally:
        try:
            await stop_process(ctx, "preview")
        except ToolError:
            pass
    return findings
