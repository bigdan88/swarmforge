"""SwarmForge control center (Streamlit). Launch with `swarmforge dashboard`.

Start runs, approve pushes, watch delegation and every agent's conversation live, browse and edit
the Zettelkasten knowledge base, and compare benchmarks. Runs execute as separate background
processes; the dashboard only reads the SQLite store / vault and writes explicit user actions
(new run, cancel, approval decision, note edits).
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from swarmforge.config import ConfigError, load_config
from swarmforge.db import Store
from swarmforge.knowledge import NOTE_TYPES, Vault, WIKILINK_RE

# --------------------------------------------------------------------- theme
# Streamlit cannot change its base theme mid-session, so `swarmforge dashboard --theme` launches the
# subprocess with a base theme and exports SWARMFORGE_DASH_THEME; the sidebar control below injects
# the other palette as CSS on top of it and persists the user's choice under THEME_KEY in the kv store.
THEME_KEY = "dash_theme"
THEME_CHOICES = ("auto", "light", "dark")
LAUNCH_THEME = os.environ.get("SWARMFORGE_DASH_THEME", "auto")  # set by `swarmforge dashboard --theme`

DARK_CSS = """<style>
/* Dark palette over Streamlit's base theme. Selectors target data-testids (stable across releases),
   not internal CSS variable names, which shift between Streamlit versions. */
.stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
    background-color: #0e1117; color: #fafafa; }
[data-testid="stSidebar"] { background-color: #16181d; }
[data-testid="stSidebar"] p, [data-testid="stSidebar"] li, [data-testid="stSidebar"] span,
[data-testid="stSidebar"] label, [data-testid="stSidebar"] a { color: #fafafa; }
h1, h2, h3, h4, h5, h6, p, li, label, span { color: #fafafa; }
[data-testid="stTextInput"] input, [data-testid="stNumberInput"] input,
[data-testid="stTextArea"] textarea, [role="listbox"], [role="listbox"] * {
    background-color: #262730; color: #fafafa; border-color: #444; }
[data-testid="stCode"], pre, code { background-color: #1a1c22; color: #e6e6e6; }
[data-testid="stVerticalBlockBorderWrapper"], [data-testid="stExpander"],
[data-testid="stExpander"] details, [data-testid="stExpander"] summary, [data-testid="stTabs"] {
    background-color: #16181d; color: #fafafa; border-color: #33343d; }
[data-testid="stButton"] button { color: #fafafa; }
[data-testid="stButton"] button[kind="secondary"], [data-testid="stBaseButton-secondary"] {
    background-color: #262730; color: #fafafa; }
[data-testid="stButton"] button[kind="primary"], [data-testid="stBaseButton-primary"] {
    background-color: #ff4b4b; color: #ffffff; }
a, [data-testid="stMarkdownContainer"] a { color: #ff4b4b; }
/* status boxes keep their own light backgrounds when the base theme is light: dark text there */
[data-testid="stAlert"] p, [data-testid="stAlert"] li, [data-testid="stAlert"] span,
[data-testid="stNotification"] p { color: inherit; }
</style>"""

LIGHT_CSS = """<style>
/* Native light palette pinned, so switching back from a dark launch base works too. */
.stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
    background-color: #ffffff; color: #31333f; }
[data-testid="stSidebar"] { background-color: #f0f2f6; }
[data-testid="stSidebar"] p, [data-testid="stSidebar"] li, [data-testid="stSidebar"] span,
[data-testid="stSidebar"] label, [data-testid="stSidebar"] a { color: #31333f; }
h1, h2, h3, h4, h5, h6, p, li, label, span { color: #31333f; }
[data-testid="stTextInput"] input, [data-testid="stNumberInput"] input,
[data-testid="stTextArea"] textarea, [role="listbox"], [role="listbox"] * {
    background-color: #f8f9fa; color: #31333f; border-color: #d5d8e0; }
[data-testid="stCode"], pre, code { background-color: #f0f2f6; color: #31333f; }
[data-testid="stVerticalBlockBorderWrapper"], [data-testid="stExpander"],
[data-testid="stExpander"] details, [data-testid="stExpander"] summary, [data-testid="stTabs"] {
    background-color: #ffffff; color: #31333f; border-color: #d5d8e0; }
[data-testid="stButton"] button { color: #31333f; }
[data-testid="stButton"] button[kind="secondary"], [data-testid="stBaseButton-secondary"] {
    background-color: #f0f2f6; color: #31333f; }
[data-testid="stButton"] button[kind="primary"], [data-testid="stBaseButton-primary"] {
    background-color: #ff4b4b; color: #ffffff; }
a, [data-testid="stMarkdownContainer"] a { color: #ff4b4b; }
</style>"""


def theme_css(mode: str) -> str:
    """CSS override for an explicit mode; "auto" follows the Streamlit base theme (no override)."""
    if mode == "dark":
        return DARK_CSS
    if mode == "light":
        return LIGHT_CSS
    return ""


def effective_theme(sel: str, launch: str) -> str:
    """The palette actually shown: an explicit choice wins, "auto" follows what the CLI launched."""
    return "dark" if sel == "dark" or (sel == "auto" and launch == "dark") else "light"


def apply_chart_theme(mode: str) -> None:
    """Match the Altair chart theme to the palette; an unknown theme name must never break the page."""
    name = "dark" if mode == "dark" else "default"
    try:
        if hasattr(alt, "theme") and hasattr(alt.theme, "enable"):   # Altair >= 5.5
            alt.theme.enable(name)
        else:                                                       # older Altair
            alt.themes.enable(name if name == "dark" else "vox")
    except Exception:  # noqa: BLE001 - chart styling is decoration
        pass


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

st.set_page_config(page_title="SwarmForge", page_icon="🐝", layout="wide")


def _password_gate() -> None:
    """When SWARMFORGE_DASH_PASSWORD is set (required for `swarmforge dashboard --lan`), ask for it once
    per browser session before showing anything."""
    import hmac
    pw = os.environ.get("SWARMFORGE_DASH_PASSWORD")
    if not pw or st.session_state.get("_authed"):
        return
    st.title("🐝 SwarmForge")
    given = st.text_input("Password", type="password", key="_pw")
    if given:
        if hmac.compare_digest(given.encode(), pw.encode()):
            st.session_state._authed = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()


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

# theme: the persisted choice wins, else what `swarmforge dashboard --theme` launched with
if "theme_mode" not in st.session_state:
    saved = store.kv_get(THEME_KEY)
    st.session_state["theme_mode"] = saved if saved in THEME_CHOICES else (
        LAUNCH_THEME if LAUNCH_THEME in THEME_CHOICES else "auto")
theme_mode = st.session_state.get("theme_mode") or LAUNCH_THEME
apply_chart_theme(effective_theme(theme_mode, LAUNCH_THEME))
css = theme_css(theme_mode)
if css:  # injected before the sidebar so it covers every page and every rerun
    st.markdown(css, unsafe_allow_html=True)


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
PAGES = ["Work", "Pull requests", "New task", "Failures", "Chat", "Knowledge", "Benchmarks", "Settings"]
NAV_ICON = {"Work": ":material/view_list:", "New task": ":material/add_circle:", "Failures": ":material/healing:",
            "Chat": ":material/chat_bubble:", "Knowledge": ":material/hub:", "Benchmarks": ":material/leaderboard:"}
NAV_ICON["Settings"] = ":material/settings:"
NAV_ICON["Pull requests"] = ":material/merge:"
SLUG = {p: p.lower().replace(" ", "-") for p in PAGES}
if st.session_state.get("page") not in PAGES:  # first load: honour ?page=... so pages can be bookmarked
    st.session_state.page = {v: k for k, v in SLUG.items()}.get(st.query_params.get("page", ""), "Work")
if "goto" in st.session_state:  # navigation requested by a button on the previous script run
    st.session_state.page = st.session_state.pop("goto")
page = st.session_state.page
pending_all = store.pending_approvals()


def _theme_changed() -> None:
    """Persist the sidebar Theme choice so it survives restarts and other browser sessions."""
    value = st.session_state.get("theme_mode")
    if value in THEME_CHOICES and value != store.kv_get(THEME_KEY):
        store.kv_set(THEME_KEY, value)
    st.rerun(scope="app")


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
    background: rgba(255, 75, 75, 0.12); color: #ff4b4b; font-weight: 600; }}
section[data-testid="stSidebar"] [class*="st-key-nav_"] {{ margin-bottom: -0.6rem; }}
.sf-badge {{ font-size: 0.75rem; opacity: 0.7; }}
</style>""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown("### 🐝 SwarmForge")
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
    st.segmented_control("Theme", list(THEME_CHOICES), key="theme_mode",
                         default=LAUNCH_THEME if "theme_mode" not in st.session_state else None,
                         on_change=_theme_changed, help="Dark mode for the whole dashboard")
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
    if not cfg:
        st.error("Configuration not loaded; cannot start runs.")
        return
    from swarmforge.launcher import start_run
    sample_issue = Path(__file__).resolve().parents[2] / "samples" / "issues" / "export-csv-json.md"
    sample_repo = Path("~/swarmforge-sample").expanduser()
    with st.expander("🧪 Try the bundled sample (inventory app, CSV/JSON export issue)"):
        st.write(f"Creates a small git repository at `{sample_repo}` and fills the form below.")
        if st.button("Prepare sample"):
            if not sample_repo.exists():
                import subprocess
                import sys
                subprocess.run([sys.executable, str(sample_issue.parents[1] / "make_sample_repo.py"), str(sample_repo)],
                               check=False, capture_output=True)
            if sample_issue.is_file():
                text = sample_issue.read_text(encoding="utf-8")
                m = re.match(r"#\s+(.+)\n", text)
                st.session_state.nr_source = "Describe it here"
                st.session_state.nr_title = m.group(1) if m else "Sample issue"
                st.session_state.nr_body = text[m.end():].strip() if m else text
                store.remember_repo(str(sample_repo))
                st.session_state.nr_repo = str(sample_repo)
                st.rerun()

    source = st.radio("What should the team work on?", ["Describe it here", "GitHub issue"],
                      key="nr_source", horizontal=True)
    url = title = body = None
    if source == "GitHub issue":
        url = st.text_input("Issue URL", placeholder="https://github.com/owner/repo/issues/42", key="nr_url")
        repo = st.text_input("Repository override (optional)", key="nr_repo_gh",
                             help="Leave empty to clone the issue's repository.")
    else:
        title = st.text_input("Title", key="nr_title", placeholder="Add CSV export to the reports page")
        body = st.text_area("Description, requirements and acceptance criteria", height=260, key="nr_body",
                            placeholder="Describe what you want built. Be specific about interfaces and "
                                        "acceptance criteria; the planner turns this into a task graph.")
        repo = repo_picker("Repository", "nr_repo")

    c1, c2, c3, c4, c5 = st.columns(5)
    mode = c1.selectbox("Team", ["swarm", "single"], format_func=lambda m: {
        "swarm": "Swarm (5 roles)", "single": "Single agent (baseline)"}[m])
    budget = c2.number_input("Budget (USD)", 0.5, 500.0, float(cfg.limits.budget_usd), 0.5, key="nr_budget")
    cycles = c3.number_input("Max repair cycles", 0, 10, int(cfg.limits.max_repair_cycles), key="nr_cycles")
    hours = c4.number_input("Time limit (hours)", 0.05, 72.0, _hrs(cfg.limits.max_runtime_s), 0.5,
                            help="The whole run stops after this long. Default from swarmforge.yaml "
                                 "(limits.max_runtime_s).")
    approval = c5.selectbox("Before pushing to GitHub", ["dashboard", "yes"], format_func=lambda a: {
        "dashboard": "Ask me here", "yes": "Pre-approve"}[a])
    heal = heal_input(st.columns(5)[0], "nr_heal")

    with st.expander("Advanced: test command and sandbox image"):
        st.caption("Leave blank to use the repository's `swarmforge.project.yml` (if it has one), "
                   f"otherwise your defaults: `{cfg.project.test_command}` in `{cfg.sandbox.image}`.")
        test_cmd = st.text_input("Test command", key="nr_test_cmd")
        image = st.text_input("Sandbox image (must already be built)", key="nr_image",
                              placeholder="e.g. swarmforge-dev-sandbox:latest")

    with st.expander("Team roster for this run"):
        roster = [{"role": r, "model": cfg.roles[r].model} for r in
                  ["planner", *cfg.developer_roles, "tester", "reviewer", cfg.librarian_role] if r in cfg.roles]
        st.dataframe(pd.DataFrame(roster).drop_duplicates(), hide_index=True, width="stretch")
        st.caption("Change models in swarmforge.yaml (then restart the dashboard).")

    if vault and (body or title):
        found = vault.search(f"{title or ''}\n{body or ''}", k=cfg.knowledge.max_notes_in_prompt)
        if found:
            with st.expander(f"📚 {len(found)} knowledge note(s) the team will start from"):
                for sc, n in found:
                    st.markdown(f"- **{n.title}** · _{n.type}_ · {n.repo or ''} (score {sc})")

    if st.button("🚀 Start run", type="primary"):
        from swarmforge.github import get_token, parse_issue_url
        from swarmforge.repos import RepoError, normalize_repo, preflight
        try:
            base = None
            repo_arg = None
            if source == "GitHub issue":
                if not (url or "").strip():
                    raise ValueError("paste the GitHub issue URL (https://github.com/owner/repo/issues/N)")
                try:
                    parse_issue_url(url.strip())
                except Exception:
                    raise ValueError(f"'{url.strip()}' is not a GitHub issue URL. To work on a repository "
                                     "without an issue, choose 'Describe it here' and put the repository URL "
                                     "in the Repository field.")
                url = url.strip()
            elif not (title or "").strip() or not (body or "").strip():
                raise ValueError("give the run a title and a description")
            from swarmforge.preflight import environment_problems
            with st.spinner("Checking Docker, keys and the repository…"):
                problems = environment_problems(cfg, (image or "").strip() or None)
            if problems:
                raise ValueError("\n\n".join(problems))
            if repo and repo.strip():
                with st.spinner("Checking the repository…"):
                    ref = preflight(normalize_repo(repo), get_token(cfg.github))
                repo_arg, base = ref.source, ref.branch
                store.remember_repo(repo.strip() if ref.branch else ref.source)
                if ref.source != repo.strip():
                    st.info(f"Using {ref.source}" + (f" (branch {ref.branch})" if ref.branch else ""))
            elif source != "GitHub issue":
                raise ValueError("give a repository: a local folder, owner/repo, or a GitHub URL")
            run_id, log = start_run(cfg, repo=repo_arg, issue_url=url, issue_title=title, issue_body=body,
                                    mode=mode, budget=budget, max_cycles=int(cycles), approval=approval,
                                    test_command=(test_cmd or "").strip() or None,
                                    sandbox_image=(image or "").strip() or None, base=base,
                                    max_runtime_s=int(hours * 3600), heal_attempts=heal)
        except (ValueError, RepoError) as e:
            st.error(str(e))
            return
        select_item(f"r:{run_id}")
        st.session_state.goto = "Work"
        time.sleep(1.5)  # give the process a moment to register the run
        st.rerun()


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

    tabs = st.tabs(["🧭 Delegation", "🤖 Agents & timeline", "💬 Transcripts", "🧪 Tests", "🔍 Review",
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
        st.markdown("#### Human interventions")
        if inter:
            st.dataframe(pd.DataFrame([{"time": fmt_ts(i["ts"]), "kind": i["kind"], "detail": i["detail"]} for i in inter]),
                         width="stretch", hide_index=True)
        else:
            st.caption("None.")

    with tabs[5]:
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

    with tabs[6]:
        st.markdown(f"Provider-reported: **${cost['measured']:.4f}** · estimated: **${cost['estimated']:.4f}** · "
                    f"calls without cost info: **{cost['unavailable_calls'] or 0}**")
        br = pd.DataFrame(store.cost_by_role(run_id))
        if not br.empty:
            st.dataframe(br, width="stretch", hide_index=True)
            st.altair_chart(alt.Chart(br).mark_bar().encode(
                x=alt.X("cost_usd:Q", title="USD"), y=alt.Y("role:N", sort="-x"), tooltip=list(br.columns)),
                width="stretch")
        calls = store.llm_calls(run_id)
        if calls:
            cdf = pd.DataFrame(calls)
            cdf["t"] = pd.to_datetime(cdf["ended_at"], unit="s")
            cdf["cumulative_cost"] = cdf["cost_usd"].fillna(0).cumsum()
            st.altair_chart(alt.Chart(cdf).mark_line(point=True).encode(
                x=alt.X("t:T", title=None), y=alt.Y("cumulative_cost:Q", title="cumulative USD")), width="stretch")

    with tabs[7]:
        log = cfg.workdir / "logs" / f"{run_id}.log" if cfg else None
        if log and log.is_file():
            st.code(log.read_text(encoding="utf-8", errors="replace")[-20000:], language=None)
        else:
            st.caption("No console log (this run was started from the command line).")

    with tabs[8]:
        kinds = sorted({e["kind"] for e in events})
        pick = st.multiselect("Kinds", kinds, default=kinds)
        rows = [{"time": fmt_ts(e["ts"]), "level": e["level"], "kind": e["kind"], "task": e["task_key"] or "",
                 "role": e["role"] or "", "message": e["message"]} for e in events if e["kind"] in pick]
        st.dataframe(pd.DataFrame(rows[::-1]), width="stretch", hide_index=True, height=600)


# ============================================================ KNOWLEDGE page
def page_knowledge():
    st.header("📚 Knowledge base (Zettelkasten)")
    if not vault:
        st.error("Configuration not loaded.")
        return
    st.caption(f"Vault: `{vault.root}` — plain Markdown with [[wikilinks]]; open the same folder in Obsidian if you like. "
               "Agents read relevant notes before planning/coding/reviewing and a librarian writes new ones after each run.")
    notes = vault.notes()
    repos = sorted({n.repo for n in notes if n.repo})
    c1, c2, c3 = st.columns([2, 2, 3])
    repo = c1.selectbox("Repository", ["(all)"] + repos)
    types = c2.multiselect("Types", list(NOTE_TYPES), default=[t for t in NOTE_TYPES if t != "run-log"])
    q = c3.text_input("Search", placeholder="keywords…")
    sel = [n for n in notes if (repo == "(all)" or n.repo == repo) and n.type in types]
    if q:
        ranked = vault.search(q, repo=None if repo == "(all)" else repo, k=200, exclude_types=())
        keep = {n.stem for _, n in ranked}
        sel = [n for _, n in ranked if n.stem in {x.stem for x in sel}] if keep else []
    tl, tg, tn = st.tabs(["Notes", "Graph", "➕ New note"])
    with tl:
        if not sel:
            st.info("No notes match. After your first run the librarian will add some, or write one yourself.")
        else:
            left, right = st.columns([1, 2])
            with left:
                labels = {n.stem: f"[{n.type}] {n.title}" for n in sel}
                stem = st.radio("Notes", list(labels), format_func=labels.get, label_visibility="collapsed",
                                key="kn_sel")
            with right:
                n = next(x for x in sel if x.stem == stem)
                st.markdown(f"### {n.title}")
                st.caption(f"{n.type} · repo {n.repo or '-'} · source {n.source} · created {n.created} · "
                           f"tags {', '.join(n.tags) or '-'} · file `{n.stem}.md`")
                st.markdown(wikilinks_to_md(n.body))
                back = vault.backlinks(n.stem, notes)
                if back:
                    st.markdown("**Linked from:** " + ", ".join(f"_{b.title}_" for b in back))
                with st.expander("✏️ Edit"):
                    with st.form(f"edit_{n.stem}"):
                        title = st.text_input("Title", n.title)
                        typ = st.selectbox("Type", list(NOTE_TYPES), index=list(NOTE_TYPES).index(n.type) if n.type in NOTE_TYPES else 0)
                        tags = st.text_input("Tags (comma separated)", ", ".join(n.tags))
                        body = st.text_area("Body (Markdown, [[wikilinks]] allowed)", n.body, height=300)
                        if st.form_submit_button("Save"):
                            n.title, n.type, n.body = title, typ, body
                            n.tags = [t.strip() for t in tags.split(",") if t.strip()]
                            n.source = n.source if n.source.startswith("user") else f"{n.source} (edited by user)"
                            vault.save(n)
                            st.success("Saved")
                            st.rerun()
                    if st.checkbox("I want to delete this note", key=f"delc_{n.stem}"):
                        if st.button("🗑 Delete note", key=f"del_{n.stem}"):
                            vault.delete(n.stem)
                            st.rerun()
    with tg:
        shown, edges = vault.graph(sel)
        if len(shown) > 150:
            st.caption(f"Showing the 150 most recent of {len(shown)} notes.")
            shown = sorted(shown, key=lambda n: n.stem)[-150:]
        stems = {n.stem for n in shown}
        dot = ['graph K { layout=neato; overlap=false; splines=true; node [shape=box style="rounded,filled" '
               'fontname=Helvetica fontsize=9 fontcolor=white];']
        for n in shown:
            dot.append(f'"{_esc(n.stem)}" [label="{_esc(n.title[:40])}" fillcolor="{TYPE_COLORS.get(n.type, "#777")}"];')
        for a, b in edges:
            if a in stems and b in stems:
                dot.append(f'"{_esc(a)}" -- "{_esc(b)}";')
        dot.append("}")
        if shown:
            st.graphviz_chart("\n".join(dot), width="stretch")
            st.caption(" · ".join(f"{t}" for t in TYPE_COLORS))
        else:
            st.caption("Nothing to draw yet.")
    with tn:
        with st.form("new_note", clear_on_submit=True):
            title = st.text_input("Title (state the single idea as a claim)")
            c1, c2 = st.columns(2)
            typ = c1.selectbox("Type", ["user-note", "decision", "pitfall", "pattern", "repo-fact", "review-lesson"])
            nrepo = c2.text_input("Repository (e.g. owner/repo or folder name; blank = applies everywhere)")
            tags = st.text_input("Tags (comma separated)")
            body = st.text_area("Body (Markdown; link with [[note title or file name]])", height=220)
            if st.form_submit_button("Create note"):
                if not title.strip() or not body.strip():
                    st.error("Title and body are required.")
                else:
                    vault.write(title, body, type=typ, repo=nrepo.strip() or None,
                                tags=[t.strip() for t in tags.split(",") if t.strip()], source="user")
                    st.success("Note created")


# ================================================================= WORK page
QSTATUS = {"queued": "⏳", "running": "🟢", "done": "✅", "failed": "❌", "cancelled": "⏹️", "skipped": "⏭️"}
FAILED_STATES = {"FAILED", "BLOCKED", "CANCELLED", "BUDGET_EXCEEDED"}
FILTERS = {"All": None, "Active": {"active"}, "Waiting": {"waiting"}, "Finished": {"done"},
           "Failed": {"failed"}}


def _short_repo(repo: str) -> str:
    repo = (repo or "").rstrip("/\\")
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
        return "done", "✅", f"Done (fixed itself, attempt {r['heal_attempt']})"
    if r["state"] == "COMPLETED":
        return "done", "✅", "Done"
    return "failed", STATE_ICON.get(r["state"], "❌"), r["state"].replace("_", " ").title()


def work_items() -> list[dict]:
    """Every queue task and every run as one list: active first, then waiting (queue order), then newest."""
    runs = store.list_runs(300)
    by_id = {r["id"]: r for r in runs}
    healed = {r["heal_of"] for r in runs if r.get("heal_of")}  # earlier attempts: shown under Failures
    approvals = {a["run_id"] for a in store.pending_approvals()}
    out, used = [], set()
    queue = store.queue_items()
    from swarmforge.runqueue import lane as _lane
    waiting_pos: dict[int, int] = {}
    per_lane: dict[str, int] = {}
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
                    "repo": i["repo"], "item": i, "run": r,
                    "when": (r["created_at"] if r else i.get("created_at")) or 0,
                    "order": waiting_pos.get(i["id"], 0), "hidden": False,
                    "archived": bool(r.get("archived")) if r else False})
    for r in runs:
        if r["id"] in used:
            continue
        grp, icon, label = _run_status(r, approvals)
        out.append({"key": f"r:{r['id']}", "group": grp, "icon": icon, "status": label,
                    "title": r["issue_title"] or r["issue_url"] or r["id"], "repo": r["repo"] or "",
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
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([2.2, 1.2, 1.2, 2])
        if pid:
            c1.markdown(f"**Queue:** 🟢 working (pid {pid})" + (" — pauses after the current task" if paused else ""))
        else:
            c1.markdown("**Queue:** ⚪ stopped" + (" (paused)" if paused and queued else "")
                        + (f" · {len(queued)} waiting" if queued else ""))
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
        policy = store.kv_get(rq.POLICY_KEY) or "stop"
        new_policy = c4.selectbox("If a task fails", ["stop", "continue"], index=["stop", "continue"].index(policy),
                                  key="qb_policy", format_func=lambda p: {"stop": "If a task fails: pause its queue",
                                                         "continue": "If a task fails: continue"}[p],
                                  label_visibility="collapsed")
        if new_policy != policy:
            store.kv_set(rq.POLICY_KEY, new_policy)
        done = sum(1 for i in items if i["status"] == "done")
        st.progress(done / len(items), text=f"{done} of {len(items)} queue tasks done · {len(running)} running "
                                             f"· {len(queued)} waiting")
        lanes = [n for n in store.queue_names() if any(rq.lane(i) == n for i in items)]
        if len(lanes) > 1 or any(rq.lane_paused(store, n) for n in lanes):
            limit = cfg.limits.max_parallel_queues if cfg else 3
            st.caption(f"Queues run side by side (up to {limit} at once); each runs its own tasks in order.")
            for n in lanes:
                mine = [i for i in items if rq.lane(i) == n]
                run_ = [i for i in mine if i["status"] == "running"]
                wait_ = [i for i in mine if i["status"] == "queued"]
                lp = rq.lane_paused(store, n)
                q1, q2 = st.columns([5, 1.3])
                q1.markdown(f"🗂 **{n}** — " + ("⏸ paused · " if lp else "")
                            + (f"running: {run_[0]['title'][:60]}" if run_ else "idle")
                            + (f" · {len(wait_)} waiting" if wait_ else ""))
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


ROW_COLS = [0.32, 0.32, 4.4, 2.0, 2.8, 2.0, 0.85, 0.95]
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
[class*="st-key-wr_"]:has([class*="st-key-ck_"] input:checked) { background: rgba(255, 75, 75, 0.08); }
[class*="st-key-ck_"], [class*="st-key-ckall"] { position: relative !important; z-index: 2; }
[class*="st-key-wr_"] p { margin: 0; font-size: 0.9rem; white-space: nowrap; overflow: hidden;
                          text-overflow: ellipsis; }
.st-key-whead p { font-size: 0.78rem; opacity: 0.65; margin: 0; }
[class*="st-key-wopen_"] { border-left: 3px solid #ff4b4b; padding-left: 0.9rem; margin: 0.2rem 0 1rem 0.4rem; }
[class*="st-key-wsel_"] { background: rgba(255, 75, 75, 0.08) !important; }
</style>"""


def _cells(w: dict) -> dict:
    r, i = w["run"], w["item"]
    now = ""
    if w["group"] == "active" and r and (r["state_reason"] or "").startswith("waiting for the repository"):
        now = r["state_reason"][:120]
    elif w["group"] == "active" and r:
        now = r["state"].replace("_", " ").title() + (f" · repair cycle {r['repair_cycle']}" if r["repair_cycle"] else "")
    elif r and r["state_reason"]:
        now = r["state_reason"][:120]
    elif i and i["note"]:
        now = i["note"][:120]
    cost = store.cost_summary(r["id"])["total"] if r else None
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
        if w["group"] == "active":
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
                store.request_cancel(w["run"]["id"])
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
                done, skipped = _delete_items(sel, files)
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


def work_row(w: dict) -> None:
    k = _wkey(w["key"])
    is_open = st.session_state.get("sel_item") == w["key"]
    cells = _cells(w)
    with st.container(key=f"wr_{k}"):
        c = st.columns(ROW_COLS, vertical_alignment="center", gap="small")
        c[0].checkbox("select", key=f"ck_{k}", label_visibility="collapsed")
        c[1].markdown(w["icon"])
        c[2].markdown(f"{'▾' if is_open else '▸'} {'📦 ' if w.get('archived') else ''}"
                      f"**{w['title'] or '(untitled)'}**")
        c[3].markdown(w["status"])
        c[4].markdown(cells["now"] or " ")
        c[5].markdown(_short_repo(w["repo"]))
        c[6].markdown(cells["cost"] or " ")
        c[7].markdown(cells["time"] or " ")
        if c[7].button("open", key=f"op_{k}", help="Show / hide details"):
            select_item(None if is_open else w["key"])
            st.rerun(scope="app")


def _item_mode(w: dict) -> str | None:
    """The mode of a work item: the run's when there is one, else the queue task's."""
    if w["run"]:
        return w["run"].get("mode")
    return w["item"].get("mode") if w["item"] else None


def work_list(items: list[dict], every) -> None:
    queue_bar()
    visible = [w for w in items if not w["hidden"]]
    if not visible:
        st.info("Nothing here yet. Go to **New task** to start a run or fill the queue.")
        return
    counts = {g: sum(1 for w in visible if w["group"] == g) for g in ("active", "waiting", "done", "failed")}
    f1, f2 = st.columns([3, 2])
    labels = {"All": f"All ({len(visible)})", "Active": f"Active ({counts['active']})",
              "Waiting": f"Waiting ({counts['waiting']})", "Finished": f"Finished ({counts['done']})",
              "Failed": f"Failed ({counts['failed']})"}
    filt = f1.segmented_control("Show", list(FILTERS), key="w_filter", format_func=labels.get,
                                label_visibility="collapsed", default="All") or "All"
    query = f2.text_input("Search", key="w_search", placeholder="🔎 Search title, repo or run id",
                          label_visibility="collapsed").strip().lower()
    # filters above the table; options come from the full list so they stay stable while narrowing it
    g1, g2, g3, g4 = st.columns([1.5, 1.5, 1.1, 1.1])
    states = g1.multiselect("State", sorted({w["run"]["state"] for w in items if w["run"]}),
                            key="w_state", placeholder="All states")
    repo_sel = g2.selectbox("Repository", ["All repos"] + sorted({w["repo"] for w in items if w["repo"]}),
                            key="w_repo")
    mode_sel = g3.selectbox("Mode", ["All modes", "swarm", "single"], key="w_mode")
    show_arch = g4.checkbox("Show archived", key="w_arch",
                            help="Archived runs stay in the database but are hidden from this list.")
    base = visible if show_arch else [w for w in visible if not w["archived"]]
    shown = [w for w in base if FILTERS[filt] is None or w["group"] in FILTERS[filt]]
    if states:
        shown = [w for w in shown if w["run"] and w["run"]["state"] in states]
    if repo_sel and repo_sel != "All repos":
        shown = [w for w in shown if w["repo"] == repo_sel]
    if mode_sel and mode_sel != "All modes":
        shown = [w for w in shown if _item_mode(w) == mode_sel]
    if query:
        shown = [w for w in shown
                 if query in f"{w['title']} {w['repo']} {w['run']['id'] if w['run'] else ''}".lower()]
    if not shown:
        st.caption("No tasks match.")
        return
    bulk_bar(shown)
    st.markdown(WORK_CSS, unsafe_allow_html=True)
    keys = [_wkey(w["key"]) for w in shown]
    with st.container(key="whead"):
        h = st.columns(ROW_COLS, vertical_alignment="center", gap="small")
        h[0].checkbox("all", key="ckall", label_visibility="collapsed", on_change=_toggle_all, args=(keys,),
                      help="Select all shown")
        for col, name in zip(h[2:], ["TASK", "STATUS", "NOW / RESULT", "REPO", "COST", "TIME"]):
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
    for w in shown:
        work_row(w)
        if sel == w["key"]:
            with st.container(key=f"wopen_{_wkey(w['key'])}"):
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
        mode = c1.selectbox("Team", ["swarm", "single"], index=["swarm", "single"].index(i["mode"] or "swarm"),
                            key=f"qe_m_{i['id']}", format_func={"swarm": "Swarm", "single": "Single agent"}.get)
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
            store.request_cancel(rid)
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
    from swarmforge import runqueue as rq
    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))
    h1, h2 = st.columns([4, 1], vertical_alignment="bottom")
    h1.header("Work")
    if h2.button("New task", icon=":material/add:", type="primary", width="stretch"):
        st.session_state.goto = "New task"
        st.rerun()
    if cfg is not None:
        st.caption(f"💽 Workdir usage: {_workdir_bytes(str(cfg.workdir)) / 1e6:.1f} MB · `{cfg.workdir}`")
    items = work_items()
    show = st.session_state.pop("show_welcome", False)
    if not items or show:  # first run (or "Getting started" in the sidebar): welcome instead of an empty table
        welcome_panel(cfg, empty=not items)
        if items:
            st.divider()
    if not items:
        return
    busy = any(w["group"] == "active" for w in items) or bool(rq.worker_pid(store))
    open_w = next((w for w in items if w["key"] == st.session_state.get("sel_item")), None)
    editing = bool(open_w and (open_w["group"] != "active" or st.session_state.get("confirm_delete")))
    # refresh live while something runs, but not while you're editing an open waiting/finished task
    every = 3 if auto and busy and not editing else None

    @st.fragment(run_every=every)
    def listing():
        work_list(work_items(), every)
    listing()


# ============================================================= FAILURES page
def page_failures():
    from swarmforge.postmortem import CATEGORIES
    st.header("Failures")
    st.caption("Every run that failed, why, and what happened next. SwarmForge diagnoses each failure "
               "automatically and, if a retry can fix it, tries again with more effort. The same post-mortems "
               "are in your knowledge vault (note **Failures**) so agents learn from them, and in each run's "
               "`postmortem.md`. CLI: `swarmforge failures`.")
    fails = store.failures()
    recorded = {f["run_id"] for f in fails}
    undiagnosed = [r for r in store.list_runs(300) if r["id"] not in recorded
                   and r["state"] in ("FAILED", "BLOCKED", "BUDGET_EXCEEDED")]
    if undiagnosed and cfg:
        c1, c2 = st.columns([3, 1])
        c1.info(f"{len(undiagnosed)} failed run(s) from before automatic diagnosis have no post-mortem yet.")
        if c2.button(f"🩺 Diagnose {len(undiagnosed)} run(s)", key="diag_all"):
            for r in undiagnosed:
                diagnose_now(r["id"])
            st.rerun()
    if not fails:
        st.success("No failures recorded.")
        return
    fixed = sum(1 for f in fails if (f["outcome"] or "").startswith("fixed itself"))
    needs = sum(1 for f in fails if (f["outcome"] or "").startswith(("needs you", "still failing", "not retried")))
    cats = pd.Series([f["category"] for f in fails]).value_counts()
    m = st.columns(3)
    m[0].metric("Failures", len(fails))
    m[1].metric("Fixed themselves", fixed)
    m[2].metric("Need you", needs)
    chart = pd.DataFrame({"cause": [CATEGORIES.get(c, c) for c in cats.index], "failures": cats.values})
    st.altair_chart(alt.Chart(chart).mark_bar(color="#e45756").encode(
        x=alt.X("failures:Q", title=None, axis=alt.Axis(tickMinStep=1)), y=alt.Y("cause:N", sort="-x", title=None),
        tooltip=["cause", "failures"]).properties(height=max(60, 28 * len(chart))), width="stretch")
    pick = st.multiselect("Cause", list(cats.index), format_func=lambda c: CATEGORIES.get(c, c), key="f_cat",
                          placeholder="All causes")
    shown = [f for f in fails if not pick or f["category"] in pick]
    df = pd.DataFrame([{"When": fmt_ts(f["created_at"], True), "Task": (f["title"] or "")[:70],
                        "Cause": CATEGORIES.get(f["category"], f["category"]),
                        "Why": (f["root_cause"] or "")[:140], "What happened next": f["outcome"] or "",
                        "Cost $": round(f["cost_usd"] or 0, 3)} for f in shown])
    tkey = f"ft_{abs(hash(tuple(f['run_id'] for f in shown))) % 10**8}"
    ev = st.dataframe(df, hide_index=True, width="stretch", height=min(420, 38 + 35 * len(shown)),
                      on_select="rerun", selection_mode="single-row", key=tkey,
                      column_config={"Why": st.column_config.TextColumn(width="large"),
                                     "Cost $": st.column_config.NumberColumn(format="$%.3f")})
    rows = list(ev.selection.rows) if ev is not None and hasattr(ev, "selection") else []
    if rows:
        st.session_state.sel_failure = shown[rows[0]]["run_id"]
    sel = st.session_state.get("sel_failure")
    f = next((x for x in shown if x["run_id"] == sel), shown[0] if shown else None)
    if not f:
        return
    st.divider()
    st.subheader(f["title"] or f["run_id"])
    st.caption(f"run `{f['run_id']}` · repo `{f['repo'] or '-'}` · {f['state']}: {(f['reason'] or '')[:200]} · "
               f"diagnosed by {f['diagnosed_by']}")
    st.markdown(f"**Cause:** {CATEGORIES.get(f['category'], f['category'])}"
                + (f"  ·  **What happened next:** {f['outcome']}" if f["outcome"] else ""))
    st.markdown("#### Why it failed")
    st.write(f["root_cause"])
    st.markdown("#### What to do differently")
    st.write(f["fix"])
    if f["lesson"]:
        st.markdown("#### Lesson")
        st.write(f["lesson"])
    with st.expander("Evidence the diagnosis was based on"):
        st.code(f["evidence"] or "", language=None)
    b = st.columns(3)
    with b[0]:
        open_run_button("Open this run", f["run_id"], f"fo_{f['run_id']}")
    if f["heal_run_id"]:
        with b[1]:
            open_run_button("Open the retry", f["heal_run_id"], f"fh_{f['run_id']}")
    if cfg and b[2].button("↻ Diagnose again", key=f"fd_{f['run_id']}"):
        diagnose_now(f["run_id"])
        st.rerun()


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


def _chat_save(history: list, actions: dict) -> None:
    try:
        store.kv_set(CHAT_KV, json.dumps({"history": history[-40:], "actions": {
            str(k): v for k, v in actions.items() if int(k) >= len(history) - 40}}))
    except Exception:  # noqa: BLE001
        pass


def _chat_load() -> tuple[list, dict]:
    if "chat_history" not in st.session_state:
        try:
            d = json.loads(store.kv_get(CHAT_KV) or "{}")
        except json.JSONDecodeError:
            d = {}
        st.session_state.chat_history = list(d.get("history") or [])
        st.session_state.chat_actions = {int(k): v for k, v in (d.get("actions") or {}).items()}
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


def page_chat():
    if cfg is None:
        st.error("Configuration not loaded; the chatbot needs a configured model role.")
        return
    from swarmforge import chatbot
    st.header("Chat: improve SwarmForge and fix failed tasks")
    st.caption("Ask about runs and statuses, ask what to improve, or review failed tasks. Suggestions come with "
               "buttons: launch tasks in one click, or pick (or write) how a failed task should be fixed. "
               "Answers use the run store, failures, lessons in the knowledge base and the platform's own code "
               "layout. CLI: `swarmforge ask \"…\"`.")
    history, actions = _chat_load()
    needs = chatbot.failures_needing_attention(store)
    q1, q2, q3, q4 = st.columns([1.4, 1.4, 1.5, 0.8])
    pending = None
    for col, key, (label, prompt_text) in zip((q1, q2, q3), ("improve", "failures", "slow"), QUICK_PROMPTS.items()):
        lab = label + (f" ({len(needs)})" if key == "failures" and needs else "")
        if col.button(lab, key="chat_q_" + key, width="stretch"):
            pending = prompt_text
    if q4.button("Reset conversation", key="chat_reset"):
        history.clear()
        actions.clear()
        st.session_state.pop("chat_done", None)
        _chat_save(history, actions)
    if needs and not history:
        st.info(f"🩺 {len(needs)} failed task(s) are waiting for your decision. Press **Review my failed tasks**.")
    for n, m in enumerate(history):
        with st.chat_message(m["role"]):
            if m["role"] == "assistant":
                st.markdown(m["content"])
                render_message_actions(n, actions.get(n))
            else:
                st.write(m["content"])
    typed = st.chat_input("Ask about runs, failures, or what to improve…")
    prompt = typed or pending
    if not prompt:
        return
    history.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.write(prompt)
    try:
        with st.spinner("Thinking…"):
            res = chatbot.ask(cfg, store, prompt, history=history[:-1])
    except chatbot.ChatError as e:
        st.error(f"Chatbot: {e}")
        _chat_save(history, actions)
        return
    history.append({"role": "assistant", "content": res["answer"]})
    if res.get("actions") and (res["actions"].get("tasks") or res["actions"].get("fixes")):
        actions[len(history) - 1] = res["actions"]
    _chat_save(history, actions)
    with st.chat_message("assistant"):
        st.markdown(res["answer"])
        st.caption(f"answered by **{res['role']}** · `{res['model']}`")
        render_message_actions(len(history) - 1, actions.get(len(history) - 1))


# ============================================================ NEW TASK page
def queue_forms() -> None:
    from swarmforge import runqueue as rq
    st.caption("Each queue (pile) runs its tasks one after another; different queues run side by side, so use "
               "one queue per project. With **chaining**, each task starts from the previous "
               "task's result on the same repository, so improvements build on each other. Nothing is ever "
               "merged into your main branch.")

    def common(prefix):
        c1, c2, c3, c5, c4 = st.columns(5)
        mode = c1.selectbox("Team", ["swarm", "single"], key=f"{prefix}_mode",
                            format_func=lambda m: {"swarm": "Swarm", "single": "Single agent"}[m])
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
    st.header("New task")
    if not cfg:
        st.error("Configuration not loaded; cannot start runs.")
        return
    how = st.radio("How should it run?", ["Start now", "Add to the queue"], horizontal=True, key="nt_how",
                   captions=["Runs right away in the background.",
                             "Waits its turn in a queue; queues for different projects run side by side."])
    st.divider()
    if how == "Start now":
        page_new_run()
    else:
        queue_forms()


# =========================================================== BENCHMARKS page
def page_benchmarks():
    st.header("Benchmarks: single agent vs swarm")
    b = store.benchmarks()
    if not b:
        st.info("No benchmarks yet. Run `swarmforge bench ISSUE --repo PATH --acceptance DIR`.")
    for row in b:
        st.subheader(f"{row['label']} — {row['issue']}")
        df = pd.DataFrame(row["results"]).set_index("mode").T
        st.dataframe(df.astype(str), width="stretch")


# ======================================================== PULL REQUESTS page
def page_prs():
    from swarmforge import prs as P
    from swarmforge import selfupdate as su
    from swarmforge.github import GitHubClient, GitHubError
    st.header("Pull requests")
    if not cfg:
        st.error("Configuration not loaded.")
        return
    gh = GitHubClient(cfg.github)
    if not gh.token:
        st.warning(f"No GitHub token: add `{cfg.github.token_env}=...` to `.env` (or run `gh auth login`) and "
                   "restart the dashboard. Merging needs Contents + Pull requests: write.")
    repos = P.known_repos(cfg, store)
    if not repos:
        st.info("No GitHub repositories yet: pull requests appear here once a run opens one.")
        return
    c1, c2, c3 = st.columns([3, 2, 1])
    chosen = c1.multiselect("Repositories", repos, default=repos, key="pr_repos")
    method = c2.selectbox("How to merge", ["squash", "merge", "rebase"], key="pr_method",
                          format_func={"squash": "Squash (one commit per PR)", "merge": "Merge commit",
                                       "rebase": "Rebase"}.get)
    if c3.button("🔄 Refresh", key="pr_refresh"):
        st.session_state.pop("pr_cache", None)
    delete = st.checkbox("Delete the swarmforge/* branch after merging", value=True, key="pr_delete")
    cache = st.session_state.get("pr_cache")
    if not cache or time.time() - cache["t"] > 60 or cache["repos"] != chosen:
        data, errors = {}, {}
        with st.spinner("Loading pull requests from GitHub…"):
            for repo in chosen:
                try:
                    data[repo] = P.list_prs(gh, repo, store)
                except (GitHubError, Exception) as e:  # noqa: BLE001
                    errors[repo] = str(e)
        cache = {"t": time.time(), "repos": chosen, "data": data, "errors": errors}
        st.session_state.pr_cache = cache
    for repo, err in cache["errors"].items():
        st.warning(f"{repo}: {err}")
    all_prs = [pr for prs in cache["data"].values() for pr in prs]
    flash = st.session_state.pop("pr_flash", None)
    if flash:
        for ok, msg in flash["results"]:
            (st.success if ok else st.error)(msg)
        own = su.own_repo()
        if own and any(ok and own.lower() in msg.lower() for ok, msg in flash["results"]):
            with st.container(border=True):
                st.markdown("**You merged changes to SwarmForge itself.** Update now to start using them.")
                if st.button("⬆️ Update & restart SwarmForge now", type="primary", key="pr_update"):
                    st.session_state.upd_status = None
                    do_update_and_restart()
    if not all_prs:
        st.success("No open pull requests. 🎉")
        return
    ready = [pr for pr in all_prs if pr.ready]
    with st.container(border=True):
        a, b = st.columns([3, 2])
        a.markdown(f"**{len(all_prs)} open** · **{len(ready)} ready to merge** · "
                   f"{sum(1 for p in all_prs if p.state == 'dirty')} with conflicts")
        sure = a.checkbox("I've reviewed these changes", key="pr_sure",
                          help="Merging changes the main branch on GitHub. Draft PRs are marked ready first.")
        if b.button(f"Merge all ready ({len(ready)})", type="primary", disabled=not ready or not sure,
                    key="pr_merge_all", width="stretch"):
            with st.spinner("Merging, oldest first…"):
                res = P.merge_all(gh, ready, method, delete)
            st.session_state.pr_flash = {"results": [
                (ok, f"{pr.repo} #{pr.number} {pr.title}: {msg}") for pr, ok, msg in res]}
            st.session_state.pop("pr_cache", None)
            st.session_state.upd_status = None
            st.rerun()
    for repo, prs in cache["data"].items():
        if not prs:
            continue
        st.subheader(repo)
        for pr in prs:
            icon, text = pr.label
            with st.container(border=True):
                h1, h2 = st.columns([5, 2])
                h1.markdown(f"**[#{pr.number} {pr.title}]({pr.url})**  \n"
                            f"`{pr.head}` → `{pr.base}` · by {pr.author} · {pr.created_at[:10]} · "
                            f"+{pr.additions} −{pr.deletions} in {pr.changed_files} file(s)")
                h2.markdown(f"{icon} {text}")
                b1, b2, b3 = st.columns([1.2, 1.4, 3])
                if b1.button("Merge", key=f"pr_m_{repo}_{pr.number}", type="primary", disabled=not pr.ready or not sure,
                             help="Tick “I've reviewed these changes” above first." if not sure else None):
                    try:
                        P.merge(gh, pr, method, delete)
                        st.session_state.pr_flash = {"results": [(True, f"{repo} #{pr.number} {pr.title}: merged")]}
                    except GitHubError as e:
                        st.session_state.pr_flash = {"results": [(False, str(e))]}
                    st.session_state.pop("pr_cache", None)
                    st.session_state.upd_status = None
                    st.rerun()
                if pr.state == "behind" and b2.button("↻ Update branch", key=f"pr_u_{repo}_{pr.number}",
                                                      help="Merge the latest main into this PR (no conflicts)."):
                    try:
                        P.update_branch(gh, pr)
                        st.session_state.pr_flash = {"results": [(True, f"#{pr.number}: branch updated")]}
                    except GitHubError as e:
                        st.session_state.pr_flash = {"results": [(False, str(e))]}
                    st.session_state.pop("pr_cache", None)
                    st.rerun()
                if pr.state == "dirty":
                    b2.caption("Resolve on GitHub, or restart its task so it rebuilds on the latest main.")
                if pr.run_id:
                    with b3.expander("What the swarm did"):
                        try:
                            from swarmforge.report import build_summary
                            st.markdown(build_summary(store, pr.run_id).split("### What you need to do")[0])
                        except Exception as e:  # noqa: BLE001
                            st.caption(f"summary unavailable: {e}")
                        open_run_button("Open the run", pr.run_id, key=f"pr_o_{repo}_{pr.number}")


# =========================================================== SETTINGS page
def page_settings():
    st.caption("Edit swarmforge.yaml, run health checks and test models.")
    from swarmforge import settings
    settings.render(cfg)


{"Work": page_work, "Pull requests": page_prs, "New task": page_new_task, "Failures": page_failures, "Chat": page_chat,
 "Knowledge": page_knowledge, "Benchmarks": page_benchmarks, "Settings": page_settings}[page]()
