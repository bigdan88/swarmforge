"""Keep the built React app in step with its sources.

The built files (src/swarmforge/resources/web) are not in git, so pulling new code leaves the old
interface in place until someone rebuilds it: new buttons silently "don't exist". Each build records a
hash of the frontend sources next to its output. When the web interface starts from a source checkout
and the hash no longer matches, it rebuilds (npm) before serving; if it cannot, it says so loudly, and
Settings → Health shows it.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "resources" / "web"
STAMP = WEB / ".source-sha256"
FRONTEND = Path(__file__).resolve().parents[3] / "frontend"
SOURCES = ("src", "index.html", "package.json", "package-lock.json", "vite.config.ts", "tsconfig.json")


def frontend_dir() -> Path | None:
    """The frontend sources, when running from a checkout (installed wheels ship only the build)."""
    return FRONTEND if (FRONTEND / "package.json").is_file() and (FRONTEND / "src").is_dir() else None


def source_hash(frontend: Path) -> str:
    """Hash of everything the build reads. Line endings are normalized: a Windows checkout (CRLF) and
    the same commit elsewhere (LF) hash the same."""
    digest = hashlib.sha256()
    files: list[Path] = []
    for name in SOURCES:
        path = frontend / name
        if path.is_dir():
            files += [p for p in path.rglob("*") if p.is_file()]
        elif path.is_file():
            files.append(path)
    for path in sorted(files, key=lambda p: p.relative_to(frontend).as_posix()):
        digest.update(path.relative_to(frontend).as_posix().encode() + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def status() -> tuple[str, str]:
    """("current" | "stale" | "missing" | "no-source", explanation)."""
    if not (WEB / "index.html").is_file():
        return "missing", "The web interface has not been built yet"
    frontend = frontend_dir()
    if frontend is None:
        return "no-source", "Installed build (no frontend sources to compare)"
    built = STAMP.read_text(encoding="utf-8").strip() if STAMP.is_file() else ""
    if built != source_hash(frontend):
        return "stale", "The built web interface is older than its source code (new features would be missing)"
    return "current", "The built web interface matches its source code"


def _npm() -> str | None:
    return shutil.which("npm.cmd") or shutil.which("npm")


def build(echo=print) -> bool:
    frontend = frontend_dir()
    npm = _npm()
    if frontend is None or npm is None:
        return False
    if not (frontend / "node_modules").is_dir():
        echo("Installing web interface dependencies (npm ci)…")
        if subprocess.run([npm, "ci"], cwd=frontend).returncode != 0:  # noqa: S603 - fixed arguments
            return False
    echo("Building the web interface (npm run build)…")
    if subprocess.run([npm, "run", "build"], cwd=frontend).returncode != 0:  # noqa: S603 - fixed arguments
        return False
    WEB.mkdir(parents=True, exist_ok=True)
    STAMP.write_text(source_hash(frontend) + "\n", encoding="utf-8")
    return True


def ensure_current(echo=print) -> str:
    """Rebuild a stale or missing build before serving. Returns the final status."""
    state, why = status()
    if state in ("current", "no-source"):
        return state
    echo(f"{why}.")
    if build(echo):
        echo("Web interface rebuilt.")
        return "current"
    echo("WARNING: could not rebuild the web interface automatically. Run:  npm ci --prefix frontend && "
         "npm run build --prefix frontend   (needs Node 22.12+), then restart. Until then the page is out of date.")
    return state


def health_row() -> dict:
    state, why = status()
    ok = state in ("current", "no-source")
    return {"name": "Web interface build", "status": "ok" if ok else "fail", "message": why,
            "fix": "" if ok else "Restart the web interface (it rebuilds itself when npm is installed), or run "
                                 "npm run build --prefix frontend", "optional": True}
