"""AC5 support: the gate command (`pytest -q -n 6 tests`) must not fail on real-browser tests.

Root cause of the earlier failures: with `-n 6`, several xdist workers launched Chromium at the same
moment. The sandbox gives /dev/shm only 64 MB and Chromium needs roughly that per instance, so a
launch lost the race and the browser died mid-test ("Target page, context or browser has been
closed", "Target crashed", or a `getComputedStyle` call landing on a page torn down underneath it).
Nothing about the tool-calling feature was involved - the same test passed every time it ran alone.

The fix lives in tests/conftest.py: tests tagged `@pytest.mark.browser` hold an exclusive
cross-process lock during the runtest protocol, so workers take turns. These tests verify the
mechanism itself, and that the fix was bought without weakening the assertions it protects.
"""

import ast
import subprocess
import sys

import pytest

from .conftest import ROOT, browser_lock

# anything that means "this test starts a real Chromium"
_BROWSER_RE = ("sync_playwright", "chromium.launch", 'importorskip("playwright')

# how the browser tests looked before the fix: the theme E2E must still assert a real switch
_THEME_ASSERTIONS = (
    "assert bg() == dark, \"--theme dark should be the first-visit theme\"",
    'has_text="Light"',
    "pg.wait_for_function(color_when(light), timeout=30000)",
    'has_text="Dark"',
    'pg.goto(f"http://localhost:{port}/?page=speed")',
    "assert bg() == dark",
    'assert pg.locator(\'[data-testid^="stMainMenuItem-theme-"]\').count() == 0',
)


def _child_script(log, tag, hold_s):
    return (
        "import sys, time\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "from tests.conftest import browser_lock\n"
        "with browser_lock(timeout_s=120):\n"
        f"    open({str(log)!r}, 'a').write('S{tag}\\n')\n"
        f"    time.sleep({hold_s})\n"
        f"    open({str(log)!r}, 'a').write('E{tag}\\n')\n"
        "    sys.stdout.flush()\n"
    )


def test_browser_lock_serialises_across_processes(tmp_path):
    """Three separate Python processes contend for the lock; no two may hold it at the same time."""
    log = tmp_path / "overlap.log"
    holds = [0.4, 0.2, 0.3]
    procs = [
        subprocess.Popen([sys.executable, "-c", _child_script(log, str(i), h)],
                         cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         text=True)
        for i, h in enumerate(holds)
    ]
    try:
        for p in procs:
            out, err = p.communicate(timeout=120)
            assert p.returncode == 0, f"child failed: {err}"
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()

    events = log.read_text(encoding="utf-8").split()
    assert sorted(e[0] for e in events if e[0] == "S") == ["S", "S", "S"]
    assert len(events) == 6
    # every start is immediately followed by *its own* end: sections never interleave
    for i in range(0, len(events), 2):
        assert events[i][0] == "S" and events[i + 1][0] == "E"
        assert events[i][1:] == events[i + 1][1:], f"overlap: {events}"


def test_browser_lock_is_reentrant_in_one_process(tmp_path):
    """A marked test may call a helper that takes the lock again (test_devtools probes for a
    browser through the same helper); a non-reentrant lock would deadlock or time out."""
    log = tmp_path / "nested.log"
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "from tests.conftest import browser_lock\n"
        "with browser_lock(timeout_s=10):\n"
        "    with browser_lock(timeout_s=10):\n"
        f"        open({str(log)!r}, 'w').write('nested ok')\n"
    )
    out = subprocess.run([sys.executable, "-c", script], cwd=str(ROOT), capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert log.read_text(encoding="utf-8") == "nested ok"


def test_browser_lock_never_blocks_forever_if_the_lock_is_unusable(tmp_path, monkeypatch):
    """A lock file that cannot be created must not break the suite: the body still runs."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr("tests.conftest.BROWSER_LOCK", blocker / "sub" / "browser.lock")
    ran = []
    with browser_lock(timeout_s=0.5):   # must not raise and must not hang
        ran.append(True)
    assert ran == [True]


def _markers_above(src_lines, func):
    """The decorator lines sitting directly above a test function's `def`."""
    start = min([d.lineno for d in func.decorator_list], default=func.lineno)
    return "\n".join(src_lines[start - 1:func.lineno - 1])


def test_every_real_browser_test_is_marked_for_serialisation():
    """Guard: any test that launches Chromium must carry `@pytest.mark.browser`, otherwise it races
    the other browser tests again under `-n 6` and the gate command goes red for no reason."""
    offenders = []
    for file in sorted((ROOT / "tests").glob("test_*.py")):
        src = file.read_text(encoding="utf-8")
        if not any(b in src for b in _BROWSER_RE):
            continue
        lines = src.splitlines()
        module_marks = "\n".join(lines[:20])
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                    and node.name.startswith("test_")):
                continue
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            if not any(b in body for b in _BROWSER_RE):
                continue
            decorators = _markers_above(lines, node) + module_marks
            if "pytest.mark.browser" not in decorators:
                offenders.append(f"{file.name}:{node.lineno}:{node.name}")
    assert offenders == [], "unmarked real-browser tests: " + ", ".join(offenders)


def test_theme_e2e_still_asserts_the_full_dark_light_dark_switch():
    """The flaky test was fixed by serialising browsers, not by gutting it: the dark -> light -> dark
    switch, the cross-page persistence check and the 'menu left closed' assertion are all still
    there, and the test is not xfail/skip-marked."""
    src = (ROOT / "tests" / "test_theme.py").read_text(encoding="utf-8")
    for fragment in _THEME_ASSERTIONS:
        assert fragment in src, f"theme E2E lost an assertion: {fragment}"
    lines = src.splitlines()
    i = next(n for n, l in enumerate(lines)
             if l.startswith("def test_real_browser_sidebar_switches_the_whole_page"))
    decorators = "\n".join(lines[max(0, i - 6):i])
    assert "pytest.mark.browser" in decorators and "pytest.mark.slow" in decorators
    assert "xfail" not in decorators and "skip" not in decorators, "flakiness must not be silenced"


def test_pytest_registers_the_browser_marker(request):
    """`-m browser` / `--strict-markers` must know the marker, so the suite stays runnable with
    `-m 'not browser'` on machines without Chromium."""
    declared = [m for m in request.config.getini("markers") if m.startswith("browser:")]
    assert declared, "the browser marker must be declared in pyproject.toml [tool.pytest.ini_options]"
    assert "serialis" in declared[0].lower() or "one at a time" in declared[0].lower()
