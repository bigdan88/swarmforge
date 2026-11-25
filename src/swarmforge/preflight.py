"""Fast environment checks run before a run starts (dashboard) and at the start of a run.

Each problem is reported as a plain-language message with the fix, instead of failing deep inside
a run with a stack trace. The low-level probes return structured (message, fix) pairs; both the
prose API (environment_problems) and the structured API (environment_check_results) are composed
from them so the two can never drift apart."""

from __future__ import annotations

import shutil
import subprocess

from .config import Config

SANDBOX_REQUIRED_TOOLS: tuple[str, ...] = ("ruff", "git", "curl")
SANDBOX_TOOL_INSTALL_HINTS: dict[str, str] = {
    "ruff": "pip install ruff",
    "git": "apt-get install -y git",
    "curl": "apt-get install -y curl",
}


def _missing_key_problems(cfg: Config) -> list[tuple[str, str]]:
    """One (message, fix) per provider in use whose API key is not set."""
    used = {
        cfg.roles[r].provider
        for r in [
            "planner",
            "reviewer",
            "tester",
            cfg.librarian_role,
            *cfg.developer_roles,
        ]
        if r in cfg.roles
    }
    out: list[tuple[str, str]] = []
    for name in sorted(used):
        p = cfg.providers[name]
        if not p.api_key():
            message = f"The API key for provider '{name}' is missing"
            fix = (
                f"add {p.api_key_env}=... to the .env file next to "
                f"{cfg.source_path or 'swarmforge.yaml'} and restart the dashboard."
            )
            out.append((message, fix))
    return out


def _git_problem() -> tuple[str, str] | None:
    if not shutil.which("git"):
        return (
            "git is not installed or not on PATH.",
            "Install Git for Windows and reopen the terminal.",
        )
    return None


_DESKTOP_TRIED_AT = 0.0
DESKTOP_START_WAIT_S = 120.0


def _docker_answers(d: str, timeout_s: int) -> tuple[bool, list[str]]:
    try:
        info = subprocess.run([d, "info", "--format", "{{.ServerVersion}}"], check=False, capture_output=True,
                              text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False, ["`docker info` did not answer"]
    if info.returncode == 0:
        return True, []
    return False, (info.stderr or info.stdout).strip().splitlines()[-1:]


def docker_desktop_path() -> str | None:
    """Docker Desktop's launcher on Windows/macOS, if installed (Linux runs dockerd as a service)."""
    import os
    import sys
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles"), r"C:\Program Files"):
            if base and os.path.isfile(os.path.join(base, "Docker", "Docker", "Docker Desktop.exe")):
                return os.path.join(base, "Docker", "Docker", "Docker Desktop.exe")
    if sys.platform == "darwin" and os.path.isdir("/Applications/Docker.app"):
        return "/Applications/Docker.app"
    return None


def start_docker_desktop(d: str, wait_s: float = DESKTOP_START_WAIT_S, *, sleep=None) -> bool:
    """Start Docker Desktop when it is installed but not running, and wait for its engine.

    The usual cause of "Docker is not responding" is simply that Docker Desktop was not started after a
    reboot. Tried at most once every ten minutes per process; SWARMFORGE_NO_DOCKER_AUTOSTART=1 turns it off."""
    import os
    import sys
    import time
    global _DESKTOP_TRIED_AT
    path = docker_desktop_path()
    if not path or os.environ.get("SWARMFORGE_NO_DOCKER_AUTOSTART") == "1" or time.time() - _DESKTOP_TRIED_AT < 600:
        return False
    _DESKTOP_TRIED_AT = time.time()
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-g", path])  # noqa: S603,S607 - fixed arguments
        else:
            subprocess.Popen([path], close_fds=True)  # noqa: S603 - fixed path under Program Files
    except OSError:
        return False
    sleep = sleep or time.sleep
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        sleep(5)
        if _docker_answers(d, 15)[0]:
            return True
    return False


def _docker_problem(cfg: Config, docker_timeout_s: int) -> tuple[str, str] | None:
    """(message, fix) when the docker binary is missing or the daemon does not answer."""
    d = cfg.sandbox.docker_binary
    if not shutil.which(d):
        return (
            "Docker is not installed (or not on PATH).",
            (
                "Install Docker Desktop, or set sandbox.mode: local in swarmforge.yaml "
                "(unsandboxed)."
            ),
        )
    ok, err = _docker_answers(d, docker_timeout_s)
    if not ok and start_docker_desktop(d):  # not started since the last reboot: start it and wait
        ok, err = True, []
    if not ok:
        return (
            "Docker is not responding ("
            + (err[0][:160] if err else "no answer")
            + ").",
            (
                "Start Docker Desktop and wait for 'Engine running'; if it is already running, "
                "quit it, run `wsl --shutdown`, and start it again."
            ),
        )
    return None


DEV_IMAGE = "swarmforge-dev-sandbox:latest"
_REBUILD_LOCK = __import__("threading").RLock()
_REBUILD_TRIED: dict[str, float] = {}  # recipe -> last attempt (monotonic)


def _recipe_of(cfg: Config, img: str):
    """(dockerfile, build_args) for SwarmForge's own images; None for anything else."""
    if img == cfg.sandbox.image:
        return "Dockerfile.sandbox", {}
    if img == DEV_IMAGE:
        return "Dockerfile.dev-sandbox", {"BASE": cfg.sandbox.image}
    return None


def _image_label(cfg: Config, img: str) -> str | None:
    """The image's recipe label, "" when it has none, None when the image is missing."""
    from .sandbox import RECIPE_LABEL
    r = subprocess.run([cfg.sandbox.docker_binary, "image", "inspect", "--format",
                        "{{ index .Config.Labels \"" + RECIPE_LABEL + "\" }}", img],
                       check=False, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    value = (r.stdout or "").strip()
    return "" if value in ("", "<no value>") else value


def refresh_image(cfg: Config, img: str) -> bool:
    """Rebuild one of SwarmForge's own sandbox images when it is missing or was built from an
    older recipe (for example the Dockerfile gained a package the tests now import). Runs at most
    once per recipe per hour per process; SWARMFORGE_NO_IMAGE_REBUILD=1 turns it off.
    Returns True when the image is current afterwards."""
    import os
    import time
    from .sandbox import DockerSandbox, recipe_hash
    recipe = _recipe_of(cfg, img)
    if not recipe:
        return _image_label(cfg, img) is not None
    dockerfile, extra = recipe
    with _REBUILD_LOCK:
        if img == DEV_IMAGE and not refresh_image(cfg, cfg.sandbox.image):
            return False
        sb = DockerSandbox(cfg.sandbox)
        want = recipe_hash(dockerfile, {"BASE_IMAGE": cfg.sandbox.base_image, **extra})
        if _image_label(cfg, img) == want:
            return True
        if os.environ.get("SWARMFORGE_NO_IMAGE_REBUILD") == "1" or time.monotonic() - _REBUILD_TRIED.get(want, -1e9) < 3600:
            return False
        _REBUILD_TRIED[want] = time.monotonic()
        try:
            ok = sb.build_image(dockerfile=dockerfile, tag=img, build_args=extra or None, quiet=True).returncode == 0
        except OSError:
            ok = False
        if ok:
            _TOOL_CHECKS.clear()
        return ok


def _image_problems(cfg: Config, image: str | None) -> list[tuple[str, str]]:
    """(message, fix) when the sandbox image is not built yet (docker mode only).

    SwarmForge's own images rebuild themselves when missing or out of date; an outdated
    image that cannot be rebuilt is still used (it worked before), a missing one blocks."""
    img = image or cfg.sandbox.image
    current = refresh_image(cfg, img) if _recipe_of(cfg, img) else False
    if not current and (
        subprocess.run(
            [cfg.sandbox.docker_binary, "image", "inspect", img],
            check=False,
            capture_output=True,
        ).returncode
        != 0
    ):
        fix = (
            "swarmforge sandbox build --dev"
            if "dev-sandbox" in img
            else "swarmforge sandbox build"
        )
        return [(f"The sandbox image '{img}' is not built yet.", fix)]
    return []


def _sandbox_tool_problems(
    cfg: Config, image: str, docker_timeout_s: int
) -> list[tuple[str, str]]:
    """(message, fix) for each required tool that is missing inside the sandbox image.

    Runs `docker run --rm <image> which <tool>` (the in-image equivalent of shutil.which). Never
    raises: a probe that cannot run at all is reported as a problem and stops further probing."""
    problems: list[tuple[str, str]] = []
    for tool in SANDBOX_REQUIRED_TOOLS:
        hint = SANDBOX_TOOL_INSTALL_HINTS[tool]
        try:
            r = subprocess.run(
                [cfg.sandbox.docker_binary, "run", "--rm", image, "which", tool],
                check=False,
                capture_output=True,
                text=True,
                timeout=docker_timeout_s,
            )
            if r.returncode != 0:
                problems.append(
                    (
                        f"The sandbox image '{image}' is missing '{tool}'.",
                        (
                            f"install the tool in the image Dockerfile (`{hint}`) and rebuild: "
                            f"swarmforge sandbox build"
                        ),
                    )
                )
        except (OSError, subprocess.SubprocessError) as exc:
            problems.append(
                (
                    f"The sandbox self-check could not run in image '{image}': {exc}",
                    "Start Docker and build the image: swarmforge sandbox build",
                )
            )
            break
    return problems


def environment_problems(
    cfg: Config, image: str | None = None, *, docker_timeout_s: int = 20
) -> list[str]:
    problems: list[str] = []
    for message, fix in _missing_key_problems(cfg):
        problems.append(f"{message}: {fix}")
    git = _git_problem()
    if git:
        problems.append(f"{git[0]} {git[1]}")
    if cfg.sandbox.mode == "docker":
        d = _docker_problem(cfg, docker_timeout_s)
        if d:
            problems.append(f"{d[0]} {d[1]}")
            return problems
        for message, fix in _image_problems(cfg, image):
            problems.append(f"{message} Run `{fix}` once.")
    return problems


_TOOL_CHECKS: dict[str, list[str]] = {}   # image id -> warnings (an image's contents never change)


def sandbox_tool_warnings(
    cfg: Config, image: str | None = None, *, docker_timeout_s: int = 20
) -> list[str]:
    """Missing convenience tools (ruff, git, curl) in the sandbox image, as WARNINGS.

    Runs never block on these: agents work without them (only the matching tool degrades), and a
    newer Dockerfile than the built image must not stop every queued task. Checked once per image id."""
    if cfg.sandbox.mode != "docker":
        return []
    img = image or cfg.sandbox.image
    try:
        r = subprocess.run([cfg.sandbox.docker_binary, "image", "inspect", "--format", "{{.Id}}", img],
                           check=False, capture_output=True, text=True, timeout=docker_timeout_s)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    key = r.stdout.strip() or img
    if key not in _TOOL_CHECKS:
        _TOOL_CHECKS[key] = [f"{m} {fix}" for m, fix in _sandbox_tool_problems(cfg, img, docker_timeout_s)]
    return list(_TOOL_CHECKS[key])


def environment_check_results(
    cfg: Config, image: str | None = None, *, docker_timeout_s: int = 20
) -> list[dict]:
    """Structured rows ({'name','status','message','fix'}, status 'ok'|'fail'|'skipped')
    for the same probes. 'skipped' means the check could not be performed.

    In local mode no docker/image rows are emitted, so callers can show the
    'not checked (sandbox.mode: local)' note themselves."""
    rows: list[dict] = []
    keys = _missing_key_problems(cfg)
    rows.append(
        {
            "name": "Provider API keys",
            "status": "fail" if keys else "ok",
            "message": "; ".join(m for m, _ in keys),
            "fix": "; ".join(f for _, f in keys),
        }
    )
    git = _git_problem()
    rows.append(
        {
            "name": "Git",
            "status": "fail" if git else "ok",
            "message": git[0] if git else "",
            "fix": git[1] if git else "",
        }
    )
    if cfg.sandbox.mode == "docker":
        d = _docker_problem(cfg, docker_timeout_s)
        rows.append(
            {
                "name": "Docker running",
                "status": "fail" if d else "ok",
                "message": d[0] if d else "",
                "fix": d[1] if d else "",
            }
        )
        if d:
            rows.append(
                {
                    "name": "Sandbox image built",
                    "status": "skipped",
                    "message": "not checked (Docker is not responding)",
                    "fix": d[1],
                }
            )
            rows.append(
                {
                    "name": "Sandbox tools",
                    "status": "skipped",
                    "message": "not checked (Docker is not responding)",
                    "fix": "",
                }
            )
        else:
            imgs = _image_problems(cfg, image)
            rows.append(
                {
                    "name": "Sandbox image built",
                    "status": "fail" if imgs else "ok",
                    "message": "; ".join(m for m, _ in imgs),
                    "fix": "; ".join(f for _, f in imgs),
                }
            )
            if imgs:
                rows.append(
                    {
                        "name": "Sandbox tools",
                        "status": "skipped",
                        "message": "not checked (sandbox image is not built)",
                        "fix": "",
                    }
                )
            else:
                img = image or cfg.sandbox.image
                tools = _sandbox_tool_problems(cfg, img, docker_timeout_s)
                rows.append(
                    {
                        "name": "Sandbox tools",
                        "status": "fail" if tools else "ok",
                        "message": "; ".join(m for m, _ in tools)
                        or f"{', '.join(SANDBOX_REQUIRED_TOOLS)} available in {img}",
                        "fix": "; ".join(f for _, f in tools),
                    }
                )
    return rows
