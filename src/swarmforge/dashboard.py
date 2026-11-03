"""SwarmForge control center (Streamlit). Launch with `swarmforge dashboard`.

Start runs, approve pushes, watch delegation and every agent's conversation live, browse and edit
the Zettelkasten knowledge base, and compare benchmarks. Runs execute as separate background
processes; the dashboard only reads the SQLite store / vault and writes explicit user actions
(new run, cancel, approval decision, note edits).
"""
from __future__ import annotations

from swarmforge.services.runs import request_cancel as request_run_cancel

import dataclasses
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from swarmforge import branding
from swarmforge.config import ConfigError, load_config
from swarmforge.db import Store
from swarmforge.knowledge import NOTE_TYPES, Vault, WIKILINK_RE

# --------------------------------------------------------------------- theme
# Light/dark is Streamlit's own theme switch (⋮ menu → System / Light / Dark): it restyles every
# element, tables and charts included, and each browser remembers its choice. SwarmForge adds no
# colour palette of its own (custom CSS here only uses see-through colours that work in both).
# The sidebar buttons drive that same native switch; `swarmforge dashboard --theme dark|light` only
# sets the starting theme for a browser that has not picked one yet.
THEME_CHOICES = ("System", "Light", "Dark")
LAUNCH_THEME = os.environ.get("SWARMFORGE_DASH_THEME", "auto")  # set by `swarmforge dashboard --theme`


def theme_switch_js(choice: str, only_if_unset: bool = False) -> str:
    """Script (run in a zero-height component) that picks `choice` in Streamlit's own ⋮ menu theme
    switch: opens the menu (it reacts to pointer events, not a plain click), clicks the item, closes it."""
    if choice not in THEME_CHOICES:
        raise ValueError(choice)
    guard = ("if (Object.keys(w.localStorage).some(k => k.startsWith('stActiveTheme'))) return;"
             if only_if_unset else "")
    return f"""<script>
(function () {{
  const w = window.parent, d = w.document;
  try {{ {guard} }} catch (e) {{}}
  const sel = '[data-testid="stMainMenuItem-theme-{choice}"]';
  const press = (el) => {{
    for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {{
      const E = t.startsWith('pointer') ? w.PointerEvent : w.MouseEvent;
      el.dispatchEvent(new E(t, {{bubbles: true, cancelable: true, button: 0}}));
    }}
  }};
  const menuButton = () => d.querySelector('[data-testid="stMainMenu"] button') || d.querySelector('[data-testid="stMainMenu"]');
  let tries = 0, opened = false;
  (function go() {{
    const item = d.querySelector(sel);
    if (item) {{
      item.click();
      setTimeout(() => {{
        if (opened && d.querySelector(sel) && menuButton()) press(menuButton());
        if (d.activeElement && d.activeElement.blur) d.activeElement.blur();
      }}, 150);
      return;
    }}
    const m = menuButton();
    if (m && !opened) {{ press(m); opened = true; }}
    if (++tries < 30) setTimeout(go, 100);
  }})();
}})();
</script>"""


CHAT_CSS = """<style>
/* Claude-style chat page: geometry, typography and see-through tints only, so it works in Streamlit's
   light and dark theme alike (the theme itself is Streamlit's own switch, no custom palette);
   the rgba() neutrals below read correctly on either base. Colour defaults sit inside :where() (zero
   specificity) so an explicit theme block always wins over them. */

/* ---- transcript */
[data-testid="stChatMessage"] { align-items: flex-start; background: transparent; padding: 0.35rem 0; gap: 0.75rem; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]), [data-testid="stChatMessage"]:has([data-testid="stChatMessageContent"][aria-label="Chat message from user"]) { flex-direction: row-reverse; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageContent"][aria-label="Chat message from user"]) [data-testid="stChatMessageAvatarCustom"] { display: none; }
[data-testid="stChatMessageContent"] { border-radius: 18px; padding: 0.15rem 0.1rem; max-width: 100%; line-height: 1.7; font-size: 1rem; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"], [data-testid="stChatMessage"]:has([data-testid="stChatMessageContent"][aria-label="Chat message from user"]) [data-testid="stChatMessageContent"] { padding: 0.65rem 1.1rem; max-width: 78%; flex: 0 1 auto; border-bottom-right-radius: 6px; }
:where([data-testid="stChatMessageContent"][aria-label="Chat message from user"]) { background-color: rgba(128, 128, 128, 0.12); }
[data-testid="stChatMessageAvatarCustom"] { opacity: 0.75; margin-top: 0.2rem; background: transparent; border: 0; }
[data-testid="stChatMessageContent"] h1, [data-testid="stChatMessageContent"] h2, [data-testid="stChatMessageContent"] h3, [data-testid="stChatMessageContent"] h4 { font-family: Georgia, "Times New Roman", serif; font-weight: 600; letter-spacing: 0.01em; line-height: 1.35; padding: 0.9rem 0 0.35rem 0; }
[data-testid="stChatMessageContent"] h1 { font-size: 1.45rem; }
[data-testid="stChatMessageContent"] h2 { font-size: 1.3rem; }
[data-testid="stChatMessageContent"] h3 { font-size: 1.12rem; }
[data-testid="stChatMessageContent"] h4 { font-size: 1rem; }
[data-testid="stChatMessageContent"] table { font-size: 0.92rem; border-collapse: collapse; }
[data-testid="stChatMessageContent"] th, [data-testid="stChatMessageContent"] td { padding: 0.35rem 0.75rem; }
[data-testid="stChatMessageContent"] code { font-size: 0.85em; border-radius: 4px; padding: 0.05em 0.3em; }

/* ---- page frame */
.st-key-chat_page h2 { font-size: 1.45rem; font-weight: 600; padding-bottom: 0.1rem; }
.st-key-chat_main { max-width: 860px; margin: 0 auto; }
.st-key-chat_topbar { border-bottom: 1px solid rgba(128, 128, 128, 0.2); padding-bottom: 0.4rem; margin-bottom: 0.4rem; }
.st-key-chat_topbar [data-testid="stMarkdown"] p { font-family: Georgia, "Times New Roman", serif; font-size: 1.1rem; font-weight: 600; margin: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.st-key-chat_empty { padding: 3.5rem 0 1.5rem 0; }
.st-key-chat_empty [data-testid="stMarkdownContainer"], .st-key-chat_empty p, .st-key-chat_empty h3 { text-align: center; justify-content: center; }
.st-key-chat_empty h3 { font-family: Georgia, "Times New Roman", serif; font-weight: 500; font-size: 1.9rem; }
.st-key-chat_empty p { opacity: 0.7; }

/* ---- composer */
[data-testid="stChatInput"] { border-radius: 16px; border: 1px solid rgba(128, 128, 128, 0.3); }
[data-testid="stChatInputTextArea"]::placeholder { color: inherit; opacity: 0.55; }

/* ---- suggestion chips */
.st-key-chat_chips button { border-radius: 999px; padding: 0.3rem 0.9rem; min-height: 2.1rem; font-size: 0.88rem; border: 1px solid rgba(128, 128, 128, 0.3); background: transparent; }
.st-key-chat_chips button:hover { background: rgba(128, 128, 128, 0.1); }
.st-key-chat_chips button p { font-size: 0.88rem; }

/* ---- transcript scrolls by itself (fills the window), so the page, the rail and the composer stay put */
[data-testid="stMainBlockContainer"]:has(.st-key-chat_scroll) { padding-top: 2.75rem; padding-bottom: 0.75rem; }
[data-testid="stMainBlockContainer"]:has(.st-key-chat_scroll) [data-testid="stElementContainer"]:has(> iframe.stIFrame) { position: absolute; height: 0; overflow: hidden; margin: 0; }
/* compact composer: a small "Upload" button instead of a big drop zone */
.st-key-chat_files [data-testid="stFileUploaderDropzone"] { padding: 0; background: transparent; border: 0; }
.st-key-chat_files [data-testid="stFileUploaderDropzoneInstructions"] { display: none; }
.st-key-chat_chips { gap: 0.4rem; }
.st-key-chat_chips button { min-height: 1.9rem !important; padding: 0.15rem 0.75rem !important; }
div[data-testid="stLayoutWrapper"]:has(> .st-key-chat_scroll) { flex: 0 0 auto !important; height: auto !important; }
.st-key-chat_scroll { flex: 0 0 auto !important; height: var(--sf-chat-h, calc(100vh - 25rem)) !important; min-height: 10rem; overflow-y: auto;
                      border: 0 !important; overscroll-behavior: contain; }
.st-key-chat_rail { position: sticky; top: 3.75rem; max-height: calc(100vh - 9rem); overflow-y: auto; flex-wrap: nowrap; }

/* ---- conversation rail */
.st-key-chat_rail { background: rgba(128, 128, 128, 0.06); border-radius: 14px; padding: 0.75rem 0.55rem; gap: 0.15rem; }
.st-key-chat_rail .st-key-chat_new button { justify-content: flex-start; border-radius: 10px; font-weight: 500; }
.st-key-chat_rail .st-key-chat_new button > div { justify-content: flex-start; }
.st-key-chat_rail .sf-rail-label { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; opacity: 0.55; padding: 0.85rem 0 0 0.6rem; height: 2.1rem; line-height: 1; }
.st-key-chat_rail [class*="st-key-chat_row_"] { border-radius: 9px; padding: 0 0.15rem 0 0; flex-wrap: nowrap; min-height: 2.25rem; }
.st-key-chat_rail [class*="st-key-chat_row_"]:hover { background: rgba(128, 128, 128, 0.1); }
.st-key-chat_rail [class*="st-key-chat_row_"]:has([data-testid="stBaseButton-secondary"]) { background: rgba(128, 128, 128, 0.16); }
.st-key-chat_rail [class*="st-key-chat_sel_"] { min-width: 0; flex: 1 1 0; width: auto; }
.st-key-chat_rail [class*="st-key-chat_row_"] > div:not([class*="st-key-chat_sel_"]) { flex: 0 0 auto; width: auto; }
.st-key-chat_rail [class*="st-key-chat_sel_"] button { width: 100%; justify-content: flex-start; border: 0; background: transparent; padding: 0.4rem 0.6rem; min-height: 2.25rem; }
.st-key-chat_rail [class*="st-key-chat_sel_"] button > div { justify-content: flex-start; min-width: 0; }
.st-key-chat_rail [class*="st-key-chat_sel_"] button p { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-align: left; font-size: 0.9rem; }
.st-key-chat_rail [class*="st-key-chat_sel_"] [data-testid="stBaseButton-secondary"] p { font-weight: 600; }
.st-key-chat_rail [class*="st-key-chat_row_"] button:hover, .st-key-chat_rail [class*="st-key-chat_row_"] button:hover p, .st-key-chat_rail [class*="st-key-chat_rename_row_"] button:hover { color: inherit; }
.st-key-chat_rail [data-testid="stMarkdownContainer"]:has(.sf-rail-label) { margin-bottom: 0.35rem; }
.st-key-chat_rail [class*="st-key-chat_row_"] [data-testid="stCaptionContainer"] { white-space: nowrap; font-size: 0.72rem; opacity: 0.6; padding-right: 0.35rem; }
.st-key-chat_rail [class*="st-key-chat_edit_"], .st-key-chat_rail [class*="st-key-chat_del_"] { display: none; }
.st-key-chat_rail [class*="st-key-chat_row_"]:hover [class*="st-key-chat_edit_"], .st-key-chat_rail [class*="st-key-chat_row_"]:hover [class*="st-key-chat_del_"] { display: block; }
.st-key-chat_rail [class*="st-key-chat_row_"]:hover [data-testid="stCaptionContainer"] { display: none; }
.st-key-chat_rail [class*="st-key-chat_edit_"] button, .st-key-chat_rail [class*="st-key-chat_del_"] button { padding: 0.2rem 0.3rem; min-height: 1.8rem; opacity: 0.65; }
.st-key-chat_rail [class*="st-key-chat_edit_"] button:hover, .st-key-chat_rail [class*="st-key-chat_del_"] button:hover { opacity: 1; }
.st-key-chat_rail [class*="st-key-chat_rename_row_"] { padding: 0.3rem; flex-wrap: nowrap; }
@media (hover: none) {
  .st-key-chat_rail [class*="st-key-chat_edit_"], .st-key-chat_rail [class*="st-key-chat_del_"] { display: block; }
  .st-key-chat_rail [class*="st-key-chat_row_"] [data-testid="stCaptionContainer"] { display: none; }
}
</style>"""

# Keeps the chat transcript scrolled to the newest message (like ChatGPT/Claude): on every render and
# whenever content grows (the "Thinking…" spinner, a long answer), unless you scrolled up to read.
CHAT_SCROLL_JS = """<script>
(function () {
  // run __N__ (unique per render). One watcher lives on the page itself: it finds the current transcript
  // box (Streamlit may swap the element on a rerun) and keeps it pinned to the newest message unless the
  // reader has scrolled up to read older ones.
  const w = window.parent, d = w.document;
  const pin = () => { const el = d.querySelector('.st-key-chat_scroll'); if (el && el.scrollTop !== el.scrollHeight) el.scrollTop = el.scrollHeight; };
  if (w.__sfChatPin) { w.__sfChatPin.stick = true; pin(); setTimeout(pin, 150); setTimeout(pin, 600); return; }
  const st = w.__sfChatPin = {stick: true, el: null};
  const onScroll = (e) => { const el = e.target; st.stick = el.scrollHeight - el.scrollTop - el.clientHeight < 80; };
  w.setInterval(() => {
    const el = d.querySelector('.st-key-chat_scroll');
    if (!el) return;
    if (el !== st.el) { st.el = el; st.stick = true; el.addEventListener('scroll', onScroll, {passive: true}); }
    // size the transcript so the page itself never scrolls: rail and composer stay in place
    const main = d.querySelector('[data-testid="stMain"]');
    const rail = d.querySelector('.st-key-chat_rail');
    if (rail && main) {  // the conversation list scrolls inside its own panel
      const h = Math.max(200, Math.floor(main.getBoundingClientRect().bottom - rail.getBoundingClientRect().top - 28));
      if (Math.abs(h - rail.getBoundingClientRect().height) > 2 || rail.style.maxHeight !== h + 'px') {
        rail.style.setProperty('max-height', h + 'px', 'important');
      }
    }
    const col = d.querySelector('.st-key-chat_main');
    if (main && col) {  // fill the window down to the composer; the page itself never scrolls
      const r = el.getBoundingClientRect(), below = col.getBoundingClientRect().bottom - r.bottom;
      const want = Math.max(160, Math.floor(main.getBoundingClientRect().bottom - r.top - below - 24));
      if (Math.abs(want - r.height) > 2) el.style.setProperty('height', want + 'px', 'important');
    }
    if (st.stick && el.scrollHeight - el.scrollTop - el.clientHeight > 2) el.scrollTop = el.scrollHeight;
  }, 200);
  pin();
})();
</script>"""

# Slash-format Material icons for the chat page avatars (colon format raises StreamlitAPIException).
CHAT_AVATARS = {"user": ":material/person:", "assistant": ":material/auto_awesome:"}


TERMINAL = {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "BUDGET_EXCEEDED"}
STATE_COLORS = {"PENDING": "#d0d4dc", "RUNNING": "#4c8bf5", "SUCCEEDED": "#7cc47f", "INTEGRATED": "#2e9e5b",
                "FAILED": "#e05252", "BLOCKED": "#f0a35e", "CANCELLED": "#999999"}
STATE_ICON = {"COMPLETED": "✅", "FAILED": "❌", "BLOCKED": "⛔", "CANCELLED": "⏹️", "BUDGET_EXCEEDED": "💸"}
FLOW = ["ISSUE_RECEIVED", "REPOSITORY_INSPECTION", "PLANNING", "TASK_VALIDATION", "TASK_EXECUTION",
        "INTEGRATION", "TESTING", "CODE_REVIEW", "APPROVED", "PULL_REQUEST_CREATION", "COMPLETED"]
PALETTE = ["#4c78a8", "#f58518", "#54a24b", "#b279a2", "#e45756", "#72b7b2", "#eeca3b", "#9d755d"]
TYPE_COLORS = {"decision": "#4c78a8", "pitfall": "#e45756", "pattern": "#54a24b", "repo-fact": "#72b7b2",
               "review-lesson": "#f58518", "model-behavior": "#b279a2", "run-log": "#bab0ac",
               "hub": "#333333", "user-note": "#eeca3b", "failure": "#d62728"}
ROLE_ICON = {"planner": "🧭", "reviewer": "🔍", "tester": "🧪", "librarian": "📚"}

st.set_page_config(page_title="SwarmForge", page_icon=branding.asset_bytes("emblem.png"), layout="wide")


def _password_gate() -> None:
    """When SWARMFORGE_DASH_PASSWORD is set (required for `swarmforge dashboard --lan`), ask for it once
    per browser session before showing anything."""
    import hmac
    pw = os.environ.get("SWARMFORGE_DASH_PASSWORD")
    if not pw or st.session_state.get("_authed"):
        return
    with st.container(key="sf-login-brand"):
        st.image(branding.asset_bytes("wordmark.png"), width="stretch")
    st.title("Sign in to SwarmForge")
    given = st.text_input("Password", type="password", key="_pw")
    if given:
        if hmac.compare_digest(given.encode(), pw.encode()):
            st.session_state._authed = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()


st.markdown(branding.styles(), unsafe_allow_html=True)
_password_gate()


# ------------------------------------------------------------------ resources
@st.cache_resource
def get_cfg():
    path = os.environ.get("SWARMFORGE_CONFIG")
    try:
        return load_config(path) if path else load_config(None)
    except ConfigError:
        return None


@st.cache_resource
def get_store(db: str) -> Store:
    return Store(Path(db))


cfg = get_cfg()
db_path = os.environ.get("SWARMFORGE_DB") or (str(cfg.db_path) if cfg else
                                              str(Path("~/.swarmforge/swarmforge.db").expanduser()))
store = get_store(db_path)
vault = Vault(cfg.vault_path) if cfg else None

# theme: `swarmforge dashboard --theme dark|light` is the starting theme for a browser that has not
# picked one (Streamlit remembers each browser's own choice); the sidebar buttons switch it later.
if LAUNCH_THEME in ("light", "dark") and not st.session_state.get("_theme_default_sent"):
    st.session_state["_theme_default_sent"] = True
    components.html(theme_switch_js(LAUNCH_THEME.title(), only_if_unset=True), height=0)
# Chat bubble styling (see-through tints, so it suits both themes); keyed so reruns replace it.
try:
    st.markdown(CHAT_CSS, key="sf_chat_css", unsafe_allow_html=True)
except TypeError:  # Streamlit without keyed markdown: one plain element still replaces the old block
    st.markdown(CHAT_CSS, unsafe_allow_html=True)


HEAL_HELP = ("If the task fails, SwarmForge asks a model why, then automatically retries this many times, "
             "continuing from the failed work with the diagnosis added and more time, budget, repair cycles "
             "and agent steps each attempt. 0 = never retry.")


def heal_input(col, key: str, value=None):
    v = cfg.self_heal.max_attempts if value is None else int(value)
    return int(col.number_input("Auto-fix attempts", 0, 5, max(0, min(5, v)), key=key, help=HEAL_HELP))


def repo_picker(label: str, key: str, *, where=None, value: str | None = None, help: str | None = None) -> str:
    """Repository field that offers the repositories used before (newest first) and accepts new ones."""
    where = where or st
    opts = store.recent_repos()
    if value and value not in opts:
        opts = [value, *opts]
    idx = opts.index(value) if value in opts else (0 if opts else None)
    v = where.selectbox(label, opts, index=idx, key=key, accept_new_options=True,
                        placeholder="Type a folder, owner/repo or GitHub URL",
                        help=help or "Pick one you used before, or type a new one: a local git folder, "
                                     "a GitHub owner/repo, or any GitHub URL of the repo (…/tree/branch "
                                     "selects that branch).")
    return (v or "").strip()


def _hrs(seconds) -> float:
    """Seconds -> hours for number inputs, clamped to the inputs' range so odd configs never crash a form."""
    return float(min(72.0, max(0.05, round((seconds or 0) / 3600, 2))))


def fmt_ts(t, full=False):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M" if full else "%H:%M:%S") if t else "-"


def fmt_dur(s):
    s = int(s or 0)
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"


def model_colors(models):
    return {m: PALETTE[i % len(PALETTE)] for i, m in enumerate(sorted({m for m in models if m}))}


def short_model(m):
    return (m or "").split("/")[-1]


from swarmforge.launcher import pid_alive as _alive  # noqa: E402 - imported once at startup, never inside a
# fragment: importing lazily from the auto-refresh fragment raced with Streamlit's module reloader


def pid_alive(pid):
    return _alive(pid)


def wikilinks_to_md(text: str) -> str:
    return WIKILINK_RE.sub(lambda m: f"🔗 **{(m.group(2) or m.group(1)).strip()}**", text)


# --------------------------------------------------------------------- sidebar
PAGES = ["Work", "Projects", "Pull requests", "New task", "Failures", "Speed", "Chat", "Knowledge", "Benchmarks", "What's new", "Settings"]
NAV_ICON = {"Work": ":material/view_list:", "New task": ":material/add_circle:", "Failures": ":material/healing:",
            "Chat": ":material/chat_bubble:", "Knowledge": ":material/hub:", "Benchmarks": ":material/leaderboard:"}
NAV_ICON["Settings"] = ":material/settings:"
NAV_ICON["Pull requests"] = ":material/merge:"
NAV_ICON["Speed"] = ":material/speed:"
NAV_ICON["Projects"] = ":material/flag:"
NAV_ICON["What's new"] = ":material/auto_awesome:"
SLUG = {p: p.lower().replace("'", "").replace(" ", "-") for p in PAGES}
if st.session_state.get("page") not in PAGES:  # first load: honour ?page=... so pages can be bookmarked
    st.session_state.page = {v: k for k, v in SLUG.items()}.get(st.query_params.get("page", ""), "Work")
if "goto" in st.session_state:  # navigation requested by a button on the previous script run
    st.session_state.page = st.session_state.pop("goto")
page = st.session_state.page
pending_all = store.pending_approvals()


def _nav_badges() -> dict:
    out = {}
    try:
        live = [r for r in store.list_runs(50) if r["state"] not in TERMINAL and r["pid"] and pid_alive(r["pid"])]
        waiting = sum(1 for i in store.queue_items() if i["status"] == "queued")
        bits = ([f"{len(live)} running"] if live else []) + ([f"{waiting} waiting"] if waiting else [])
        if pending_all:
            bits.insert(0, f"🔔 {len(pending_all)}")
        out["Work"] = " · ".join(bits)
        needs = sum(1 for f in store.failures() if (f["outcome"] or "").startswith(("needs you", "still failing",
                                                                                      "not retried")))
        out["Failures"] = f"{needs} need you" if needs else ""
        from swarmforge.history import entries
        seen = set((store.kv_get("feature_history_seen") or "").split(","))
        new = sum(e["id"] not in seen for e in entries())
        out["What's new"] = f"{new} new" if new else ""
    except Exception:  # noqa: BLE001 - badges are decoration
        pass
    return out


# ------------------------------------------------------------ self-update
def _update_status(force: bool = False) -> dict:
    from swarmforge import selfupdate as su
    if os.environ.get("SWARMFORGE_DASH_UPDATE_CHECK", "1") == "0":
        return {"ok": False}
    cache = st.session_state.get("upd_status")
    if force or not cache or time.time() - cache["t"] > 300:   # check GitHub at most every 5 minutes
        try:
            s = su.status(fetch=True)
        except Exception as e:  # noqa: BLE001
            s = {"ok": False, "error": str(e)}
        cache = {"t": time.time(), "s": s}
        st.session_state.upd_status = cache
    return cache["s"]


def do_update_and_restart() -> None:
    """Pull the latest SwarmForge code, restart the queue worker when it's free, restart this dashboard."""
    from swarmforge import selfupdate as su
    with st.status("Updating SwarmForge…", expanded=True) as box:
        try:
            res = su.update()
        except su.UpdateError as e:
            box.update(label="Not updated", state="error")
            st.error(str(e))
            return
        st.write(res["message"])
        for c in res["commits"][:15]:
            st.caption(c)
        if res.get("deps"):
            st.write(f"Python packages: {res['deps']}")
        if res["images_stale"]:
            st.warning("The sandbox images changed. In a terminal run `swarmforge sandbox build` and "
                       "`swarmforge sandbox build --dev` before the next run.")
        st.write(su.request_worker_restart(store))
        if not res["behind"]:
            box.update(label="Already up to date", state="complete")
            st.session_state.upd_status = None
            return
        su.restart_dashboard(str(cfg.source_path) if cfg and cfg.source_path else None,
                             (cfg.workdir if cfg else Path.home() / ".swarmforge") / "logs")
        box.update(label="Updated — restarting the dashboard…", state="complete")
    st.success("The dashboard is restarting with the new code. This page reconnects by itself in about "
               "10–20 seconds (refresh it if it doesn't).")
    st.stop()


def update_box() -> None:
    s = _update_status()
    if not s.get("ok"):
        return
    if s.get("behind"):
        st.info(f"⬆️ **{s['behind']} update(s) available** from GitHub")
        with st.expander("What's new"):
            for c in s.get("incoming", [])[:20]:
                st.caption(c)
        if s.get("dirty"):
            st.caption("⚠️ You have uncommitted changes in: " + ", ".join(s["dirty"][:5])
                       + ". Commit them first, or the update stops (nothing is lost).")
        if st.button("⬆️ Update & restart", type="primary", key="upd_go", width="stretch",
                     help="Pulls the merged work from GitHub, restarts the dashboard, and restarts the queue "
                          "worker as soon as its running tasks finish."):
            do_update_and_restart()
    else:
        c1, c2 = st.columns([3, 1])
        c1.caption(f"✅ Up to date ({s.get('head')})" + (f" · {s['ahead']} local commit(s) not on GitHub"
                                                        if s.get("ahead") else ""))
        if c2.button("↻", key="upd_check", help="Check GitHub for updates now"):
            _update_status(force=True)
            st.rerun()


st.markdown(f"""<style>
section[data-testid="stSidebar"] [class*="st-key-nav_"] button {{
    justify-content: flex-start !important; padding: 0.5rem 0.8rem; border-radius: 0.6rem; width: 100%;
    font-size: 0.95rem; color: inherit; }}
section[data-testid="stSidebar"] [class*="st-key-nav_"] button > div {{ justify-content: flex-start !important; }}
section[data-testid="stSidebar"] [class*="st-key-nav_"] button p {{ text-align: left; }}
section[data-testid="stSidebar"] [class*="st-key-nav_"] button:hover {{ background: rgba(128,128,128,0.12); }}
section[data-testid="stSidebar"] .st-key-nav_{SLUG[page]} button {{
    background: rgba(238, 107, 48, 0.13); color: inherit; font-weight: 650;
    box-shadow: inset 3px 0 0 #ee6b30; }}
section[data-testid="stSidebar"] [class*="st-key-nav_"] {{ margin-bottom: -0.6rem; }}
.sf-badge {{ font-size: 0.75rem; opacity: 0.7; }}
</style>""", unsafe_allow_html=True)

with st.sidebar:
    with st.container(key="sf-sidebar-brand"):
        st.image(branding.asset_bytes("lockup-dark.png"), width="stretch")
        st.markdown('<div class="sf-brand-tagline">AUTONOMOUS SOFTWARE TEAMS</div>', unsafe_allow_html=True)
    st.markdown('<div class="sf-nav-label">WORKSPACE</div>', unsafe_allow_html=True)
    badges = _nav_badges()
    for p in PAGES:
        label = p + (f"  ·  {badges[p]}" if badges.get(p) else "")
        if st.button(label, icon=NAV_ICON[p], key=f"nav_{SLUG[p]}", type="tertiary", width="stretch"):
            st.session_state.page = p
            st.rerun()
    if st.button("Getting started", icon=":material/help:", key="nav_getting_started", type="tertiary",
                 width="stretch"):
        st.session_state.show_welcome = True
        st.session_state.goto = "Work"
        st.rerun()
    st.write("")
    if pending_all:
        st.warning(f"🔔 {len(pending_all)} approval(s) waiting on the Work page")
    st.divider()
    auto = st.toggle("Live refresh (3 s)", value=os.environ.get("SWARMFORGE_DASH_AUTOREFRESH", "1") == "1",
                     help="Refreshes the Work list and the open task while something is running.")
    st.segmented_control(
        "Theme", ["Light", "Dark", "System"], key="theme_pick", width="stretch",
        format_func=lambda c: "Auto" if c == "System" else c,
        on_change=lambda: st.session_state.update(_theme_pick=st.session_state.get("theme_pick")),
        help="Streamlit's own light/dark switch (also in the ⋮ menu, top right): it restyles everything, "
             "tables and charts included, and this browser remembers it. Auto follows your computer.")
    if st.session_state.get("_theme_pick"):
        components.html(theme_switch_js(st.session_state.pop("_theme_pick")), height=0)
    update_box()
    if cfg:
        with st.expander("Setup", icon=":material/settings:"):
            st.caption(f"Config: `{cfg.source_path}`")
            st.caption(f"Knowledge vault: `{cfg.vault_path}`")
            st.caption(f"Work folder: `{cfg.workdir}`")
    else:
        st.error("No swarmforge.yaml found. Start the dashboard with `swarmforge dashboard` "
                 "from your SwarmForge folder.")
try:
    st.query_params["page"] = SLUG[page]
except Exception:  # noqa: BLE001
    pass


# ============================================================== NEW RUN page
def page_new_run():
    from swarmforge.dashboard_pages.new_run import render
    return render(Path, _hrs, cfg, dataclasses, heal_input, pd, re, repo_picker, select_item, st, store, time, vault)


# ============================================================ run details
def runs_overview(runs):
    rows = []
    for r in runs:
        c = store.cost_summary(r["id"])
        active = r["state"] not in TERMINAL
        rows.append({"": "🟢" if active and pid_alive(r["pid"]) else ("⚠️" if active else STATE_ICON.get(r["state"], "")),
                     "run": r["id"], "state": r["state"], "issue": (r["issue_title"] or r["issue_url"] or "")[:70],
                     "repo": r["repo"] or "", "mode": r["mode"], "cost $": round(c["total"], 4),
                     "elapsed": fmt_dur((r["finished_at"] or time.time()) - r["created_at"]),
                     "started": fmt_ts(r["created_at"], True)})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=min(400, 38 + 35 * len(rows)))


def delegation_graph(run, tasks, events):
    colors = model_colors([t["model"] for t in tasks] + [run_cfg_model(run, "planner")])
    dot = ['digraph G { rankdir=LR; compound=true; fontname=Helvetica; '
           'node [shape=box style="rounded,filled" fontname=Helvetica fontsize=10 fontcolor=white]; '
           'edge [fontname=Helvetica fontsize=9 color="#888888"];',
           f'issue [label="Issue\\n{_esc((run["issue_title"] or "")[:40])}" fillcolor="#333333"];']
    cycles = sorted({t["cycle"] for t in tasks})
    reviews = {}
    for e in events:
        if e["kind"] == "review" and e["message"].startswith("verdict="):
            data = json.loads(e["data"]) if e["data"] else {}
            reviews.setdefault(len(reviews), (e["message"], data))
    prev = "issue"
    planner_model = run_cfg_model(run, "planner")
    for c in cycles:
        ctasks = [t for t in tasks if t["cycle"] == c and t["kind"] in ("implement", "fix")]
        tester = [t for t in tasks if t["cycle"] == c and t["kind"] == "test"]
        pid = f"planner{c}"
        dot.append(f'subgraph cluster_{c} {{ label="{"Initial plan" if c == 0 else f"Repair cycle {c}"}"; '
                   f'style=dashed; color="#bbbbbb";')
        dot.append(f'{pid} [label="🧭 Planner\\n{_esc(short_model(planner_model))}" '
                   f'fillcolor="{colors.get(planner_model, "#555")}"];')
        for t in ctasks:
            border = STATE_COLORS.get(t["state"], "#999")
            dot.append(f'"{t["task_key"]}" [label="{t["task_key"]}: {_esc((t["title"] or "")[:30])}\\n'
                       f'{t["role"]} · {_esc(short_model(t["model"]))}" fillcolor="{colors.get(t["model"], "#777")}" '
                       f'color="{border}" penwidth=3];')
            dot.append(f'{pid} -> "{t["task_key"]}" [label="delegates"];')
            for d in t["depends_on"]:
                if any(x["task_key"] == d for x in ctasks):
                    dot.append(f'"{d}" -> "{t["task_key"]}" [style=dashed label="needed by"];')
        tnode = f"tester{c}"
        if tester:
            dot.append(f'{tnode} [label="🧪 Tester\\n{_esc(short_model(tester[0]["model"]))}" '
                       f'fillcolor="{colors.get(tester[0]["model"], "#666")}"];')
        rnode = f"review{c}"
        if c in reviews:
            verdict = reviews[c][0].split()[0].replace("verdict=", "")
            dot.append(f'{rnode} [label="🔍 Reviewer\\n{verdict}" fillcolor="{"#2e9e5b" if verdict == "approve" else "#e05252"}"];')
        dot.append("}")
        dot.append(f'{prev} -> {pid} [label="{"issue" if c == 0 else "problems"}"];')
        sinks = [t["task_key"] for t in ctasks if not any(t["task_key"] in x["depends_on"] for x in ctasks)]
        nxt = tnode if tester else (rnode if c in reviews else None)
        for s in sinks:
            if nxt:
                dot.append(f'"{s}" -> {nxt} [label="merged"];')
        if tester and c in reviews:
            dot.append(f'{tnode} -> {rnode} [label="test evidence"];')
        prev = rnode if c in reviews else (tnode if tester else pid)
    if run["state"] in TERMINAL:
        dot.append(f'end [label="{run["state"]}" shape=ellipse fillcolor="{"#2e9e5b" if run["state"] == "COMPLETED" else "#e05252"}"];')
        dot.append(f"{prev} -> end;")
    dot.append("}")
    return "\n".join(dot), colors


def _esc(s):
    return str(s).replace('"', "'").replace("\\", "/")


def run_cfg_model(run, role):
    try:
        return json.loads(run["config_json"] or "{}")["roles"][role]["model"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return ""


def render_transcript(path: Path):
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for raw in lines:
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = d.get("type")
        if t == "start":
            st.caption(f"Agent session: role **{d.get('role')}**, task **{d.get('task')}**")
            with st.expander("System prompt (role instructions)"):
                st.code(d.get("system", ""), language=None)
            with st.chat_message("user", avatar="📋"):
                st.markdown("**Brief handed to this agent**")
                txt = d.get("user", "")
                st.code(txt[:12000] + ("\n…" if len(txt) > 12000 else ""), language=None)
        elif t == "assistant":
            m = d.get("message", {})
            u = d.get("usage") or {}
            with st.chat_message("assistant", avatar="🤖"):
                cost = f" · ${u['cost_usd']:.5f}" if u.get("cost_usd") is not None else ""
                st.caption(f"step {d.get('step')} · {u.get('prompt_tokens') or '?'} in / "
                           f"{u.get('completion_tokens') or '?'} out tokens{cost}")
                if m.get("reasoning_content"):
                    with st.expander("💭 reasoning"):
                        st.write(m["reasoning_content"][:20000])
                if m.get("content"):
                    st.markdown(m["content"][:8000])
                for tc in m.get("tool_calls") or []:
                    fn = tc["function"]
                    args = fn.get("arguments") or ""
                    try:
                        args = json.dumps(json.loads(args), indent=1, ensure_ascii=False)
                    except (json.JSONDecodeError, TypeError):
                        pass
                    st.markdown(f"🔧 `{fn['name']}`")
                    st.code(args[:6000] + ("\n…" if len(args) > 6000 else ""), language="json")
        elif t == "tool":
            with st.chat_message("tool", avatar="📎"):
                with st.expander(f"result of `{d.get('name')}`"):
                    st.code(d.get("output", "")[:8000], language=None)
        elif t == "terminal":
            st.success(f"Submitted `{d.get('tool')}` (accepted by orchestrator)")
            with st.expander("submitted payload"):
                st.json(d.get("value"))
        elif t == "end":
            (st.info if d.get("status") == "completed" else st.warning)(
                f"Session ended: {d.get('status')} after {d.get('steps')} step(s)"
                + (f" — {d.get('error')}" if d.get("error") else ""))


def run_resumable(run_id: str) -> bool:
    """True if a stopped run left a clone with integrated work that a new run can continue from."""
    from swarmforge.postmortem import resumable
    return resumable(store, run_id)


def _requeue_next(item: dict, kw: dict) -> tuple[int, bool]:
    """Re-add a queue item at the front of the waiting line (never twice)."""
    from swarmforge.runqueue import requeue
    return requeue(store, item, **kw)


def _restarted_as(item: dict | None) -> int | None:
    from swarmforge.runqueue import RESTARTED
    note = (item or {}).get("note") or ""
    if RESTARTED in note:
        try:
            return int(note.rsplit(RESTARTED, 1)[1].split()[0])
        except ValueError:
            return None
    return None


def restart_panel(run: dict) -> None:
    """Start again / continue a stopped run, with editable limits."""
    from swarmforge.launcher import start_run
    issue_src = run["issue_url"] or ""
    is_url = issue_src.startswith("http")
    issue_ok = is_url or Path(issue_src).is_file()
    can_continue = run_resumable(run["id"])
    cl = json.loads(run["config_json"] or "{}").get("limits", {})
    with st.container(border=True):
        st.markdown("**Restart this task**")
        if not issue_ok:
            st.caption("The original task description file is missing, so this run can't be restarted from here.")
            return
        c1, c2, c3 = st.columns(3)
        hours = c1.number_input("Time limit (hours)", 0.05, 72.0,
                                _hrs(max(cl.get("max_runtime_s", 0) * 1.5, cfg.limits.max_runtime_s)),
                                0.5, key=f"rs_h_{run['id']}")
        budget = c2.number_input("Budget (USD)", 0.5, 500.0, float(run["budget_usd"] or cfg.limits.budget_usd),
                                 0.5, key=f"rs_b_{run['id']}")
        cycles = c3.number_input("Max repair cycles", 0, 10, int(cl.get("max_repair_cycles",
                                                                        cfg.limits.max_repair_cycles)),
                                 key=f"rs_c_{run['id']}")
        b1, b2 = st.columns(2)

        def launch(seed):
            try:
                rid, _ = start_run(cfg, repo=None if is_url else (run["repo"] or None),
                                   issue_url=issue_src if is_url else None,
                                   issue_file=None if is_url else issue_src,
                                   mode=run["mode"], budget=budget, max_cycles=int(cycles),
                                   max_runtime_s=int(hours * 3600), seed_run_id=seed,
                                   base=run["base_branch"] if not seed else run["base_branch"])
                select_item(f"r:{rid}")
                st.session_state.flash = f"Started run {rid}."
                st.rerun()
            except ValueError as e:
                st.error(str(e))
        if b1.button("↻ Start again from scratch", key=f"rs_new_{run['id']}"):
            launch(None)
        if b2.button("⏩ Continue from where it stopped", key=f"rs_cont_{run['id']}", disabled=not can_continue,
                     type="primary" if can_continue else "secondary",
                     help="Starts from this run's code (its integrated work) and adds its unresolved problems "
                          "to the task, so nothing already done is lost."):
            launch(run["id"])
        if not can_continue:
            st.caption("Continue is available when the run integrated some work and its workspace still exists.")


# Lines of an explanation action that are shell commands (shown in st.code so they can be copied).
_CMD_PREFIXES = ("git ", "swarmforge ", "docker ", "pip ", "python ", "wsl ", "export ", "cd ", "make ")


def _prefill_new_run(run: dict) -> None:
    """Copy a finished run's settings into the New task form (page_new_run reads these keys)."""
    try:
        limits = json.loads(run.get("config_json") or "{}").get("limits") or {}
    except Exception:  # noqa: BLE001 - a corrupt config must not block a retry
        limits = {}
    if (run.get("issue_url") or "").startswith("http"):
        st.session_state.nr_source = "GitHub issue"
        st.session_state.nr_url = run["issue_url"]
    else:
        st.session_state.nr_source = "Describe it here"
        st.session_state.nr_title = run.get("issue_title") or ""
        st.session_state.nr_body = run.get("issue_body") or ""
        if run.get("repo"):
            store.remember_repo(str(run["repo"]))  # so the repository selectbox offers it
            st.session_state.nr_repo = run["repo"]
    budget = run.get("budget_usd")
    if budget is None:
        budget = cfg.limits.budget_usd if cfg else 5.0
    cycles = limits.get("max_repair_cycles")
    if cycles is None:
        cycles = cfg.limits.max_repair_cycles if cfg else 2
    st.session_state.nr_budget = min(500.0, max(0.5, float(budget)))   # widget ranges
    st.session_state.nr_cycles = min(10, max(0, int(cycles)))


def _explain_card(run: dict, events: list[dict], run_id: str) -> None:
    """Status card above the metrics: the outcome in plain language plus what to do next."""
    try:
        from swarmforge.explain import explain_run
        exp = explain_run(run, events)
    except Exception as e:  # noqa: BLE001 - an explanation bug must never break the page
        st.caption(f"explanation unavailable: {e}")
        return
    with st.container(border=True):
        box = {"success": st.success, "info": st.info, "warning": st.warning,
               "error": st.error}.get(exp.severity, st.info)
        box(f"**{exp.headline}**")
        st.write(exp.detail)
        prev = ""
        for action in exp.actions:
            for line in str(action).splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.lower().startswith(_CMD_PREFIXES) or prev.endswith(":"):
                    st.code(line, language=None)  # copyable command
                else:
                    st.markdown(f"- {line}")
                prev = line
        if (run["state"] in ("FAILED", "BLOCKED", "CANCELLED")
                and st.button("🔁 Run again with the same settings", key=f"again_{run_id}")):
            _prefill_new_run(run)
            st.session_state.goto = "New task"
            st.rerun()


def render_screenshots(run: dict, findings: list[dict], events: list[dict]) -> None:
    """The app as SwarmForge saw it after each test run (light + dark), with the vision model's notes."""
    from swarmforge.report import visual_shots
    shots = visual_shots(run)
    if not shots:
        st.caption("No screenshots for this run. SwarmForge takes them when the repository's "
                   "swarmforge.project.yml has a `preview:` section (how to start the app and which pages "
                   "to open) and the change touches the files it watches.")
        return
    summaries = {}
    for e in events:
        if e["kind"] == "visual" and e.get("data"):
            try:
                d = json.loads(e["data"]) if isinstance(e["data"], str) else e["data"]
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(d, dict) and d.get("summary"):
                summaries[d.get("cycle")] = d["summary"]
    by_cycle: dict[int, dict[str, dict[str, Path]]] = {}
    for p in shots:
        m = re.fullmatch(r"c(\d+)-(.+)-(light|dark)\.png", p.name)
        if m:
            by_cycle.setdefault(int(m.group(1)), {}).setdefault(m.group(2), {})[m.group(3)] = p
    cycles = sorted(by_cycle, reverse=True)
    pick = st.selectbox("Test run", cycles, format_func=lambda c: f"cycle {c}" + (" (latest)" if c == cycles[0] else ""),
                        key=f"shots_cycle_{run['id']}") if len(cycles) > 1 else cycles[0]
    if summaries.get(pick):
        st.info(f"👁 Vision review: {summaries[pick]}")
    vf = [f for f in findings if f["source"] == "visual" and f["cycle"] == pick]
    if vf:
        order = {"critical": 0, "major": 1, "minor": 2, "info": 3}
        for f in sorted(vf, key=lambda f: order.get(f["severity"], 9)):
            icon = {"critical": "🔴", "major": "🟠", "minor": "🟡"}.get(f["severity"], "⚪")
            st.markdown(f"{icon} **{f['title']}** · `{f['file']}`  \n{f['description'] or ''}"
                        + (f"  \n*Fix:* {f['recommendation']}" if f["recommendation"] else ""))
    elif summaries.get(pick):
        st.caption("No visual problems reported.")
    for page, themes in sorted(by_cycle[pick].items()):
        st.markdown(f"**{page}**")
        cols = st.columns(len(themes))
        for col, theme in zip(cols, [t for t in ("light", "dark") if t in themes]):
            col.image(str(themes[theme]), caption=f"{page} · {theme}", width="stretch")


def run_detail(run_id, header=True):
    run = store.get_run(run_id)
    tasks = store.tasks(run_id)
    events = store.events(run_id)
    cost = store.cost_summary(run_id)
    inter = store.interventions(run_id)
    tests = store.test_runs(run_id)
    findings = store.findings(run_id)
    acc = store.acceptance(run_id)
    elapsed = (run["finished_at"] or time.time()) - run["created_at"]

    if header:
        st.subheader(run["issue_title"] or run_id)
    st.caption(f"Run `{run_id}` · {run['mode']} · source {run['issue_url'] or '-'} · repo {run['repo'] or '-'}"
               + (f" · variant `{run['variant_id']}`" if run.get("variant_id") else ""))

    for ap in store.pending_approvals(run_id):
        with st.container(border=True):
            st.markdown(f"### 🔔 Approval needed\n{ap['question']}")
            st.caption("Approving pushes the result branch and opens a **draft** PR. Nothing is merged.")
            a, b, _ = st.columns([1, 1, 4])
            if a.button("✅ Approve", key=f"ap{ap['id']}", type="primary"):
                store.decide_approval(ap["id"], True)
                st.rerun()
            if b.button("✋ Decline", key=f"de{ap['id']}"):
                store.decide_approval(ap["id"], False)
                st.rerun()

    active = run["state"] not in TERMINAL
    if active and run["pid"] and not pid_alive(run["pid"]):
        st.error("The process running this run has exited without finishing (crash, reboot or killed). "
                 "See the Console tab for its output.")
    _explain_card(run, events, run_id)  # plain-language outcome + next steps, above the metrics
    c = st.columns([2.2, 1, 1, 1, 1.2, 1, 1.2])
    c[0].metric("State", run["state"].replace("_", " ").title())
    c[1].metric("Elapsed", fmt_dur(elapsed))
    c[2].metric("Cost", f"${cost['total']:.4f}", help=f"provider-reported ${cost['measured']:.4f} · "
                f"estimated ${cost['estimated']:.4f} · {cost['unavailable_calls'] or 0} call(s) without cost")
    c[3].metric("Budget left", f"${max(0, (run['budget_usd'] or 0) - cost['total']):.2f}")
    c[4].metric("Tokens", f"{cost['total_tokens']:,}")
    c[5].metric("Repair cycle", run["repair_cycle"])
    c[6].metric("Human interventions", len(inter))
    if run["state_reason"]:
        (st.success if run["state"] == "COMPLETED" else st.error if run["state"] in TERMINAL else st.info)(run["state_reason"])
    if run["pr_url"]:
        st.markdown(f"**Draft pull request:** [{run['pr_url']}]({run['pr_url']})")
    elif run["state"] == "COMPLETED" and "PR not created" in (run["state_reason"] or "") and cfg:
        with st.container(border=True):
            st.markdown("**This run was approved but has no pull request yet.** Add a GitHub token "
                        f"(`{cfg.github.token_env}=...` in `.env`, or `gh auth login`), restart the dashboard, then:")
            if st.button("📤 Push branch and open draft PR", key=f"pub_{run_id}"):
                import asyncio
                from swarmforge.publish import publish_run
                with st.spinner("Publishing…"):
                    rc = asyncio.run(publish_run(cfg, store, run_id, lambda q: True))
                if rc == 0:
                    st.success("Draft pull request created.")
                    st.rerun()
                else:
                    st.error("Publishing failed: check that the token has Contents and Pull requests write "
                             "access to this repository (details in the Events tab).")

    with st.expander("📋 Task & summary: what was asked, what was done, what you need to do",
                     expanded=run["state"] in TERMINAL):
        try:
            from swarmforge.report import build_summary
            st.markdown(build_summary(store, run_id))
        except Exception as e:  # the summary must never break the page
            st.caption(f"summary unavailable: {e}")

    visited = [json.loads(e["data"])["to"] for e in events if e["kind"] == "state" and e["data"]]
    chips = [("🔵" if s == run["state"] else "🟢" if s in visited else "⚪") + f" {s}" for s in FLOW]
    if run["state"] in TERMINAL and run["state"] != "COMPLETED":
        chips.append(f"🔴 {run['state']}")
    fixes = visited.count("FIX_REQUIRED")
    st.markdown(" → ".join(chips) + (f"  ·  FIX_REQUIRED ×{fixes}" if fixes else ""))

    tabs = st.tabs(["🧭 Delegation", "🤖 Agents & timeline", "💬 Transcripts", "🧪 Tests", "🔍 Review", "🖼 Screenshots",
                    "📚 Knowledge", "💰 Tokens & cost", "🖥 Console", "📜 Events"])

    with tabs[0]:
        dot, colors = delegation_graph(run, tasks, events)
        st.graphviz_chart(dot, width="stretch")
        st.caption("Box colour = model · border = task state · dashed = dependency. "
                   + " · ".join(f"{short_model(m)}" for m in colors))
        calls = pd.DataFrame(store.llm_calls(run_id))
        per_task = calls.groupby("task_key").agg(calls=("id", "count"), cost=("cost_usd", "sum"),
                                                 tokens=("total_tokens", "sum")) if not calls.empty else None
        st.markdown("#### Briefs and hand-backs")
        for t in [x for x in tasks if x["kind"] in ("implement", "fix", "test")]:
            icon = ROLE_ICON.get(t["role"], "👩‍💻")
            stats = ""
            if per_task is not None and t["task_key"] in per_task.index:
                r = per_task.loc[t["task_key"]]
                stats = f" · {int(r['calls'])} model calls · ${r['cost']:.4f}"
            with st.expander(f"{icon} {t['task_key']} → {t['role']} ({short_model(t['model'])}) · {t['state']}{stats}"):
                a, b = st.columns(2)
                with a:
                    st.markdown("**Planner's brief**")
                    st.write(t["description"] or "")
                    if t["acceptance_criteria"]:
                        st.markdown("Acceptance criteria:\n" + "\n".join(f"- {x}" for x in t["acceptance_criteria"]))
                    if t["expected_files"]:
                        st.markdown("Expected files: " + ", ".join(f"`{x}`" for x in t["expected_files"]))
                    if t["depends_on"]:
                        st.markdown("Waits for: " + ", ".join(t["depends_on"]))
                with b:
                    st.markdown("**Reported back**")
                    st.write(t["summary"] or t["error"] or "_(still working)_")
                    st.caption(f"attempts {t['attempts']} · duration "
                               f"{fmt_dur((t['ended_at'] or time.time()) - t['started_at']) if t['started_at'] else '-'}")

    with tabs[1]:
        running = [t for t in tasks if t["state"] == "RUNNING"]
        last_agent = {}
        for e in events:
            if e["kind"] == "agent" and e["task_key"]:
                last_agent[e["task_key"]] = e
        st.markdown("#### Working right now")
        if running:
            cols = st.columns(max(1, min(4, len(running))))
            for col, t in zip(cols * 3, running):
                le = last_agent.get(t["task_key"])
                col.info(f"**{ROLE_ICON.get(t['role'], '👩‍💻')} {t['role']}** · `{short_model(t['model'])}`\n\n"
                         f"{t['task_key']}: {t['title']}\n\nfor {fmt_dur(time.time() - (t['started_at'] or time.time()))}"
                         + (f"\n\n_last: {le['message'][:120]}_" if le else ""))
        else:
            other = [e for e in events if e["kind"] == "agent"][-1:]
            st.caption("No coding task running." + (f" Last agent activity: {other[0]['message'][:150]}" if other else ""))
        rows = [{"task": f"{t['task_key']} ({t['role']})", "start": datetime.fromtimestamp(t["started_at"]),
                 "end": datetime.fromtimestamp(t["ended_at"] or time.time()), "model": short_model(t["model"]),
                 "state": t["state"]} for t in tasks if t["started_at"]]
        if rows:
            st.markdown("#### Timeline (overlapping bars = agents working in parallel)")
            ch = alt.Chart(pd.DataFrame(rows)).mark_bar(cornerRadius=3).encode(
                x=alt.X("start:T", title=None), x2="end:T", y=alt.Y("task:N", sort=None, title=None),
                color=alt.Color("model:N"), tooltip=["task", "model", "state", "start", "end"]
            ).properties(height=36 * len(rows) + 40)
            st.altair_chart(ch, width="stretch")
        st.dataframe(pd.DataFrame([{
            "task": t["task_key"], "cycle": t["cycle"], "kind": t["kind"], "title": t["title"], "role": t["role"],
            "model": t["model"], "depends_on": ", ".join(t["depends_on"]), "state": t["state"],
            "attempts": t["attempts"]} for t in tasks]), width="stretch", hide_index=True)

    with tabs[2]:
        tdir = Path(run["run_dir"] or "") / "transcripts"
        files = sorted(tdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime) if tdir.is_dir() else []
        if not files:
            st.caption("No agent sessions yet.")
        else:
            names = [f.stem for f in files]
            pick = st.selectbox("Agent session", names, index=len(names) - 1,
                                help="Every agent session: the brief it was given, its reasoning, every tool "
                                     "call and result, and what it submitted.")
            render_transcript(tdir / f"{pick}.jsonl")

    with tabs[3]:
        if tests:
            st.dataframe(pd.DataFrame([{k: tr[k] for k in ("label", "cycle", "exit_code", "passed", "failed", "errors",
                                                             "skipped", "duration_s", "timed_out")} for tr in tests]),
                         width="stretch", hide_index=True)
            sel = st.selectbox("Log", [tr["label"] for tr in tests], index=len(tests) - 1)
            tr = next(t for t in tests if t["label"] == sel)
            if tr["log_path"] and Path(tr["log_path"]).is_file():
                st.code(Path(tr["log_path"]).read_text(encoding="utf-8", errors="replace")[-15000:], language=None)
        else:
            st.caption("No tests executed yet.")
        if acc:
            st.markdown("#### Acceptance criteria (decided from executed tests only)")
            st.dataframe(pd.DataFrame(acc)[["cycle", "criterion_id", "status", "text", "evidence"]],
                         width="stretch", hide_index=True)

    with tabs[4]:
        if findings:
            df = pd.DataFrame(findings)[["cycle", "source", "severity", "file", "line", "title", "description",
                                         "recommendation"]]
            st.dataframe(df, width="stretch", hide_index=True)
        else:
            st.caption("No findings recorded.")
        scope_skips = [e for e in events if e["kind"] == "scope" and "out-of-scope" in e["message"]]
        if scope_skips:
            st.caption(f"skipped out-of-scope findings: {len(scope_skips)}")
        st.markdown("#### Human interventions")
        if inter:
            st.dataframe(pd.DataFrame([{"time": fmt_ts(i["ts"]), "kind": i["kind"], "detail": i["detail"]} for i in inter]),
                         width="stretch", hide_index=True)
        else:
            st.caption("None.")

    with tabs[5]:
        render_screenshots(run, findings, events)

    with tabs[6]:
        kev = [e for e in events if e["kind"] == "knowledge"]
        if not kev:
            st.caption("No knowledge activity yet (notes are retrieved before planning and written after the run).")
        for e in kev:
            st.markdown(f"**{fmt_ts(e['ts'])}** {e['message'][:300]}")
            items = json.loads(e["data"]) if e["data"] else []
            if vault and isinstance(items, list):
                for it in items:
                    n = vault.get(it.get("stem", ""))
                    if n:
                        with st.expander(f"[{n.type}] {n.title}"):
                            st.markdown(wikilinks_to_md(n.body))

    with tabs[7]:
        st.markdown(f"Provider-reported: **${cost['measured']:.4f}** · estimated: **${cost['estimated']:.4f}** · "
                    f"calls without cost info: **{cost['unavailable_calls'] or 0}**")
        br = pd.DataFrame(store.cost_by_role(run_id))
        if not br.empty:
            st.dataframe(br, width="stretch", hide_index=True)
            st.altair_chart(alt.Chart(br).mark_bar().encode(
                x=alt.X("cost_usd:Q", title="USD"), y=alt.Y("role:N", sort="-x"), tooltip=list(br.columns)), width="stretch")
        calls = store.llm_calls(run_id)
        if calls:
            cdf = pd.DataFrame(calls)
            cdf["t"] = pd.to_datetime(cdf["ended_at"], unit="s")
            cdf["cumulative_cost"] = cdf["cost_usd"].fillna(0).cumsum()
            st.altair_chart(alt.Chart(cdf).mark_line(point=True).encode(
                x=alt.X("t:T", title=None), y=alt.Y("cumulative_cost:Q", title="cumulative USD")), width="stretch")

    with tabs[8]:
        log = cfg.workdir / "logs" / f"{run_id}.log" if cfg else None
        if log and log.is_file():
            st.code(log.read_text(encoding="utf-8", errors="replace")[-20000:], language=None)
        else:
            st.caption("No console log (this run was started from the command line).")

    with tabs[9]:
        kinds = sorted({e["kind"] for e in events})
        pick = st.multiselect("Kinds", kinds, default=kinds)
        rows = [{"time": fmt_ts(e["ts"]), "level": e["level"], "kind": e["kind"], "task": e["task_key"] or "",
                 "role": e["role"] or "", "message": e["message"]} for e in events if e["kind"] in pick]
        st.dataframe(pd.DataFrame(rows[::-1]), width="stretch", hide_index=True, height=600)


# ============================================================ KNOWLEDGE page
def page_knowledge():
    from swarmforge.dashboard_pages.knowledge import render
    return render(st, vault, wikilinks_to_md, _esc, TYPE_COLORS)


# ================================================================= WORK page
QSTATUS = {"queued": "⏳", "running": "🟢", "done": "✅", "failed": "❌", "cancelled": "⏹️", "skipped": "⏭️"}
FAILED_STATES = {"FAILED", "BLOCKED", "CANCELLED", "BUDGET_EXCEEDED"}
FILTERS = {"All": None, "Active": {"active"}, "Waiting": {"waiting"}, "Finished": {"done"},
           "Failed": {"failed"}}


def _short_repo(repo: str) -> str:
    repo = (repo or "").strip().rstrip("/\\")
    m = re.match(r"(?:https?://|git@)github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?(?:/.*)?$", repo)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    parts = re.split(r"[/\\]", repo)
    return "/".join(parts[-2:]) if len(parts) > 2 else repo


def _run_status(r: dict, approvals: set) -> tuple[str, str, str]:
    """(group, icon, label) for a run."""
    if r["id"] in approvals:
        return "active", "🔔", "Needs your approval"
    if r["state"] not in TERMINAL:
        if r["pid"] and not pid_alive(r["pid"]):
            return "failed", "⚠️", "Stopped unexpectedly"
        if (r.get("state_reason") or "").startswith("waiting for the repository"):
            return "active", "⏸️", "Waiting for the repo"
        if r.get("heal_attempt"):
            return "active", "🩹", f"Self-healing (attempt {r['heal_attempt']})"
        return "active", "🟢", "Running"
    if r["state"] == "COMPLETED" and r.get("heal_attempt"):
        return "done", "✅", "Done · self-healed"
    if r["state"] == "COMPLETED":
        return "done", "✅", "Done"
    return "failed", STATE_ICON.get(r["state"], "❌"), r["state"].replace("_", " ").title()


def _merge_states() -> dict[str, str]:
    """Latest auto-merge outcome per run id (merged / waiting for you / being fixed / ...)."""
    try:
        from swarmforge import automerge as am
        kinds = {am.MERGED: "merged", am.MANUAL: "merged", am.HELD: "waiting for you",
                 am.FIXING: "being fixed", am.CLOSED: "replaced by a fix-up", "auto_merge_reverted": "reverted"}
        rows = store._all(f"SELECT run_id, kind FROM interventions WHERE kind IN ({','.join('?' * len(kinds))}) "
                          "ORDER BY id", tuple(kinds))
    except Exception:
        return {}
    return {r["run_id"]: kinds[r["kind"]] for r in rows}


def _task_key(w: dict) -> str:
    return re.sub(r"\s+", " ", (w["title"] or "").strip().lower()) + "|" + _short_repo(w["repo"]).lower()


def collapse_attempts(items: list[dict]) -> list[dict]:
    """One row per task: repeated attempts of the same task (same title and repo) fold into the most
    relevant one (running, else waiting, else the newest). `items` must already be in display order."""
    out, by_key = [], {}
    for w in items:
        k = _task_key(w)
        if k in by_key:
            by_key[k]["attempts"].append(w)
            continue
        w = {**w, "attempts": []}
        by_key[k] = w
        out.append(w)
    return out


def work_items() -> list[dict]:
    """Every queue task and every run as one list: active first, then waiting (queue order), then newest."""
    runs = store.list_runs(300)
    by_id = {r["id"]: r for r in runs}
    healed = {r["heal_of"] for r in runs if r.get("heal_of")}  # earlier attempts: shown under Failures
    approvals = {a["run_id"] for a in store.pending_approvals()}
    merges = _merge_states()
    out, used = [], set()
    queue = store.queue_items()
    from swarmforge.runqueue import lane as _lane
    waiting_pos: dict[int, int] = {}
    per_lane: dict[str, int] = {}
    running_by_lane = {_lane(q): q for q in queue if q["status"] == "running"}
    for q in queue:
        if q["status"] == "queued":
            per_lane[_lane(q)] = per_lane.get(_lane(q), 0) + 1
            waiting_pos[q["id"]] = per_lane[_lane(q)]
    multi = len({_lane(q) for q in queue if q["status"] in ("queued", "running")}) > 1
    for i in queue:
        r = by_id.get(i["run_id"]) if i["run_id"] else None
        if r:
            used.add(r["id"])
        if i["status"] == "queued":
            grp, icon, label = "waiting", "⏳", (f"Waiting (#{waiting_pos[i['id']]} in {_lane(i)})" if multi
                                                 else f"Waiting (#{waiting_pos[i['id']]} in line)")
        elif i["status"] == "tentative":
            grp, icon, label = "waiting", "○", "Tentative · not ready"
        elif _restarted_as(i):
            grp, icon, label = "failed", "↻", f"Restarted as #{_restarted_as(i)}"
        elif r:
            grp, icon, label = _run_status(r, approvals)
        elif i["status"] == "running":
            grp, icon, label = "active", "🟢", "Starting"
        elif i["status"] == "done":
            grp, icon, label = "done", "✅", "Done"
        else:
            grp, icon, label = "failed", QSTATUS.get(i["status"], "❌"), i["status"].title()
        out.append({"key": f"q:{i['id']}", "group": grp, "icon": icon, "status": label, "title": i["title"],
                    "repo": i["repo"], "item": i, "run": r, "merge": merges.get(r["id"]) if r else None,
                    "waiting_on": running_by_lane.get(_lane(i)) if i["status"] == "queued" else None,
                    "when": (r["created_at"] if r else i.get("created_at")) or 0,
                    "order": waiting_pos.get(i["id"], 0), "hidden": False,
                    "archived": bool(r.get("archived")) if r else False})
    for r in runs:
        if r["id"] in used:
            continue
        grp, icon, label = _run_status(r, approvals)
        out.append({"key": f"r:{r['id']}", "group": grp, "icon": icon, "status": label,
                    "title": r["issue_title"] or r["issue_url"] or r["id"], "repo": r["repo"] or "",
                    "merge": merges.get(r["id"]),
                    "item": None, "run": r, "when": r["created_at"] or 0, "order": 0,
                    "hidden": r["id"] in healed, "archived": bool(r.get("archived"))})
    rank = {"active": 0, "waiting": 1}
    out.sort(key=lambda w: (rank.get(w["group"], 2), w["order"] if w["group"] == "waiting" else -w["when"]))
    return out


def queue_picker(col, key: str, value: str | None = None) -> str:
    """Pick an existing queue ("pile") or name a new one. Works inside forms (no rerun needed)."""
    names = store.queue_names()
    default = value or st.session_state.get("last_queue") or names[0]
    if default not in names:
        names = [default] + names
    a, b = col.columns(2)
    pick = a.selectbox("Queue (pile)", names, index=names.index(default), key=f"{key}_pick",
                       help="Each queue runs one task at a time, in order. Different queues run side by side, "
                            "so use one queue per project.")
    new = b.text_input("…or a new queue", key=f"{key}_new", placeholder="e.g. commanderforge")
    return (new.strip() or pick).strip() or "main"


def queue_bar() -> None:
    from swarmforge import runqueue as rq
    items = store.queue_items()
    pid = rq.worker_pid(store)
    paused = store.kv_get(rq.PAUSED_KEY) == "1"
    queued = [i for i in items if i["status"] == "queued"]
    running = [i for i in items if i["status"] == "running"]
    if not items:
        return
    with st.container(border=True, key="qbar"):
        c1, c2, c3, c4 = st.columns([2.6, 1.3, 1.3, 2], vertical_alignment="center")
        counts = f"{len(running)} running · {len(queued)} waiting"
        if pid:
            c1.markdown(f"**Queue** 🟢 working · {counts}" + (" · pauses after the current task" if paused else ""),
                        help=f"Queue worker process {pid}")
        else:
            c1.markdown("**Queue** ⚪ stopped" + (" (paused)" if paused and queued else "") + f" · {counts}")
        if not pid:
            if c2.button("▶ Start queue", type="primary", disabled=not queued or not cfg, key="qb_start"):
                rq.resume_all(store)
                rq.start_worker(cfg, store)
                time.sleep(1.5)
                st.rerun(scope="app")
        elif not paused:
            if c2.button("⏸ Pause after current", key="qb_pause"):
                store.kv_set(rq.PAUSED_KEY, "1")
                st.rerun(scope="app")
        else:
            if c2.button("▶ Keep going", key="qb_resume"):
                rq.resume_all(store)
                st.rerun(scope="app")
        if any(i["status"] in ("done", "failed", "cancelled", "skipped") for i in items):
            if c3.button("🧹 Clear finished", key="qb_clear", help="Removes finished queue entries from the "
                         "queue. Their runs stay in the list."):
                store.queue_clear_finished()
                st.rerun(scope="app")
        blocked = [i for i in items if i["status"] == "failed" and "BLOCKED:" in (i.get("note") or "")
                   and rq.RESTARTED not in (i.get("note") or "")]
        if blocked:
            b1, b2 = st.columns([5, 1.3])
            b1.caption(f"⛔ {len(blocked)} task(s) stopped as BLOCKED before doing any work (e.g. Docker or the "
                       "sandbox image). Once that is fixed they can go back in line, in their original order.")
            if b2.button("↻ Retry blocked", key="qb_retry_blocked"):
                rq.retry_blocked(store)
                if not pid and cfg:
                    rq.start_worker(cfg, store)
                st.rerun(scope="app")
        policy = store.kv_get(rq.POLICY_KEY) or "stop"
        new_policy = c4.selectbox("If a task fails", ["stop", "continue"], index=["stop", "continue"].index(policy),
                                  key="qb_policy", format_func=lambda p: {"stop": "If a task fails: pause its queue",
                                                         "continue": "If a task fails: continue"}[p],
                                  label_visibility="collapsed")
        if new_policy != policy:
            store.kv_set(rq.POLICY_KEY, new_policy)
        lanes = [n for n in store.queue_names()
                 if any(rq.lane(i) == n and i["status"] in ("queued", "running") for i in items)
                 or rq.lane_paused(store, n)]
        if len(lanes) > 1 or any(rq.lane_paused(store, n) for n in lanes):
            limit = cfg.limits.max_parallel_queues if cfg else 3
            for n in lanes:
                mine = [i for i in items if rq.lane(i) == n]
                run_ = [i for i in mine if i["status"] == "running"]
                wait_ = [i for i in mine if i["status"] == "queued"]
                lp = rq.lane_paused(store, n)
                q1, q2 = st.columns([5, 1.3], vertical_alignment="center")
                q1.markdown(f"🗂 **{n}** — " + ("⏸ paused · " if lp else "")
                            + (f"running: {run_[0]['title'][:70]}" if run_ else "idle")
                            + (f" · {len(wait_)} waiting" if wait_ else ""),
                            help=f"Queues run side by side (up to {limit} at once); each runs its own tasks in order.")
                env = next((i for i in wait_ if (i.get("note") or "").startswith(rq.ENV_PAUSE_NOTE)), None)
                if lp and env:
                    q1.warning("Paused because of an environment problem; fix it, then Resume. "
                               + env["note"][len(rq.ENV_PAUSE_NOTE) + 2:][:400], icon="⚠️")
                if lp and q2.button("▶ Resume", key=f"qb_lane_res_{n}"):
                    rq.set_lane_paused(store, n, False)
                    if not pid and cfg:
                        rq.start_worker(cfg, store)
                    st.rerun(scope="app")
                if not lp and wait_ and q2.button("⏸ Pause", key=f"qb_lane_pause_{n}",
                                                  help="Start nothing new in this queue (the running task finishes)."):
                    rq.set_lane_paused(store, n, True)
                    st.rerun(scope="app")


def select_item(key: str | None) -> None:
    """Open (expand) an item in the Work list; None collapses."""
    st.session_state.sel_item = key


def _wkey(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", key)


ROW_COLS = [0.3, 0.3, 4.6, 1.9, 2.6, 1.8, 0.7, 0.85]
WORK_CSS = """<style>
[class*="st-key-wr_"] { position: relative; border-bottom: 1px solid rgba(128,128,128,0.18);
                        padding: 0.15rem 0.25rem; border-radius: 0.4rem; }
[class*="st-key-wr_"]:hover { background: rgba(128,128,128,0.08); }
[class*="st-key-wr_"] [data-testid="stColumn"],
[class*="st-key-wr_"] [data-testid="stHorizontalBlock"],
[class*="st-key-wr_"] [data-testid="stVerticalBlock"],
[class*="st-key-wr_"] [data-testid="stElementContainer"],
[class*="st-key-wr_"] .stButton { position: static !important; }
[class*="st-key-op_"] { height: 0 !important; margin-top: -1rem; overflow: visible; }
[class*="st-key-op_"] button { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0;
                               z-index: 1; cursor: pointer; }
[class*="st-key-wr_"]:has([class*="st-key-ck_"] input:checked) { background: rgba(238, 107, 48, 0.10); }
[class*="st-key-ck_"], [class*="st-key-ckall"] { position: relative !important; z-index: 2; }
[class*="st-key-wr_"] p { margin: 0; font-size: 0.9rem; white-space: nowrap; overflow: hidden;
                          text-overflow: ellipsis; }
.st-key-qbar { gap: 0.4rem; padding-top: 0.6rem; padding-bottom: 0.6rem; }
.st-key-qbar p { margin: 0; }
.st-key-whead p { font-size: 0.75rem; opacity: 0.6; margin: 0; letter-spacing: 0.04em; }
[class*="st-key-wr_"] .sf-tries { font-size: 0.72rem; font-weight: 600; opacity: 0.75; padding: 0.05rem 0.4rem;
                                  border-radius: 999px; background: rgba(128,128,128,0.18); margin-left: 0.35rem; }
.sf-wsec { font-size: 0.78rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; opacity: 0.6;
           margin: 1.1rem 0 0.1rem 0.25rem; }
[class*="st-key-wr_"] [data-testid="stMarkdownContainer"] p { line-height: 1.6; }
[class*="st-key-wr_"] [data-testid="stCheckbox"] label { min-height: 0; }
[class*="st-key-wopen_"] { border-left: 3px solid #ee6b30; padding-left: 0.9rem; margin: 0.2rem 0 1rem 0.4rem; }
[class*="st-key-wsel_"] { background: rgba(238, 107, 48, 0.10) !important; }
</style>"""


def _short_result(text: str) -> str:
    """The run's last word, shortened to what matters in a list."""
    t = (text or "").strip()
    low = t.lower()
    if "queue worker stopped" in low:
        return "Interrupted (the worker restarted)"
    if low.startswith("the sandbox image") and "missing" in low:
        return "Sandbox image is missing a tool"
    if low.startswith("maximum runtime"):
        return "Hit the time limit"
    m = re.match(r"repair limit reached.*?(\d+) unresolved problem", low)
    if m:
        return f"Still {m.group(1)} problem(s) after repairs"
    if low.startswith("unexpected error: giterror"):
        return "Git error: " + t.split(":", 2)[-1].strip()[:60]
    return t[:120]


def _cells(w: dict, costs: dict[str, float]) -> dict:
    r, i = w["run"], w["item"]
    now = ""
    pr = re.search(r"/pull/(\d+)", (r.get("pr_url") or r.get("state_reason") or "")) if r else None
    if pr and w["group"] == "done":
        now = f"PR #{pr.group(1)}" + (f" · {w['merge']}" if w.get("merge") else "")
    elif w["group"] == "active" and r and (r["state_reason"] or "").startswith("waiting for the repository"):
        now = r["state_reason"][:120]
    elif w["group"] == "active" and r:
        now = r["state"].replace("_", " ").title() + (f" · repair cycle {r['repair_cycle']}" if r["repair_cycle"] else "")
    elif r and r["state_reason"]:
        now = _short_result(r["state_reason"])
    elif i and i["note"]:
        now = _short_result(i["note"])
    cost = costs.get(r["id"], 0.0) if r else None
    return {"now": now, "cost": f"${cost:.2f}" if cost is not None else "",
            "time": fmt_dur((r["finished_at"] or time.time()) - r["created_at"]) if r else "",
            "when": fmt_ts(r["created_at"], True)[5:] if r else ""}


def _checked(shown: list[dict]) -> list[dict]:
    return [w for w in shown if st.session_state.get(f"ck_{_wkey(w['key'])}")]


def _run_chain(run_id: str) -> list[str]:
    """A run plus the earlier self-healing attempts it continued from."""
    out, seen = [], set()
    while run_id and run_id not in seen:
        seen.add(run_id)
        r = store.get_run(run_id)
        if not r:
            break
        out.append(run_id)
        run_id = r.get("heal_of")
    return out


def _delete_items(ws: list[dict], files: bool) -> tuple[int, int]:
    import shutil
    import stat

    def unlock(func, path, _exc):  # git objects are read-only on Windows
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass
    done = skipped = 0
    root = cfg.workdir.resolve() if cfg else None
    for w in ws:
        if w["group"] == "active" or (w.get("item") or {}).get("project_id"):
            skipped += 1
            continue
        runs = []
        for q in ([w["item"]] if w["item"] else []):
            runs += [q["run_id"]] if q["run_id"] else []
            store.queue_remove(q["id"])
        if w["run"]:
            runs.append(w["run"]["id"])
        for rid in {x for r0 in runs for x in _run_chain(r0)}:
            r = store.get_run(rid)
            if files and r and r["run_dir"] and root:
                d = Path(r["run_dir"]).resolve()
                if root in d.parents:  # never delete outside SwarmForge's work folder
                    shutil.rmtree(d, onerror=unlock)
            store.delete_run(rid)
        done += 1
    return done, skipped


def bulk_bar(shown: list[dict]) -> None:
    sel = _checked(shown)
    if any((w.get("item") or {}).get("project_id") for w in sel):
        st.info("Manage project tasks from Projects so their plan, merge barriers and spend stay together.")
        sel = [w for w in sel if not (w.get("item") or {}).get("project_id")]
    if not sel:
        return
    with st.container(border=True, key="bulkbar"):
        c = st.columns([1.6, 1, 1, 1, 1.1, 1])
        c[0].markdown(f"**{len(sel)} selected**")
        running = [w for w in sel if w["group"] == "active" and w["run"]]
        waiting = [w for w in sel if w["group"] == "waiting"]
        failed_q = [w for w in sel if w["item"] and w["item"]["status"] in ("failed", "cancelled", "skipped")]
        if c[1].button("Delete", icon=":material/delete:", key="bk_del"):
            st.session_state.confirm_delete = True
        if c[2].button("Cancel", icon=":material/stop_circle:", key="bk_cancel", disabled=not running,
                       help="Stop the selected running tasks."):
            for w in running:
                request_run_cancel(store, w["run"]["id"])
                store.event(w["run"]["id"], "cancel", "cancel requested from dashboard", level="warn")
            st.session_state.flash = f"Cancel requested for {len(running)} task(s)."
            st.rerun(scope="app")
        if c[3].button("Run again", icon=":material/replay:", key="bk_again", disabled=not failed_q,
                       help="Put the selected failed queue tasks back in the queue (front of the line)."):
            for w in failed_q:
                i = w["item"]
                _requeue_next(i, dict(mode=i["mode"], budget=i["budget"], max_cycles=i["max_cycles"],
                                      chain=i["chain"], approval=i["approval"], max_runtime_s=i["max_runtime_s"],
                                      heal_attempts=i.get("heal_attempts")))
            st.session_state.flash = f"Queued {len(failed_q)} task(s) again."
            _clear_checks(shown)
            st.rerun(scope="app")
        if c[4].button("Move to front", icon=":material/vertical_align_top:", key="bk_front", disabled=not waiting):
            others = [q for q in store.queue_items() if q["status"] == "queued"]
            top = min((q["position"] for q in others), default=1)
            for n, w in enumerate(waiting):
                store.queue_update(w["item"]["id"], position=top - len(waiting) + n - 0.5)
            st.rerun(scope="app")
        if c[5].button("Clear", icon=":material/close:", key="bk_clear", help="Unselect all"):
            _clear_checks(shown)
            st.rerun(scope="app")
        if st.session_state.get("confirm_delete"):
            n_active = sum(1 for w in sel if w["group"] == "active")
            st.warning(f"Delete {len(sel) - n_active} task(s) and their run history? This can't be undone."
                       + (f" {n_active} running task(s) will be skipped; cancel them first." if n_active else "")
                       + " Failure post-mortems are kept for learning.")
            files = st.checkbox("Also delete their files on disk (clones, logs, transcripts)", value=True,
                                key="bk_files")
            d1, d2, _ = st.columns([1, 1, 4])
            if d1.button("Yes, delete", type="primary", key="bk_yes"):
                done, skipped = _delete_items(sel + [a for w in sel for a in w.get("attempts") or []], files)
                st.session_state.confirm_delete = False
                _clear_checks(shown)
                if st.session_state.get("sel_item") in {w["key"] for w in sel if w["group"] != "active"}:
                    select_item(None)
                st.session_state.flash = f"Deleted {done} task(s)." + (f" Skipped {skipped} running." if skipped else "")
                st.rerun(scope="app")
            if d2.button("Keep them", key="bk_no"):
                st.session_state.confirm_delete = False
                st.rerun(scope="app")


def _clear_checks(shown: list[dict]) -> None:
    for w in shown:
        st.session_state[f"ck_{_wkey(w['key'])}"] = False
    st.session_state["ckall"] = False


def _toggle_all(keys: list[str]) -> None:
    v = st.session_state.get("ckall", False)
    for k in keys:
        st.session_state[f"ck_{k}"] = v


def _row_cols(show_repo: bool) -> list[float]:
    return ROW_COLS if show_repo else ROW_COLS[:5] + ROW_COLS[6:]


def work_row(w: dict, show_repo: bool, costs: dict[str, float]) -> None:
    k = _wkey(w["key"])
    is_open = st.session_state.get("sel_item") == w["key"]
    cells = _cells(w, costs)
    tries = len(w.get("attempts") or [])
    with st.container(key=f"wr_{k}"):
        c = st.columns(_row_cols(show_repo), vertical_alignment="center", gap="small")
        c[0].checkbox("select", key=f"ck_{k}", label_visibility="collapsed")
        c[1].markdown(w["icon"])
        c[2].markdown(f"{'▾' if is_open else '▸'} {'📦 ' if w.get('archived') else ''}"
                      f"**{w['title'] or '(untitled)'}**"
                      + (f" <span class='sf-tries' title='earlier attempts of this task are folded into this row'>"
                         f"×{tries + 1}</span>" if tries else ""), unsafe_allow_html=True)
        blocker = w.get("waiting_on")
        if blocker:
            c[3].markdown(f"Waiting on #{blocker['id']}", help=blocker["title"])
        else:
            c[3].markdown(w["status"])
        c[4].markdown(cells["now"] or " ")
        rest = c[5:]
        if show_repo:
            rest[0].markdown(_short_repo(w["repo"]))
            rest = rest[1:]
        rest[0].markdown(cells["cost"] or " ")
        rest[1].markdown(cells["time"] or " ")
        if rest[1].button("open", key=f"op_{k}", help="Show / hide details"):
            select_item(None if is_open else w["key"])
            st.rerun(scope="app")


def _item_mode(w: dict) -> str | None:
    """The mode of a work item: the run's when there is one, else the queue task's."""
    if w["run"]:
        return w["run"].get("mode")
    return w["item"].get("mode") if w["item"] else None


SECTIONS = {"active": "Running now", "waiting": "Up next", "finished": "Finished"}


def work_list(items: list[dict], every) -> None:
    st.markdown(WORK_CSS, unsafe_allow_html=True)
    queue_bar()
    visible = [w for w in items if not w["hidden"]]
    if not visible:
        st.info("Nothing here yet. Go to **New task** to start a run or fill the queue.")
        return
    f1, f2, f3 = st.columns([3.2, 2, 1.1], vertical_alignment="center")
    query = f2.text_input("Search", key="w_search", placeholder="🔎 Search title, repo or run id",
                          label_visibility="collapsed").strip().lower()
    every_try = f3.toggle("Every attempt", key="w_all",
                          help="Show each attempt of a task as its own row. Off: one row per task, with the "
                               "number of attempts (×N) next to its title.")
    with st.expander("More filters"):
        g1, g2, g3, g4 = st.columns([1.5, 1.5, 1.1, 1.1], vertical_alignment="bottom")
        states = g1.multiselect("State", sorted({w["run"]["state"] for w in items if w["run"]}),
                                key="w_state", placeholder="All states")
        repo_sel = g2.selectbox("Repository", ["All repos"] + sorted({_short_repo(w["repo"]) for w in items
                                                                     if w["repo"]}), key="w_repo")
        mode_sel = g3.selectbox("Mode", ["All modes", "swarm", "single"], key="w_mode")
        show_arch = g4.checkbox("Show archived", key="w_arch",
                                help="Archived runs stay in the database but are hidden from this list.")
    base = visible if show_arch else [w for w in visible if not w["archived"]]
    if states:
        base = [w for w in base if w["run"] and w["run"]["state"] in states]
    if repo_sel and repo_sel != "All repos":
        base = [w for w in base if _short_repo(w["repo"]) == repo_sel]
    if mode_sel and mode_sel != "All modes":
        base = [w for w in base if _item_mode(w) == mode_sel]
    if query:
        base = [w for w in base if query in f"{w['title']} {w['repo']} {w['run']['id'] if w['run'] else ''}".lower()]
    if not every_try:
        base = collapse_attempts(base)
    counts = {g: sum(1 for w in base if w["group"] == g) for g in ("active", "waiting", "done", "failed")}
    labels = {"All": f"All ({len(base)})", "Active": f"Running ({counts['active']})",
              "Waiting": f"Up next ({counts['waiting']})", "Finished": f"Finished ({counts['done']})",
              "Failed": f"Failed ({counts['failed']})"}
    filt = f1.segmented_control("Show", list(FILTERS), key="w_filter", format_func=labels.get,
                                label_visibility="collapsed", default="All") or "All"
    shown = [w for w in base if FILTERS[filt] is None or w["group"] in FILTERS[filt]]
    if not shown:
        st.caption("No tasks match.")
        return
    history_limit = st.session_state.get("work_history_limit", 25)
    history = [w for w in shown if w["group"] in ("done", "failed")]
    if len(history) > history_limit:
        visible_history = {w["key"] for w in history[:history_limit]}
        shown = [w for w in shown if w["group"] not in ("done", "failed") or w["key"] in visible_history]
        if st.button(f"Show more finished tasks ({history_limit} of {len(history)} shown)", key="work_more_history"):
            st.session_state.work_history_limit = history_limit + 25
            st.rerun(scope="app")
    bulk_bar(shown)
    show_repo = len({_short_repo(w["repo"]).lower() for w in shown}) > 1
    keys = [_wkey(w["key"]) for w in shown]
    with st.container(key="whead"):
        h = st.columns(_row_cols(show_repo), vertical_alignment="center", gap="small")
        h[0].checkbox("all", key="ckall", label_visibility="collapsed", on_change=_toggle_all, args=(keys,),
                      help="Select all shown")
        names = ["TASK", "STATUS", "RESULT"] + (["REPO"] if show_repo else []) + ["COST", "TIME"]
        for col, name in zip(h[2:], names):
            col.markdown(name)
    sel = st.session_state.get("sel_item")
    if sel and sel not in {w["key"] for w in shown}:  # opened from elsewhere (e.g. Failures): show on top
        w = next((x for x in items if x["key"] == sel), None)
        if w:
            with st.container(key=f"wopen_top_{_wkey(w['key'])}"):
                if st.button("Close", icon=":material/close:", key="w_close_top"):
                    select_item(None)
                    st.rerun(scope="app")
                item_detail(w, every)
    section = None
    from swarmforge.projects.store import Projects
    project_names = {p["id"]: p["name"] for p in Projects(store).list()}
    if any(w.get("item") and w["item"].get("project_id") for w in shown):
        shown.sort(key=lambda w: ((w.get("item") or {}).get("project_id") or "~", w["order"]))
    costs = store.run_cost_totals([w["run"]["id"] for w in shown if w["run"]])
    previous_project = None
    for w in shown:
        project_id = (w.get("item") or {}).get("project_id")
        if project_id != previous_project:
            st.subheader(project_names.get(project_id, "Single tasks"))
            section = None
        previous_project = project_id
        sec = w["group"] if w["group"] in ("active", "waiting") else "finished"
        if filt == "All" and sec != section:
            st.markdown(f"<div class='sf-wsec'>{SECTIONS[sec]}</div>", unsafe_allow_html=True)
        section = sec
        if (w.get("item") or {}).get("status") == "tentative":
            with st.container(key=f"tentative_{_wkey(w['key'])}"):
                work_row(w, show_repo, costs)
        else:
            work_row(w, show_repo, costs)
        if sel == w["key"]:
            with st.container(key=f"wopen_{_wkey(w['key'])}"):
                if w.get("attempts"):
                    st.caption("Earlier attempts of this task: " + " · ".join(
                        f"{a['icon']} {a['status']} ({fmt_ts(a['when'], True)[5:]})" for a in w["attempts"][:8])
                        + (" …" if len(w["attempts"]) > 8 else "") + ". Turn on **Every attempt** to open them.")
                item_detail(w, every)
    st.caption("Click a task to open it. Tick boxes to act on several tasks at once.")


def waiting_editor(i: dict) -> None:
    """Everything about a not-yet-started queue task can be changed."""
    b1, b2, b3, b4, _ = st.columns([1, 0.7, 0.8, 0.9, 5])
    if b1.button("⏫ Run next", key=f"q_top_{i['id']}", help="Move to the front of the waiting line."):
        from swarmforge.runqueue import lane as _lane
        waiting = [q for q in store.queue_items() if q["status"] == "queued" and q["id"] != i["id"]
                   and _lane(q) == _lane(i)]
        if waiting:
            store.queue_update(i["id"], position=waiting[0]["position"] - 0.5)
        st.rerun()
    if b2.button("⬆ Up", key=f"q_up_{i['id']}"):
        store.queue_move(i["id"], -1)
        st.rerun()
    if b3.button("⬇ Down", key=f"q_dn_{i['id']}"):
        store.queue_move(i["id"], +1)
        st.rerun()
    if b4.button("🗑 Remove", key=f"q_rm_{i['id']}"):
        store.queue_remove(i["id"])
        select_item(None)
        st.rerun()
    if i["seed_run_id"]:
        st.caption(f"Continues from run `{i['seed_run_id']}` (its code and unresolved problems).")
    with st.form(f"q_form_{i['id']}", border=True):
        st.markdown("**⚙️ Settings** (used when this task starts)")
        title = st.text_input("Title", i["title"], key=f"qe_t_{i['id']}")
        body = st.text_area("Description and acceptance criteria", i["body"], height=240, key=f"qe_d_{i['id']}")
        repo = repo_picker("Repository", f"qe_r_{i['id']}", value=i["repo"])
        c1, c2, c3, c4, c5 = st.columns(5)
        mode = c1.selectbox("Team", ["swarm", "reviewed", "single"], index=["swarm", "reviewed", "single"].index(i["mode"] or "swarm"),
                            key=f"qe_m_{i['id']}", format_func={"swarm": "Swarm", "reviewed": "One developer + test/review", "single": "Single agent"}.get)
        budget = c2.number_input("Budget (USD)", 0.5, 500.0, float(i["budget"] or cfg.limits.budget_usd), 0.5,
                                 key=f"qe_b_{i['id']}")
        cycles = c3.number_input("Max repair cycles", 0, 10, int(i["max_cycles"] if i["max_cycles"] is not None
                                                                 else cfg.limits.max_repair_cycles),
                                 key=f"qe_c_{i['id']}")
        hours = c4.number_input("Time limit (hours)", 0.05, 72.0, _hrs(i["max_runtime_s"] or cfg.limits.max_runtime_s),
                                0.5, key=f"qe_h_{i['id']}")
        approval = c5.selectbox("GitHub pushes", ["yes", "dashboard"],
                                index=["yes", "dashboard"].index(i["approval"] or "yes"), key=f"qe_a_{i['id']}",
                                format_func={"yes": "Draft PR without asking", "dashboard": "Ask me"}.get)
        h1, h2 = st.columns([1, 4])
        heal = heal_input(h1, f"qe_hl_{i['id']}", i.get("heal_attempts"))
        chain = h2.checkbox("Chain: start from the previous task's result on this repository", bool(i["chain"]),
                            key=f"qe_ch_{i['id']}")
        lane = queue_picker(st, f"qe_q_{i['id']}", i.get("queue") or "main")
        if st.form_submit_button("💾 Save changes", type="primary"):
            if not title.strip() or not body.strip() or not repo.strip():
                st.error("Title, description and repository can't be empty.")
            else:
                store.queue_update(i["id"], title=title.strip(), body=body.strip(), repo=repo.strip(), mode=mode,
                                   budget=float(budget), max_cycles=int(cycles), max_runtime_s=int(hours * 3600),
                                   approval=approval, chain=int(chain), heal_attempts=heal, queue=lane)
                st.session_state.flash = f"Saved “{title.strip()}”."
                st.rerun()


def live_settings(run: dict) -> None:
    """Change limits of a run that is going right now; the run picks them up within a few seconds."""
    rid = run["id"]
    lim = json.loads(run["config_json"] or "{}").get("limits", {})
    cur_t = run.get("max_runtime_s") or lim.get("max_runtime_s") or cfg.limits.max_runtime_s
    cur_c = run.get("max_repair_cycles")
    if cur_c is None:
        cur_c = lim.get("max_repair_cycles", cfg.limits.max_repair_cycles)
    elapsed = time.time() - run["created_at"]
    with st.form(f"live_{rid}", border=True):
        st.markdown("**⚙️ Settings for this run** — applied within a few seconds, no restart needed")
        c1, c2, c3 = st.columns(3)
        budget = c1.number_input("Budget (USD)", 0.1, 1000.0, float(run["budget_usd"] or cfg.limits.budget_usd),
                                 0.5, key=f"ls_b_{rid}", help=f"Spent so far: ${store.cost_summary(rid)['total']:.3f}")
        hours = c2.number_input("Time limit (hours)", 0.05, 72.0, _hrs(cur_t), 0.5, key=f"ls_h_{rid}",
                                help=f"Running for {fmt_dur(elapsed)} so far. The run stops when it reaches this.")
        cycles = c3.number_input("Max repair cycles", 0, 10, int(cur_c), key=f"ls_c_{rid}",
                                 help=f"Currently in repair cycle {run['repair_cycle']}.")
        if st.form_submit_button("Apply", type="primary"):
            if hours * 3600 <= elapsed:
                st.error(f"The run has already been going for {fmt_dur(elapsed)}; pick a longer time limit "
                         "(or use Cancel below).")
            else:
                store.update_run(rid, budget_usd=float(budget), max_runtime_s=int(hours * 3600),
                                 max_repair_cycles=int(cycles))
                store.record_intervention(rid, "settings", f"budget ${budget:.2f}, time limit {hours:g} h, "
                                                           f"max repair cycles {int(cycles)}")
                st.session_state.flash = "Settings sent to the run; it applies them within a few seconds."
                st.rerun()
    if not run["cancel_requested"]:
        if st.button("⏹ Cancel this run", key=f"cancel_{rid}"):
            request_run_cancel(store, rid)
            store.event(rid, "cancel", "cancel requested from dashboard", level="warn")
            st.rerun()
    else:
        st.caption("Cancel requested; the run stops at its next checkpoint.")


def queue_restart(i: dict) -> None:
    with st.container(border=True):
        st.markdown("**Try this task again** (it goes to the front of the waiting line)")
        r1, r2, r3 = st.columns(3)
        more = r3.number_input("Time limit (h)", 0.05, 72.0,
                               _hrs(max(i["max_runtime_s"] or cfg.limits.max_runtime_s, cfg.limits.max_runtime_s)),
                               0.5, key=f"q_h_{i['id']}")
        kw = dict(mode=i["mode"], budget=i["budget"], max_cycles=i["max_cycles"], chain=i["chain"],
                  approval=i["approval"], max_runtime_s=int(more * 3600), heal_attempts=i.get("heal_attempts"))
        if r1.button("↻ Start again", key=f"q_re_{i['id']}", help="Queue this task again from scratch."):
            new_id, created = _requeue_next(i, kw)
            st.session_state.flash = (f"Queued again as task #{new_id}; it starts when the task ahead of it finishes."
                                      if created else f"This task is already queued again as task #{new_id}.")
            st.rerun()
        can_continue = bool(i["run_id"]) and run_resumable(i["run_id"])
        if r2.button("⏩ Continue where it stopped", key=f"q_co_{i['id']}", disabled=not can_continue,
                     help="Queue it again starting from the failed run's code, with its unresolved problems "
                          "added to the task."):
            new_id, created = _requeue_next(i, {**kw, "seed_run_id": i["run_id"]})
            st.session_state.flash = (f"Queued to continue as task #{new_id}." if created
                                      else f"This task is already queued again as task #{new_id}.")
            st.rerun()


def open_run_button(label: str, run_id: str, key: str) -> None:
    if st.button(label, key=key):
        select_item(f"r:{run_id}")
        st.session_state.goto = "Work"
        st.rerun()


def diagnosis_box(r: dict, w: dict) -> None:
    """Self-healing lineage and the post-mortem of this run, if any."""
    from swarmforge.postmortem import CATEGORIES
    if r.get("heal_of"):
        prev = store.failure(r["heal_of"])
        with st.container(border=True):
            st.markdown(f"🩹 **Self-healing attempt {r['heal_attempt']}.** The previous attempt failed"
                        + (f" ({CATEGORIES.get(prev['category'], prev['category'])}): {prev['root_cause']}"
                           if prev else "."))
            st.caption("This attempt continues from that work with the diagnosis added to the task and more "
                       "time, budget, repair cycles and agent steps.")
            open_run_button("Open the previous attempt", r["heal_of"], f"prev_{r['id']}")
    f = store.failure(r["id"])
    if f:
        with st.container(border=True):
            st.markdown(f"🩺 **Why it failed** · {CATEGORIES.get(f['category'], f['category'])}"
                        + (f" · _{f['outcome']}_" if f["outcome"] else ""))
            st.write(f["root_cause"])
            st.markdown(f"**What to do differently:** {f['fix']}")
            if f["heal_run_id"]:
                open_run_button("Open the retry", f["heal_run_id"], f"next_{r['id']}")
    elif w["group"] == "failed" and cfg and r["state"] != "CANCELLED":
        if st.button("🩺 Diagnose why it failed", key=f"diag_{r['id']}",
                     help="Asks a model to explain the failure from the run's evidence (about a cent)."):
            diagnose_now(r["id"])
            st.rerun()


def diagnose_now(run_id: str) -> None:
    import asyncio
    from swarmforge.postmortem import diagnose
    with st.spinner("Diagnosing…"):
        try:
            asyncio.run(diagnose(cfg, store, run_id))
        except Exception as e:  # noqa: BLE001
            st.error(f"Diagnosis failed: {e}")


def item_detail(w: dict, every) -> None:
    i, r = w["item"], w["run"]
    bits = [w["status"]]
    if i:
        bits.append(f"queue task #{i['id']} in 🗂 {i.get('queue') or 'main'}")
    if r:
        bits.append(f"run `{r['id']}`")
    bits.append(f"repo `{w['repo']}`")
    st.caption(" · ".join(bits))
    if blocker := w.get("waiting_on"):
        st.info(f"Waiting for task #{blocker['id']} to leave this queue slot. {blocker['title']}")
    if i and i["status"] != "queued":
        extra = []
        if i["base_branch"]:
            extra.append(f"started from `{i['base_branch']}`")
        if i["result_branch"]:
            extra.append(f"result branch `{i['result_branch']}`")
        if extra:
            st.caption(" · ".join(extra))
    if r:
        diagnosis_box(r, w)
    if not cfg:
        st.warning("Configuration not loaded: settings can't be changed.")
    elif i and i.get("project_id"):
        st.info("The project lead owns this plan. Open Projects to review the spec or pause the project.")
        st.markdown(i["body"].split("Approved project spec", 1)[0])
    elif i and i["status"] == "queued":
        waiting_editor(i)
    elif r and w["group"] == "active":
        live_settings(r)
    elif i and i["status"] in ("failed", "cancelled", "skipped") and _restarted_as(i):
        st.info(f"Restarted as task #{_restarted_as(i)} — see it in the waiting line above.")
    elif i and i["status"] in ("failed", "cancelled", "skipped"):
        queue_restart(i)
    elif r and (r["state"] in FAILED_STATES or w["status"] == "Stopped unexpectedly"):
        restart_panel(r)
    if not r:
        if i and i["status"] != "queued":
            st.markdown(i["body"][:3000])
        return
    if r.get("archived"):
        if st.button("📂 Unarchive", key=f"unarch_{r['id']}",
                     help="Show this run in the Work list again."):
            store.set_archived(r["id"], False)
            st.rerun()
    elif st.button("📦 Archive", key=f"arch_{r['id']}",
                   help="Hide this run from the Work list. Its history, report and transcripts stay."):
        store.set_archived(r["id"], True)
        select_item(None)  # it leaves the list, so close it too
        st.rerun()
    free_disk_panel(r, w)
    run_detail(r["id"], header=False)


def free_disk_panel(r: dict, w: dict) -> None:
    """Reclaim the disk a finished run's clone and worktrees use.

    Only ``repo`` and ``worktrees`` are removed: the database row, the report,
    the transcripts and the logs stay. Active runs are never offered the action."""
    from swarmforge.db import free_run_disk

    rid = r["id"]
    if w["group"] == "active":
        st.caption("🧹 Free disk space is available once the run finishes.")
        return
    st.checkbox("Confirm: delete this run's repo and worktrees from disk", key=f"fd_ok_{rid}",
                help="Frees disk space by removing the clone and the worktrees. The run's history, "
                     "report, transcripts and logs are kept.")
    if st.button("🧹 Free disk space", key=f"fd_{rid}",
                 help="Deletes this run's repo and worktrees folders from the workdir."):
        if not st.session_state.get(f"fd_ok_{rid}"):
            st.warning("Tick the confirmation box first.")
            return
        rd = Path(r["run_dir"]) if r.get("run_dir") else (cfg.workdir / "runs" / rid if cfg else None)
        if rd is None:
            st.warning("This run has no work folder to clean up.")
            return
        freed = free_run_disk(rd)
        store.event(rid, "disk", f"freed {freed} bytes")
        _workdir_bytes.clear()  # the usage figure above must reflect the cleanup
        st.success(f"Freed {freed / 1e6:.1f} MB from disk.")


def welcome_panel(cfg, empty: bool = False) -> None:
    """First-run welcome: setup checklist, the sample run and how a run works.

    ``empty`` adds the Work page's "nothing here yet" hint; only a fresh database shows it.
    Checks and the sample run go through ``swarmforge.welcome`` module attributes so tests can
    monkeypatch them (AppTest runs the script in this process, but functions defined here could
    not be patched from outside)."""
    from swarmforge import welcome

    with st.container(key="sf-welcome-banner"):
        st.image(branding.asset_bytes("banner.png"), width="stretch")
    st.header("Welcome to SwarmForge")
    st.caption("SwarmForge turns an issue into a reviewed branch: plan it, build it in parallel, "
               "test it, review it, repair it, then prepare a draft pull request.")
    if empty:
        st.info("Nothing here yet. Go to **New task** to start a run or find an issue to fix.")
    st.subheader("Getting started")
    try:
        for item in welcome.health_checks(cfg):
            if item.ok:
                st.markdown(f"✅ {item.name}")
            else:
                st.markdown(f"{'⬜' if item.optional else '❌'} {item.name}"
                            f"{' (optional)' if item.optional else ''}")
                if item.fix:
                    st.code(item.fix, language=None)
    except Exception as e:  # noqa: BLE001 - a broken check must never blank the page
        st.caption(f"Setup checks unavailable: {e}")
    st.caption("Creates ~/swarmforge-sample and runs the bundled CSV/JSON export issue with 1 repair "
               "cycle and a $3 budget.")
    if st.button("🧪 Try the sample (about $0.40, 15 minutes)", type="primary", key="ws_sample"):
        if not cfg:
            st.error("Configuration not loaded; cannot start the sample run.")
            return
        try:
            with st.spinner("Preparing the sample…"):
                run_id = welcome.start_sample_run(cfg, store)
        except Exception as e:  # noqa: BLE001 - show it, keep the page alive
            st.error(f"Could not start the sample run: {e}")
            return
        st.session_state.flash = "Sample run started — watch it below."
        select_item(f"r:{run_id}")
        st.session_state.goto = "Work"
        st.rerun()
    st.subheader("How SwarmForge works")
    st.markdown("The planner breaks the issue down → two developers work **in parallel** on separate "
                "branches → the branches are merged → the tester runs the suite → the reviewer checks "
                "the result → blocking problems go back for a **repair cycle** → an approved change "
                "becomes a draft **PR**.")
    st.graphviz_chart("""digraph swarmforge {
  rankdir=LR;
  Issue -> Planner;
  Planner -> Backend;
  Planner -> Frontend;
  Backend -> Merge;
  Frontend -> Merge;
  Merge -> Tester;
  Tester -> Reviewer;
  Reviewer -> Planner [style=dashed, label="blocking problems (repair cycle)"];
  Reviewer -> PR [label="approved"];
}""")


@st.cache_data(ttl=60, show_spinner=False)
def _workdir_bytes(path: str) -> int:
    """Total size of the workdir in bytes; cached briefly because it can hold many clones."""
    from swarmforge.db import dir_size_bytes
    return dir_size_bytes(Path(path))


def page_work():
    from swarmforge.dashboard_pages.work import render
    return render(_workdir_bytes, auto, cfg, st, store, welcome_panel, work_items, work_list, collapse_attempts)


# ============================================================= FAILURES page
def page_failures():
    from swarmforge.dashboard_pages.failures import render
    return render(alt, cfg, diagnose_now, fmt_ts, open_run_button, pd, st, store)


# ================================================================= CHAT page
QUICK_PROMPTS = {
    "💡 What should we improve next?": "Looking at recent runs, failures, slowness and lessons learned, what are "
                                       "the most valuable improvements to SwarmForge right now? Suggest tasks.",
    "🩺 Review my failed tasks": "Go through the failed tasks that need my decision. For each, explain briefly "
                                "what went wrong and suggest ways to fix it.",
    "⚡ Why are runs slow or expensive?": "Where do runs spend their time and money, and what tasks would make "
                                         "them faster or cheaper?",
}
CHAT_KV = "chat_history_v1"
CHAT_MIGRATED = False  # legacy flat history is folded into 'General' once per process


def _chat_actions_key(conv_id: str) -> str:
    return f"chat_actions_v1:{conv_id}"


def _chat_rel_time(ts: float) -> str:
    d = time.time() - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    if d < 7 * 86400:
        return f"{int(d // 86400)} d ago"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _chat_conv_id() -> str:
    """The active conversation: the remembered one if it still exists, else the most recent."""
    from swarmforge import chatbot
    convs = chatbot.list_conversations(store)
    cid = st.session_state.get("chat_conv_id")
    if cid and any(c["id"] == cid for c in convs):
        return cid
    if convs:
        st.session_state.chat_conv_id = convs[0]["id"]
        return convs[0]["id"]
    cid = chatbot.ensure_default_conversation(store)["id"]
    st.session_state.chat_conv_id = cid
    return cid


def _chat_migrate_legacy() -> None:
    """Fold the pre-conversation flat chat_history_v1 kv into the seeded 'General' thread."""
    global CHAT_MIGRATED
    if CHAT_MIGRATED:
        return
    CHAT_MIGRATED = True
    from swarmforge import chatbot
    try:
        raw = store.kv_get(CHAT_KV)
    except Exception:  # noqa: BLE001
        return
    if not raw:
        return
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return
    conv = chatbot.ensure_default_conversation(store)
    for h in (d.get("history") or [])[-40:]:
        chatbot.add_message(store, conv["id"], h.get("role", "user"), h.get("content", ""))
    acts = {str(k): v for k, v in (d.get("actions") or {}).items()}
    if acts:
        store.kv_set(_chat_actions_key(conv["id"]), json.dumps(acts))
    store.kv_set(CHAT_KV, None)


def _chat_save(actions: dict, conv_id: str) -> None:
    """Persist the per-conversation action payloads (messages persist via add_message at emit time)."""
    try:
        store.kv_set(_chat_actions_key(conv_id), json.dumps({str(k): v for k, v in actions.items()}))
    except Exception:  # noqa: BLE001
        pass


def _chat_load(conv_id: str) -> tuple[list, dict]:
    """Session chat_history/chat_actions for the active conversation, loading it on switch."""
    from swarmforge import chatbot
    _chat_migrate_legacy()
    if st.session_state.get("chat_loaded_conv") != conv_id:
        def _row(m):
            d = {"role": m["role"], "content": m["content"]}
            if m.get("tool_calls"):
                d["tool_calls"] = m["tool_calls"]
            return d

        st.session_state.chat_history = [_row(m) for m in chatbot.get_messages(store, conv_id)]
        try:
            d = json.loads(store.kv_get(_chat_actions_key(conv_id)) or "{}")
        except json.JSONDecodeError:
            d = {}
        st.session_state.chat_actions = {int(k): v for k, v in (d or {}).items()}
        st.session_state.chat_loaded_conv = conv_id
        st.session_state.pop("chat_done", None)
    return st.session_state.chat_history, st.session_state.setdefault("chat_actions", {})


def _chat_note(key: str, msg: str, ok: bool = True) -> None:
    st.session_state.setdefault("chat_done", {})[key] = (ok, msg)


def render_task_actions(i: int, tasks: list[dict]) -> None:
    from swarmforge import chat_actions as CA
    done = st.session_state.setdefault("chat_done", {})
    with st.container(border=True):
        st.markdown(f"**💡 {len(tasks)} suggested task(s)** — tick the ones you want, then launch them in one go.")
        c1, c2, c3 = st.columns([2, 2, 1.6])
        repo = c1.text_input("Repository", value=tasks[0].get("repo") or CA.default_repo(store) or "",
                             key=f"ct_repo_{i}")
        how = c3.radio("How", ["queue", "now"], key=f"ct_how_{i}", horizontal=False,
                       format_func={"queue": "Add to a queue", "now": "Start each now"}.get)
        lane = queue_picker(c2, f"ct_q_{i}", st.session_state.get("last_queue") or "self-improvement")
        chosen = []
        for j, t in enumerate(tasks):
            key = f"t{i}_{j}"
            row = st.columns([0.35, 6])
            if key in done:
                row[0].markdown("✅" if done[key][0] else "⚠️")
                row[1].markdown(f"**{t['title']}** — {done[key][1]}")
                continue
            if row[0].checkbox("pick", value=True, key=f"ct_pick_{i}_{j}", label_visibility="collapsed"):
                chosen.append(j)
            row[1].markdown(f"**{t['title']}** · size {t.get('size', 'M')}")
            if t.get("why"):
                row[1].caption(t["why"])
            with row[1].expander("Description (edit before launching if you like)"):
                st.text_area("Description", t["description"], height=220, key=f"ct_desc_{i}_{j}",
                             label_visibility="collapsed")
        if chosen and st.button(f"🚀 Launch {len(chosen)} task(s)", type="primary", key=f"ct_go_{i}"):
            for j in chosen:
                t = {**tasks[j], "description": st.session_state.get(f"ct_desc_{i}_{j}", tasks[j]["description"])}
                try:
                    _chat_note(f"t{i}_{j}", CA.launch_task(cfg, store, t, how=how, repo=repo, queue=lane))
                except Exception as e:  # noqa: BLE001
                    _chat_note(f"t{i}_{j}", f"not launched: {e}", ok=False)
            st.session_state.last_queue = lane
            st.rerun()


def render_fix_actions(i: int, fixes: list[dict]) -> None:
    from swarmforge import chat_actions as CA
    from swarmforge.postmortem import resumable
    done = st.session_state.setdefault("chat_done", {})
    for j, fx in enumerate(fixes):
        run = store.get_run(fx["run_id"]) or {}
        key = f"f{i}_{j}"
        with st.container(border=True):
            st.markdown(f"**🩺 {run.get('issue_title') or fx['run_id']}**  \n`{fx['run_id']}` · {fx.get('summary', '')}")
            if key in done:
                (st.success if done[key][0] else st.error)(done[key][1])
                continue
            labels = [o["label"] for o in fx["options"]] + ["✍️ Write my own answer"]
            pick = st.radio("How should the next attempt fix it?", range(len(labels)), key=f"cf_pick_{i}_{j}",
                            format_func=lambda n: labels[n])
            opt = fx["options"][pick] if pick < len(fx["options"]) else {}
            text = st.text_area("Instructions for the next attempt (edit freely)", opt.get("instructions", ""),
                                height=110, key=f"cf_txt_{i}_{j}_{pick}",
                                placeholder="e.g. Only change dashboard.py; skip the slow tests; keep the API as is")
            a, b, c, d = st.columns(4)
            hours = a.number_input("Time limit (h)", 0.1, 72.0, float(opt.get("time_limit_h") or
                                   max(1.0, (run.get("max_runtime_s") or 0) / 3600 or 3.0)), 0.5,
                                   key=f"cf_h_{i}_{j}_{pick}")
            budget = b.number_input("Budget ($)", 0.5, 500.0, float(opt.get("budget_usd") or run.get("budget_usd") or 10),
                                    0.5, key=f"cf_b_{i}_{j}_{pick}")
            cycles = c.number_input("Repair cycles", 0, 10, int(opt.get("max_cycles") if opt.get("max_cycles") is not None
                                                             else (run.get("max_repair_cycles") or 2)),
                                    key=f"cf_c_{i}_{j}_{pick}")
            can_continue = resumable(store, fx["run_id"])
            cont = d.checkbox("Keep its code (continue)", value=bool(opt.get("continue_code")) and can_continue,
                              disabled=not can_continue, key=f"cf_k_{i}_{j}_{pick}",
                              help="Start from the code the failed run already wrote" if can_continue
                              else "Nothing reusable was integrated in that run")
            x, y, _ = st.columns([1.4, 1, 3])
            if x.button("🔁 Retry with this fix", type="primary", key=f"cf_go_{i}_{j}"):
                try:
                    _chat_note(key, CA.retry_failure(cfg, store, fx["run_id"], text, time_limit_h=hours, budget=budget,
                                                     max_cycles=int(cycles), continue_code=cont))
                except Exception as e:  # noqa: BLE001
                    _chat_note(key, f"not restarted: {e}", ok=False)
                st.rerun()
            if y.button("Dismiss", key=f"cf_no_{i}_{j}", help="Don't retry; stop suggesting it"):
                _chat_note(key, CA.dismiss_failure(store, fx["run_id"]))
                st.rerun()


def render_message_actions(i: int, actions: dict | None) -> None:
    if not actions:
        return
    if actions.get("fixes"):
        render_fix_actions(i, actions["fixes"])
    if actions.get("tasks"):
        render_task_actions(i, actions["tasks"])


def _chat_save_attachments(uploaded):
    """Save chat uploads under <workdir>/chat_attachments.

    Returns ``(metadata, paths)`` where metadata dicts carry name/kind/path for
    transcript rendering and paths feed ``chatbot.ask(attachments=...)``.
    """
    if not uploaded:
        return [], []
    try:
        from swarmforge.attachments import classify, save_attachments
        paths = save_attachments(uploaded, cfg.workdir / "chat_attachments")
    except Exception as e:  # never break the Chat page on upload problems
        st.error(f"Could not save attachments: {e}")
        return [], []
    meta = [{"name": p.name, "kind": classify(p), "path": str(p)} for p in paths]
    return meta, paths


def _render_chat_attachments(atts):
    """Render attached files: thumbnails for images, name chips otherwise."""
    for att in atts or []:
        try:
            if att.get("kind") == "image" and Path(att.get("path", "")).is_file():
                st.image(att["path"], width=180, caption=att.get("name", ""))
            else:
                st.markdown(f"`:blue[{att.get('name', 'file')}]`")
        except Exception:
            continue


_UNESCAPED_DOLLAR = re.compile(r"(?<!\\)\$")


def _chat_md(text: str) -> str:
    """Escape bare $ so Streamlit markdown doesn't read "$0.15 ... $0.80" as a LaTeX formula."""
    return _UNESCAPED_DOLLAR.sub(r"\\$", text or "")


def _chat_rail(active_id: str, chatbot) -> dict:
    """Claude-style conversation list. Returns which rail control (if any) was clicked this pass."""
    clicked = dict.fromkeys(("new", "sel", "edit", "del", "save", "cancel"))
    with st.container(key="chat_rail"):
        clicked["new"] = st.button("New chat", icon=":material/add:", key="chat_new", width="stretch")
        st.markdown('<div class="sf-rail-label">Recents</div>', unsafe_allow_html=True)
        renaming = st.session_state.get("chat_renaming")
        for c in chatbot.list_conversations(store):
            cid = c["id"]
            if renaming == cid:
                with st.container(key=f"chat_rename_row_{cid}", horizontal=True, vertical_alignment="center",
                                  gap="xxsmall"):
                    st.text_input("Rename", value=c["title"], key=f"chat_rename_{cid}",
                                  label_visibility="collapsed")
                    if st.button("", icon=":material/check:", key=f"chat_rename_save_{cid}", type="tertiary",
                                 help="Save"):
                        clicked["save"] = cid
                    if st.button("", icon=":material/close:", key=f"chat_rename_cancel_{cid}", type="tertiary",
                                 help="Cancel"):
                        clicked["cancel"] = cid
                continue
            active = cid == active_id
            with st.container(key=f"chat_row_{cid}", horizontal=True, vertical_alignment="center", gap=None):
                if st.button(c["title"], key=f"chat_sel_{cid}", type="secondary" if active else "tertiary"):
                    clicked["sel"] = cid
                st.caption(_chat_rel_time(c["updated_at"]))
                if st.button("", icon=":material/edit:", key=f"chat_edit_{cid}", type="tertiary", help="Rename"):
                    clicked["edit"] = cid
                if st.button("", icon=":material/delete:", key=f"chat_del_{cid}", type="tertiary", help="Delete"):
                    clicked["del"] = cid
    return clicked


def page_chat():
    from swarmforge.dashboard_pages.chat import render
    return render(CHAT_AVATARS, CHAT_SCROLL_JS, QUICK_PROMPTS, _chat_actions_key, _chat_conv_id, _chat_load, _chat_md, _chat_rail, _chat_save, _chat_save_attachments, _render_chat_attachments, cfg, components, dataclasses, json, render_message_actions, st, store, uuid)


# ============================================================ NEW TASK page
def queue_forms() -> None:
    from swarmforge import runqueue as rq
    st.caption("Each queue (pile) runs its tasks one after another; different queues run side by side, so use "
               "one queue per project. With **chaining**, each task starts from the previous "
               "task's result on the same repository, so improvements build on each other. Nothing is ever "
               "merged into your main branch.")

    def common(prefix):
        c1, c2, c3, c5, c4 = st.columns(5)
        mode = c1.selectbox("Team", ["swarm", "reviewed", "single"], key=f"{prefix}_mode",
                            format_func=lambda m: {"swarm": "Swarm", "reviewed": "One developer + test/review", "single": "Single agent"}[m])
        budget = c2.number_input("Budget per task (USD)", 0.5, 500.0, 5.0, 0.5, key=f"{prefix}_budget")
        cycles = c3.number_input("Max repair cycles", 0, 10, 2, key=f"{prefix}_cycles")
        hours = c5.number_input("Time limit per task (h)", 0.05, 72.0, _hrs(cfg.limits.max_runtime_s),
                                0.5, key=f"{prefix}_hours")
        approval = c4.selectbox("GitHub pushes", ["yes", "dashboard"], key=f"{prefix}_appr",
                                format_func=lambda a: {"yes": "Draft PRs without asking",
                                                       "dashboard": "Ask me each time"}[a])
        h1, h2 = st.columns([1, 4])
        heal = heal_input(h1, f"{prefix}_heal")
        chain = h2.checkbox("Chain: start from the previous task's result on this repository", value=True,
                            key=f"{prefix}_chain")
        lane = queue_picker(st, f"{prefix}_queue")
        return mode, budget, cycles, approval, chain, int(hours * 3600), heal, lane

    def checked_repo(repo):
        from swarmforge.repos import normalize_repo, preflight
        from swarmforge.github import get_token
        return preflight(normalize_repo(repo), get_token(cfg.github)).source

    t_add, t_imp = st.tabs(["➕ One task", "📥 Import a backlog file"])
    with t_add:
        title = st.text_input("Title", key="qa_title")
        body = st.text_area("Description with acceptance criteria", height=200, key="qa_body")
        repo = repo_picker("Repository", "qa_repo")
        mode, budget, cycles, approval, chain, runtime, heal, lane = common("qa")
        if st.button("Add to queue", type="primary"):
            try:
                if not title.strip() or not body.strip():
                    raise ValueError("title and description are required")
                src = checked_repo(repo)
                new_id = store.queue_add(title.strip(), body.strip(), src, mode=mode, budget=budget,
                                         max_cycles=int(cycles), chain=int(chain), approval=approval,
                                         max_runtime_s=runtime, heal_attempts=heal, queue=lane)
                store.remember_repo(src)
                st.session_state.last_queue = lane
                st.session_state.flash = f"Queued in “{lane}”: {title.strip()}. Press ▶ Start queue when you're ready."
                select_item(f"q:{new_id}")
                st.session_state.goto = "Work"
                st.rerun()
            except Exception as e:
                st.error(str(e))

    with t_imp:
        default = str((cfg.source_path.parent / "docs" / "SELF_IMPROVEMENT_BACKLOG.md")) if cfg.source_path else ""
        path = st.text_input("Backlog file (Markdown with **Title:** / **Description:** sections)", value=default,
                             key="qi_path")
        repo_i = repo_picker("Repository for all imported tasks", "qi_repo")
        mode, budget, cycles, approval, chain, runtime, heal, lane = common("qi")
        found = []
        if path and Path(path).is_file():
            found = rq.parse_backlog(Path(path).read_text(encoding="utf-8"))
            st.caption(f"{len(found)} task(s) found: " + "; ".join(f.title for f in found))
        elif path:
            st.warning("File not found.")
        if st.button("Import into queue", disabled=not found):
            try:
                src = checked_repo(repo_i)
                for f in found:
                    store.queue_add(f.title, f.body, src, mode=mode, budget=budget, max_cycles=int(cycles),
                                    chain=int(chain), approval=approval, max_runtime_s=runtime,
                                    heal_attempts=heal, queue=lane)
                store.remember_repo(src)
                st.session_state.last_queue = lane
                st.session_state.flash = f"Imported {len(found)} task(s). Press ▶ Start queue when you're ready."
                select_item(None)
                st.session_state.goto = "Work"
                st.rerun()
            except Exception as e:
                st.error(str(e))


def page_new_task():
    from swarmforge.dashboard_pages.new_task import render
    return render(cfg, page_new_run, queue_forms, st)


# =========================================================== BENCHMARKS page
def page_speed():
    from swarmforge.dashboard_pages.speed import render
    return render(st, store, pd, alt)


def page_benchmarks():
    from swarmforge.dashboard_pages.benchmarks import render
    return render(st, store, pd)


# ======================================================== PULL REQUESTS page
def _am_latest(rows: list[dict]) -> dict[str, tuple[dict, dict]]:
    """Latest auto-merge event per PR url -> (row, detail); rows are newest first."""
    out: dict[str, tuple[dict, dict]] = {}
    for r in rows:
        try:
            d = json.loads(r["detail"] or "{}")
        except json.JSONDecodeError:
            continue
        if d.get("url") and d["url"] not in out:
            out[d["url"]] = (r, d)
    return out


def auto_merge_panel(gh, open_urls: set[str] | None = None) -> None:
    """What auto-merge did, and buttons for everything that waits for you."""
    from swarmforge import automerge as am
    from swarmforge.github import GitHubError
    amc = cfg.auto_merge
    kinds = (am.MERGED, am.HELD, am.FIXING, am.CLOSED, am.MANUAL, "auto_merge_reverted")
    rows = store._all(f"SELECT * FROM interventions WHERE kind IN ({','.join('?' * len(kinds))}) "
                      "ORDER BY id DESC LIMIT 60", kinds)
    latest = _am_latest(rows)
    gone = {u for u, (r, _) in latest.items()
            if r["kind"] == am.HELD and open_urls is not None and u not in open_urls}
    waiting = [u for u, (r, _) in latest.items() if r["kind"] == am.HELD and u not in gone]
    where = ", ".join(amc.repos) if amc.repos else "every repository"
    title = (f"🤖 Auto-merge: **on** for {where}" + (f" · **{len(waiting)} waiting for you**" if waiting else "")
             if amc.enabled else "🤖 Auto-merge: **off** (set `auto_merge: {enabled: true}` in swarmforge.yaml)")
    with st.expander(title, expanded=bool(waiting)):
        st.caption(f"Up to {amc.max_lines} clean lines merge at once. Larger changes, open [major] findings and "
                   f"core files (protected_paths) go to the {amc.judge_role} model; core files need a low-risk "
                   "verdict. Conflicts are resolved against the latest main and re-tested. A PR that is still held "
                   + ("gets one automatic fix-up task that replaces it; after that it waits here."
                      if amc.auto_fix else "waits here.")
                   + " Nothing restarts by itself.")
        flash = st.session_state.pop("am_flash", None)
        if flash:
            (st.success if flash[0] else st.error)(flash[1])
        if not latest:
            st.caption("No auto-merge decisions yet.")
        label = {am.MERGED: "✅ merged", am.MANUAL: "✅ merged by you", am.HELD: "⏸ waiting for you",
                 am.FIXING: "🔧 being fixed", am.CLOSED: "🗑 closed", "auto_merge_reverted": "↩️ reverted"}
        for url, (r, d) in list(latest.items())[:25]:
            kind = r["kind"]
            if url in gone:
                st.markdown(f"**✔ no longer open** (merged or closed on GitHub) · [{d.get('title') or url}]({url})")
                continue
            extra = (f" (task #{d['fix_task']})" if kind == am.FIXING and d.get("fix_task") else "") + \
                    (f" (run `{d['fix_run']}`)" if kind == am.FIXING and d.get("fix_run") else "") + \
                    (f" → {d['replaced_by']}" if d.get("replaced_by") else "")
            st.markdown(f"**{label.get(kind, kind)}**{extra} · [{d.get('title') or url}]({url}) · {fmt_ts(r['ts'])}"
                        + (" · 🧑‍⚖️ judged" if d.get("judged") else "")
                        + (f"  \n{d['reason']}" if d.get("reason") else ""))
            key = f"am_{r['id']}"
            if kind == am.HELD and d.get("number"):
                c1, c2, c3, _ = st.columns([1.2, 1.6, 1, 3])
                if c1.button("Merge now", key=key + "_merge", type="primary",
                             help="Merge it as it is (marks a draft ready first)"):
                    try:
                        pr = am.get_pr(gh, d["repo"], int(d["number"]))
                        sha = am.apply(gh, pr, amc.method)
                        store.record_intervention(r["run_id"], am.MANUAL, json.dumps({**d, "sha": sha,
                                                                                     "reason": "merged by you"}))
                        st.session_state.am_flash = (True, f"Merged {url}. Update & restart to pick it up.")
                    except (GitHubError, Exception) as e:  # noqa: BLE001
                        st.session_state.am_flash = (False, f"Could not merge {url}: {e}. "
                                                            "Use **Fix with the swarm** to have it updated.")
                    st.session_state.pop("pr_cache", None)
                    st.rerun()
                if c2.button("Fix with the swarm", key=key + "_fix",
                             help="Start a run right now that continues this PR's code, fixes why it was held "
                                  "(and any conflicts with main), then replaces this PR"):
                    run = store.get_run(r["run_id"]) or {}
                    item = store._one("SELECT queue FROM queue_items WHERE run_id = ?", (r["run_id"],)) or {}
                    findings = [f for f in store.findings(r["run_id"])
                                if f["cycle"] == run.get("repair_cycle") and f["severity"] != "info"]
                    body = run.get("issue_body") or d.get("title") or ""
                    if am.fix_depth(body)[0] and "supersedes=" in body:   # allow one more manual round
                        body = am.FIX_MARK.sub("", body)
                    args = dict(title=run.get("issue_title") or d.get("title") or "", body=body, repo=d["repo"],
                                url=url, reason=d.get("reason", ""), findings=findings)
                    try:
                        rid = am.start_fix(cfg, r["run_id"], **args)                 # starts right now
                        store.record_intervention(r["run_id"], am.FIXING, json.dumps({**d, "fix_run": rid}))
                        st.session_state.am_flash = (True, f"Started fix-up run {rid} now; its PR will replace {url}.")
                    except Exception as e:  # noqa: BLE001 - fall back to the queue
                        qid = am.queue_fix(store, r["run_id"], queue=item.get("queue"), **args)
                        store.record_intervention(r["run_id"], am.FIXING, json.dumps({**d, "fix_task": qid}))
                        from swarmforge import runqueue as rq
                        if not rq.worker_pid(store):
                            rq.start_worker(cfg, store)
                        st.session_state.am_flash = (True, f"Couldn't start it right away ({e}); queued fix-up "
                                                           f"task #{qid} at the front instead.")
                    st.rerun()
                if c3.button("Close", key=key + "_close", help="Close the PR without merging"):
                    try:
                        am.close_pr(gh, d["repo"], int(d["number"]), "Closed from the SwarmForge dashboard.")
                        store.record_intervention(r["run_id"], am.CLOSED, json.dumps({**d, "reason": "closed by you"}))
                        st.session_state.am_flash = (True, f"Closed {url}.")
                    except (GitHubError, Exception) as e:  # noqa: BLE001
                        st.session_state.am_flash = (False, f"Could not close {url}: {e}")
                    st.session_state.pop("pr_cache", None)
                    st.rerun()
            elif kind in (am.MERGED, am.MANUAL) and d.get("number"):
                if st.session_state.get(key + "_ask"):
                    if st.button("Confirm revert", key=key + "_yes", type="primary"):
                        try:
                            rev = am.revert(gh, d["repo"], int(d["number"]), amc.method)
                            store.record_intervention(r["run_id"], "auto_merge_reverted",
                                                      json.dumps({**d, "revert_url": rev}))
                            st.session_state.am_flash = (True, f"Reverted: {rev} is merged. Update & restart to "
                                                               "pick up the change.")
                        except (GitHubError, Exception) as e:  # noqa: BLE001
                            st.session_state.am_flash = (False, f"Could not revert {url}: {e}")
                        st.session_state.pop(key + "_ask", None)
                        st.session_state.pop("pr_cache", None)
                        st.rerun()
                elif st.button("↩ Revert", key=key + "_rev", help="GitHub creates a revert PR and it is merged at once"):
                    st.session_state[key + "_ask"] = True
                    st.rerun()


def page_prs():
    from swarmforge.dashboard_pages.prs import render
    return render(auto_merge_panel, cfg, do_update_and_restart, open_run_button, st, store, time)


# =========================================================== SETTINGS page
def page_settings():
    from swarmforge.dashboard_pages.settings import render
    return render(cfg, st)


def page_projects():
    from swarmforge.dashboard_pages.projects import render
    render(cfg, st, store)


def page_history():
    from swarmforge.dashboard_pages.history import render
    render(st, store)


{"Work": page_work, "Projects": page_projects, "What's new": page_history, "Pull requests": page_prs, "New task": page_new_task, "Failures": page_failures, "Speed": page_speed, "Chat": page_chat,
 "Knowledge": page_knowledge, "Benchmarks": page_benchmarks, "Settings": page_settings}[page]()
