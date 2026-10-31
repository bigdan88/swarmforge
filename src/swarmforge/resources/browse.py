# SwarmForge browser probe: run inside the agent sandbox as `python -` (script on stdin).
# Arguments come from the SF_BROWSE_ARGS environment variable (JSON). Prints one line:
# __SF_BROWSE__{json}
import json
import os
import time

MARK = "__SF_BROWSE__"
args = json.loads(os.environ.get("SF_BROWSE_ARGS") or "{}")
result = {"url": args.get("url"), "console": [], "page_errors": [], "failed_requests": [],
          "action_errors": [], "app_errors": [], "controls": []}


def emit():
    print(MARK + json.dumps(result), flush=True)


try:
    from playwright.sync_api import sync_playwright
except Exception as e:  # noqa: BLE001
    result["fatal"] = f"No module named 'playwright' ({e})"
    emit()
    raise SystemExit(0)

ERROR_SELECTORS = ['[data-testid="stException"]', '[data-testid="stAlertContentError"]',
                   '[role="alert"]', '.traceback', '#traceback']
CONTROL_JS = """() => {
  const out = [];
  const els = document.querySelectorAll('button, a[href], input, select, textarea, [role=button], [role=tab], [role=checkbox], [role=link]');
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const role = el.getAttribute('role') || el.tagName.toLowerCase();
    const name = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder || el.name || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
    const id = el.id ? '#' + el.id : '';
    out.push(role + (id ? ' ' + id : '') + (name ? ' "' + name + '"' : ''));
    if (out.length >= 40) break;
  }
  return out;
}"""


def settle(page, timeout_ms):
    """Wait until the visible text stops changing (works for SPAs/Streamlit whose sockets never idle)."""
    end = time.time() + min(timeout_ms, 15000) / 1000
    last, stable = None, 0
    while time.time() < end:
        try:
            txt = page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:  # noqa: BLE001
            txt = None
        if txt is not None and txt == last and txt.strip():
            stable += 1
            if stable >= 3:
                return
        else:
            stable = 0
        last = txt
        page.wait_for_timeout(500)


def run_action(page, a, timeout_ms):
    t = min(timeout_ms, 15000)
    if "click" in a:
        page.locator(a["click"]).first.click(timeout=t)
    elif "fill" in a:
        page.locator(a["fill"]).first.fill(str(a.get("value", "")), timeout=t)
    elif "press" in a:
        if a.get("selector"):
            page.locator(a["selector"]).first.press(a["press"], timeout=t)
        else:
            page.keyboard.press(a["press"])
    elif "select" in a:
        page.locator(a["select"]).first.select_option(str(a.get("value", "")), timeout=t)
    elif "check" in a:
        page.locator(a["check"]).first.check(timeout=t)
    elif "wait_for" in a:
        page.locator(a["wait_for"]).first.wait_for(timeout=t)
    elif "wait_ms" in a:
        page.wait_for_timeout(min(int(a["wait_ms"]), 15000))
    elif "goto" in a:
        page.goto(a["goto"], timeout=t, wait_until="load")
    else:
        raise ValueError(f"unknown action {a!r}")
    settle(page, timeout_ms)


with sync_playwright() as p:
    browser = None
    try:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", lambda m: result["console"].append(f"{m.type}: {m.text[:300]}")
                if m.type in ("error", "warning") and len(result["console"]) < 30 else None)
        page.on("pageerror", lambda e: result["page_errors"].append(str(e)[:500])
                if len(result["page_errors"]) < 20 else None)
        page.on("response", lambda r: result["failed_requests"].append(f"{r.status} {r.url[:200]}")
                if r.status >= 400 and len(result["failed_requests"]) < 20 else None)
        page.on("requestfailed", lambda r: result["failed_requests"].append(f"FAILED {r.url[:200]} ({r.failure})")
                if len(result["failed_requests"]) < 20 else None)
        timeout_ms = int(args.get("timeout_ms") or 60000)
        resp = page.goto(args["url"], timeout=timeout_ms, wait_until="load")
        result["status"] = resp.status if resp else None
        settle(page, timeout_ms)
        for i, a in enumerate(args.get("actions") or []):
            try:
                run_action(page, a if isinstance(a, dict) else {}, timeout_ms)
            except Exception as e:  # noqa: BLE001
                result["action_errors"].append(f"action {i + 1} {json.dumps(a)[:120]}: {str(e).splitlines()[0][:300]}")
                break
        result["url"] = page.url
        result["title"] = page.title()
        result["text"] = page.evaluate("() => document.body ? document.body.innerText : ''")
        for sel in ERROR_SELECTORS:
            for el in page.locator(sel).all()[:5]:
                try:
                    t = el.inner_text(timeout=2000).strip()
                except Exception:  # noqa: BLE001
                    continue
                if t and t[:800] not in result["app_errors"]:
                    result["app_errors"].append(t[:800])
        result["controls"] = page.evaluate(CONTROL_JS)
        shot = args.get("screenshot")
        if shot:
            try:
                os.makedirs(os.path.dirname(shot), exist_ok=True)
                page.screenshot(path=shot, full_page=True)
                result["screenshot"] = shot
            except Exception as e:  # noqa: BLE001
                result["screenshot"] = None
                result["action_errors"].append(f"screenshot failed: {e}")
    except Exception as e:  # noqa: BLE001
        result["fatal"] = str(e).splitlines()[0][:500] if str(e) else type(e).__name__
    finally:
        if browser:
            browser.close()
emit()
