"""Accessibility analysis: Playwright a11y snapshot + WCAG AA contrast checks.

Pure, offline-computable analysis helpers (parse_color, contrast_ratio,
label_findings, contrast_findings) plus a host-side entry point ``audit()`` that
drives a real (or injected) browser page per theme and returns a JSON-safe
report whose findings feed ``state.repair_tasks_from_findings``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

from .visual import slugify

INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "combobox",
        "checkbox",
        "radio",
        "slider",
        "switch",
        "menuitem",
        "tab",
        "spinbutton",
    }
)

MAX_CONTRAST_PAIRS = 200
MAX_CONTRAST_FINDINGS = 10
MAX_FINDING_TEXT = 1500


class A11yError(RuntimeError):
    """The accessibility audit could not run (bad URL, no browser, ...)."""


_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3,8})$")
_RGB_RE = re.compile(
    r"^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)$"
)


def parse_color(value: str) -> tuple[int, int, int] | None:
    """Parse a CSS color into an (r, g, b) tuple; None when unparseable.

    Accepts ``#rgb``, ``#rrggbb``, ``#rrggbbaa`` (alpha composited over white)
    and ``rgb()``/``rgba()`` (alpha composited over white). ``transparent``
    and anything unparseable return None.
    """
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if not s or s == "transparent":
        return None
    m = _HEX_RE.match(s)
    if m:
        h = m.group(1)
        if len(h) == 3:
            r, g, b = (int(c * 2, 16) for c in h)
            return (r, g, b)
        if len(h) == 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        if len(h) == 8:
            r, g, b = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            a = int(h[6:8], 16) / 255.0
            return _composite(r, g, b, a)
        return None
    m = _RGB_RE.match(s)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        a = float(m.group(4)) if m.group(4) is not None else 1.0
        return _composite(r, g, b, a)
    return None


def _composite(r: int, g: int, b: int, a: float) -> tuple[int, int, int]:
    """Composite an alpha color over a white background."""
    a = min(max(a, 0.0), 1.0)
    return (
        round(r * a + 255 * (1 - a)),
        round(g * a + 255 * (1 - a)),
        round(b * a + 255 * (1 - a)),
    )


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def lin(c: int) -> float:
        v = c / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast_ratio(fg: Any, bg: Any) -> float:
    """WCAG 2.x contrast ratio between two colors; 1.0 when unparseable."""
    f = fg if isinstance(fg, tuple) else parse_color(str(fg))
    b = bg if isinstance(bg, tuple) else parse_color(str(bg))
    if not f or not b:
        return 1.0
    lf, lb = _relative_luminance(f), _relative_luminance(b)
    if lf < lb:
        lf, lb = lb, lf
    return (lf + 0.05) / (lb + 0.05)


_ARIA_LINE_RE = re.compile(r"^\s*-\s+(\S+)\s*(.*)$")
_ARIA_NAME_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def parse_aria_snapshot(text: str) -> list[dict]:
    """Parse Playwright's aria_snapshot YAML into flat node dicts.

    Handles lines like ``- heading "Chat" [level=1]``,
    ``- button "Named": "n"``, ``- button`` and ``- textbox "Prompt"``.
    ``[attributes]`` and ``: value`` suffixes are ignored; lines that do
    not start with ``- role`` are skipped. Each node is
    ``{'role': <role>, 'name': <quoted string or ''>}``.
    """
    if not isinstance(text, str):
        return []
    nodes: list[dict] = []
    for line in text.splitlines():
        m = _ARIA_LINE_RE.match(line)
        if not m:
            continue
        role = m.group(1)
        rest = m.group(2)
        # drop [attribute] blocks and ": value" suffixes
        rest = re.sub(r"\[[^\]]*\]", "", rest)
        colon = rest.find(":")
        if colon != -1:
            rest = rest[:colon]
        nm = _ARIA_NAME_RE.search(rest)
        name = nm.group(1) if nm else ""
        name = name.replace('\\"', '"').replace("\\\\", "\\")
        nodes.append({"role": role, "name": name})
    return nodes


def flatten_a11y_tree(node: Any) -> list[dict]:
    """Depth-first flattening of a Playwright accessibility snapshot.

    Accepts a dict/list snapshot (legacy ``accessibility.snapshot()``) or the
    YAML string returned by ``locator(...).aria_snapshot()``.
    """
    if isinstance(node, str):
        return parse_aria_snapshot(node)
    out: list[dict] = []
    stack: list[Any] = [node] if not isinstance(node, list) else list(reversed(node))
    while stack:
        cur = stack.pop()
        if not isinstance(cur, dict):
            continue
        out.append(cur)
        children = cur.get("children")
        if isinstance(children, list):
            stack.extend(reversed(children))
    return out


def _dashboard_page_file(tab: str) -> str:
    return f"src/swarmforge/dashboard_pages/{slugify(tab)}.py"


def label_findings(tab: str, theme: str, snapshot: Any) -> list[dict]:
    """Flag interactive a11y nodes whose accessible name is empty ([major])."""
    findings: list[dict] = []
    offenders: list[str] = []
    for node in flatten_a11y_tree(snapshot):
        role = str(node.get("role") or "")
        if role not in INTERACTIVE_ROLES:
            continue
        name = str(node.get("name") or "").strip()
        if name:
            continue
        offenders.append(role)
        findings.append(
            {
                "severity": "major",
                "page": slugify(tab),
                "theme": theme,
                "blocking": True,
                "a11y": True,
                "file": _dashboard_page_file(tab),
                "line": None,
                "title": f"{role} without an accessible name",
                "description": f"{role} on the {tab} page has no accessible name",
                "recommendation": (
                    f"give the {tab} page's unnamed {role}s a label "
                    "(st.button label / aria_label / help text)"
                ),
            }
        )
    if not findings:
        return []
    summary = ", ".join(offenders[:8])
    if len(offenders) > 8:
        summary += f" (+{len(offenders) - 8} more)"
    if len(summary) > MAX_FINDING_TEXT:
        summary = summary[: MAX_FINDING_TEXT - 3] + "..."
    for f in findings:
        f["description"] = summary
    return findings


CONTRAST_JS = """
() => {
    const selector = (el) => {
        let s = (el.tagName || "*").toLowerCase();
        if (el.id) s += "#" + el.id;
        if (el.classList && el.classList.length) s += "." + el.classList[0];
        return s.length > 60 ? s.slice(0, 57) + "..." : s;
    };
    const backgroundOf = (el) => {
        let cur = el;
        while (cur && cur !== document.documentElement) {
            let style = null;
            try { style = getComputedStyle(cur); } catch (e) { style = null; }
            if (style) {
                const bg = style.backgroundColor;
                if (bg && bg !== "transparent" && !/rgba\\(.*,\\s*0\\)/.test(bg)) return bg;
            }
            cur = cur.parentElement;
        }
        return "#ffffff";
    };
    const pairs = [];
    let els = [];
    try { els = document.querySelectorAll("body *"); } catch (e) { els = []; }
    for (const el of els) {
        if (pairs.length >= 200) break;
        try {
            const text = (el.textContent || "").trim();
            if (!text) continue;
            const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
            if (!rect || !(rect.width > 0) || !(rect.height > 0)) continue;
            const style = getComputedStyle(el);
            if (!style || style.display === "none" ||
                style.visibility === "hidden") continue;
            pairs.push({
                selector: selector(el),
                text: text.length > 60 ? text.slice(0, 57) + "..." : text,
                color: style.color,
                background: backgroundOf(el),
            });
        } catch (e) { /* skip unreadable elements */ }
    }
    return pairs;
}
"""


def contrast_findings(
    tab: str, theme: str, pairs: Any, *, min_ratio: float = 4.5
) -> list[dict]:
    """Flag text/background pairs below the WCAG AA ratio ([minor])."""
    if not isinstance(pairs, list):
        return []
    seen: set[tuple[str, str, str]] = set()
    scored: list[tuple[float, dict]] = []
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        selector = str(pair.get("selector") or "*")
        color = str(pair.get("color") or "")
        background = str(pair.get("background") or "")
        key = (selector, color, background)
        if key in seen:
            continue
        seen.add(key)
        fg, bg = parse_color(color), parse_color(background)
        if not fg or not bg:
            continue
        ratio = contrast_ratio(fg, bg)
        if ratio >= min_ratio:
            continue
        text = str(pair.get("text") or "")
        scored.append(
            (
                ratio,
                {
                    "severity": "minor",
                    "page": slugify(tab),
                    "theme": theme,
                    "blocking": False,
                    "a11y": True,
                    "contrast": True,
                    "file": _dashboard_page_file(tab),
                    "line": None,
                    "title": f"contrast {ratio:.2f}:1 below WCAG AA",
                    "description": (
                        f"{selector} text {text!r} uses {color} on {background} "
                        f"({ratio:.2f}:1 contrast)"
                    ),
                    "recommendation": (
                        f"raise the {theme}-theme contrast of {selector} "
                        f"to at least 4.5:1"
                    ),
                },
            )
        )
    scored.sort(key=lambda item: item[0])
    return [f for _, f in scored[:MAX_CONTRAST_FINDINGS]]


def _default_open_page(viewport: dict | None = None) -> Callable[[str], Any]:
    """Real-browser page factory: one shared browser, one page per theme."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - depends on the sandbox
        raise A11yError(
            "playwright is not installed; run `playwright install chromium`"
        ) from e

    state: dict[str, Any] = {"api": None, "browser": None}

    def open_page(theme: str):
        if state["browser"] is None:
            pw = sync_playwright().start()
            state["api"] = pw
            state["browser"] = pw.chromium.launch()
        browser = state["browser"]
        page = browser.new_page(
            color_scheme=theme,  # type: ignore[arg-type]
            viewport=viewport,  # type: ignore[arg-type]
        )

        class _Page:
            def goto(self, url, **kw):
                page.goto(url, **kw)

            def wait_for_timeout(self, ms):
                page.wait_for_timeout(ms)

            def evaluate(self, js, arg=None):
                return page.evaluate(js, arg)

            def aria_snapshot(self):
                return page.locator("body").aria_snapshot()

            @property
            def accessibility(self):
                return getattr(page, "accessibility", None)

            def close(self):
                try:
                    page.close()
                except Exception:  # noqa: BLE001, S110 - best effort cleanup
                    pass

        return _Page()

    def _shutdown():
        browser = state["browser"]
        state["browser"] = None
        api = state["api"]
        state["api"] = None
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001, S110 - best effort cleanup
                pass
        if api is not None:
            try:
                api.stop()
            except Exception:  # noqa: BLE001, S110 - best effort cleanup
                pass

    open_page.shutdown = _shutdown  # type: ignore[attr-defined]

    return open_page


def _default_tab(tab_url: str) -> str:
    parsed = urlparse(tab_url)
    page = (parse_qs(parsed.query).get("page") or [""])[0].strip()
    if page:
        return slugify(page)
    last = [seg for seg in parsed.path.split("/") if seg]
    return slugify(last[-1]) if last else "page"


def audit(
    tab_url: str,
    *,
    tab: str | None = None,
    themes: tuple[str, ...] | list[str] = ("light", "dark"),
    open_page: Callable[[str], Any] | None = None,
    viewport: dict | None = None,
    min_ratio: float = 4.5,
    wait_ms: int = 1500,
) -> dict:
    """Audit a served page's accessibility across themes.

    Returns a JSON-safe report ``{ok, url, tab, themes, results, findings,
    error}``. Per-theme errors are recorded, never raised; ``ok`` is False
    only when every theme errored.
    """
    if not isinstance(tab_url, str) or not tab_url.startswith(("http://", "https://")):
        raise A11yError(f"tab_url must be an http(s) URL, got: {tab_url!r}")
    themes = tuple(themes)
    if not themes or not set(themes) <= {"light", "dark"}:
        raise A11yError(f"themes must be a subset of ('light', 'dark'), got: {themes}")
    if tab is None:
        tab = _default_tab(tab_url)

    factory = open_page
    if factory is None:
        factory = _default_open_page(viewport)

    findings: list[dict] = []
    results: list[dict] = []
    errors: list[str] = []
    try:
        for theme in themes:
            page = None
            error: str | None = None
            snapshot: Any = None
            pairs: list = []
            try:
                page = factory(theme)
                page.goto(tab_url, wait_until="load")
                page.wait_for_timeout(wait_ms)
                snapshot_error: str | None = None
                contrast_error: str | None = None
                try:
                    acc = getattr(page, "accessibility", None)
                    if acc is not None and hasattr(acc, "snapshot"):
                        snapshot = acc.snapshot()
                    else:
                        snapshot = page.aria_snapshot()
                except Exception as e:  # noqa: BLE001 - any browser failure is recorded
                    snapshot = None
                    snapshot_error = f"accessibility snapshot failed: {e}"
                if snapshot is None and snapshot_error is None:
                    snapshot_error = "accessibility snapshot returned no data"
                try:
                    pairs = page.evaluate(CONTRAST_JS)
                except Exception as e:  # noqa: BLE001 - any browser failure is recorded
                    pairs = []
                    contrast_error = f"contrast probe failed: {e}"
                if not isinstance(pairs, list):
                    pairs = []
                # label findings need the snapshot; contrast findings run regardless
                if snapshot_error is None:
                    findings.extend(label_findings(tab, theme, snapshot))
                findings.extend(
                    contrast_findings(tab, theme, pairs, min_ratio=min_ratio)
                )
                error = (
                    "; ".join(
                        e for e in (snapshot_error, contrast_error) if e is not None
                    )
                    or None
                )
            except Exception as e:  # noqa: BLE001 - any browser failure is recorded
                error = str(e)
            finally:
                if page is not None:
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001, S110 - best effort cleanup
                        pass
            if error is not None:
                errors.append(f"{theme}: {error}")
            results.append(
                {
                    "theme": theme,
                    "error": error,
                    "interactive": len(
                        [
                            n
                            for n in flatten_a11y_tree(snapshot)
                            if n.get("role") in INTERACTIVE_ROLES
                        ]
                    )
                    if snapshot is not None
                    else 0,
                    "text_pairs": len(pairs),
                }
            )
    finally:
        shut = getattr(factory, "shutdown", None)
        if callable(shut):
            try:
                shut()
            except Exception:  # noqa: BLE001, S110 - best effort cleanup
                pass

    return {
        "ok": len(errors) < len(results),
        "url": tab_url,
        "tab": tab,
        "themes": list(themes),
        "results": results,
        "findings": findings,
        "error": "; ".join(errors) if errors else None,
    }
