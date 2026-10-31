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

TERMINAL = {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "BUDGET_EXCEEDED"}
STATE_COLORS = {"PENDING": "#d0d4dc", "RUNNING": "#4c8bf5", "SUCCEEDED": "#7cc47f", "INTEGRATED": "#2e9e5b",
                "FAILED": "#e05252", "BLOCKED": "#f0a35e", "CANCELLED": "#999999"}
STATE_ICON = {"COMPLETED": "✅", "FAILED": "❌", "BLOCKED": "⛔", "CANCELLED": "⏹️", "BUDGET_EXCEEDED": "💸"}
FLOW = ["ISSUE_RECEIVED", "REPOSITORY_INSPECTION", "PLANNING", "TASK_VALIDATION", "TASK_EXECUTION",
        "INTEGRATION", "TESTING", "CODE_REVIEW", "APPROVED", "PULL_REQUEST_CREATION", "COMPLETED"]
PALETTE = ["#4c78a8", "#f58518", "#54a24b", "#b279a2", "#e45756", "#72b7b2", "#eeca3b", "#9d755d"]
TYPE_COLORS = {"decision": "#4c78a8", "pitfall": "#e45756", "pattern": "#54a24b", "repo-fact": "#72b7b2",
               "review-lesson": "#f58518", "model-behavior": "#b279a2", "run-log": "#bab0ac",
               "hub": "#333333", "user-note": "#eeca3b"}
ROLE_ICON = {"planner": "🧭", "reviewer": "🔍", "tester": "🧪", "librarian": "📚"}

st.set_page_config(page_title="SwarmForge", page_icon="🐝", layout="wide")


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


def pid_alive(pid):
    from swarmforge.launcher import pid_alive as _alive
    return _alive(pid)


def wikilinks_to_md(text: str) -> str:
    return WIKILINK_RE.sub(lambda m: f"🔗 **{(m.group(2) or m.group(1)).strip()}**", text)


# --------------------------------------------------------------------- sidebar
if "page" not in st.session_state:
    st.session_state.page = "Runs"
if "goto" in st.session_state:  # navigation requested by a button on the previous script run
    st.session_state.page = st.session_state.pop("goto")
pending_all = store.pending_approvals()
with st.sidebar:
    st.title("🐝 SwarmForge")
    pages = ["Runs", "New run", "Queue", "Knowledge", "Benchmarks"]
    st.radio("Go to", pages, key="page", label_visibility="collapsed")
    if pending_all:
        st.warning(f"🔔 {len(pending_all)} approval(s) waiting")
    st.divider()
    auto = st.toggle("Live refresh (3 s)", value=os.environ.get("SWARMFORGE_DASH_AUTOREFRESH", "1") == "1",
                     help="Refreshes the Runs page while a run is active.")
    if cfg:
        st.caption(f"Config: `{cfg.source_path}`")
        st.caption(f"Knowledge vault: `{cfg.vault_path}`")
    else:
        st.error("No swarmforge.yaml found. Start the dashboard with `swarmforge dashboard` "
                 "from your SwarmForge folder.")
page = st.session_state.page


# ============================================================== NEW RUN page
def page_new_run():
    st.header("Start a new run")
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
        repo = st.text_input("Repository", key="nr_repo",
                             placeholder=r"C:\Users\you\code\myproject   or   owner/repo   or   https://github.com/owner/repo",
                             help="A local git folder, a GitHub owner/repo, or any GitHub URL of the repo "
                                  "(…/tree/branch selects that branch).")

    c1, c2, c3, c4, c5 = st.columns(5)
    mode = c1.selectbox("Team", ["swarm", "single"], format_func=lambda m: {
        "swarm": "Swarm (5 roles)", "single": "Single agent (baseline)"}[m])
    budget = c2.number_input("Budget (USD)", 0.5, 500.0, float(cfg.limits.budget_usd), 0.5)
    cycles = c3.number_input("Max repair cycles", 0, 10, int(cfg.limits.max_repair_cycles))
    hours = c4.number_input("Time limit (hours)", 0.05, 72.0, _hrs(cfg.limits.max_runtime_s), 0.5,
                            help="The whole run stops after this long. Default from swarmforge.yaml "
                                 "(limits.max_runtime_s).")
    approval = c5.selectbox("Before pushing to GitHub", ["dashboard", "yes"], format_func=lambda a: {
        "dashboard": "Ask me here", "yes": "Pre-approve"}[a])

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
                if ref.source != repo.strip():
                    st.info(f"Using {ref.source}" + (f" (branch {ref.branch})" if ref.branch else ""))
            elif source != "GitHub issue":
                raise ValueError("give a repository: a local folder, owner/repo, or a GitHub URL")
            run_id, log = start_run(cfg, repo=repo_arg, issue_url=url, issue_title=title, issue_body=body,
                                    mode=mode, budget=budget, max_cycles=int(cycles), approval=approval,
                                    test_command=(test_cmd or "").strip() or None,
                                    sandbox_image=(image or "").strip() or None, base=base,
                                    max_runtime_s=int(hours * 3600))
        except (ValueError, RepoError) as e:
            st.error(str(e))
            return
        st.session_state.sel_run = run_id
        st.session_state.goto = "Runs"
        time.sleep(1.5)  # give the process a moment to register the run
        st.rerun()


# ================================================================= RUNS page
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


def page_runs():
    runs = store.list_runs(200)
    st.header("Runs")
    if not runs:
        st.info("No runs yet. Go to **New run** to start one.")
        return
    runs_overview(runs)
    ids = [r["id"] for r in runs]
    default = st.session_state.get("sel_run")
    idx = ids.index(default) if default in ids else 0
    labels = {r["id"]: f"{STATE_ICON.get(r['state'], '🟢')} {r['id']} · {(r['issue_title'] or '')[:50]}" for r in runs}
    run_id = st.selectbox("Open run", ids, index=idx, format_func=labels.get)
    st.session_state.sel_run = run_id
    st.divider()
    run_detail(run_id)
    run = store.get_run(run_id)
    if auto and (run["state"] not in TERMINAL or store.pending_approvals()):
        time.sleep(3)
        st.rerun()


def run_resumable(run_id: str) -> bool:
    """True if a stopped run left a clone with integrated work that a new run can continue from."""
    r = store.get_run(run_id)
    if not r or not r["integration_branch"] or not r["run_dir"]:
        return False
    if not (Path(r["run_dir"]) / "repo").is_dir():
        return False
    return any(t["state"] == "INTEGRATED" and t["kind"] in ("implement", "fix") for t in store.tasks(run_id))


def _requeue_next(item: dict, kw: dict) -> None:
    """Re-add a queue item right after the items that are already running (front of the waiting line)."""
    new_id = store.queue_add(item["title"], item["body"], item["repo"], **kw)
    waiting = [q for q in store.queue_items() if q["status"] == "queued" and q["id"] != new_id]
    if waiting:
        store.queue_update(new_id, position=waiting[0]["position"] - 0.5)
    store.kv_set("queue_paused", "0")


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
                st.session_state.sel_run = rid
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



def run_detail(run_id):
    run = store.get_run(run_id)
    tasks = store.tasks(run_id)
    events = store.events(run_id)
    cost = store.cost_summary(run_id)
    inter = store.interventions(run_id)
    tests = store.test_runs(run_id)
    findings = store.findings(run_id)
    acc = store.acceptance(run_id)
    elapsed = (run["finished_at"] or time.time()) - run["created_at"]

    st.subheader(run["issue_title"] or run_id)
    st.caption(f"Run `{run_id}` · {run['mode']} · source {run['issue_url'] or '-'} · repo {run['repo'] or '-'}")

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
    if run["state"] in ("FAILED", "BLOCKED", "CANCELLED", "BUDGET_EXCEEDED") and cfg:
        restart_panel(run)
    if active and not run["cancel_requested"]:
        if st.button("⏹ Request cancel"):
            store.request_cancel(run_id)
            store.event(run_id, "cancel", "cancel requested from dashboard", level="warn")
            st.rerun()

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


# ================================================================ QUEUE page
QSTATUS = {"queued": "⏳", "running": "🟢", "done": "✅", "failed": "❌", "cancelled": "⏹️", "skipped": "⏭️"}


def page_queue():
    from swarmforge import runqueue as rq
    st.header("Queue")
    st.caption("Line up tasks; SwarmForge runs them one after another. With **chaining**, each task starts "
               "from the previous task's result on the same repository, so improvements build on each other "
               "without merging in between. Nothing is ever merged into your main branch.")
    if not cfg:
        st.error("Configuration not loaded.")
        return
    items = store.queue_items()
    pid = rq.worker_pid(store)
    paused = store.kv_get(rq.PAUSED_KEY) == "1"
    queued = [i for i in items if i["status"] == "queued"]
    running = [i for i in items if i["status"] == "running"]

    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([2, 1, 1, 2])
        if pid:
            c1.markdown(f"**Worker:** 🟢 running (pid {pid})" + (" — will pause after the current run" if paused else ""))
        else:
            c1.markdown("**Worker:** ⚪ stopped" + (" (paused)" if paused and queued else ""))
        if not pid:
            if c2.button("▶ Start queue", type="primary", disabled=not queued):
                rq.start_worker(cfg, store)
                time.sleep(1.5)
                st.rerun()
        elif not paused:
            if c2.button("⏸ Pause after current"):
                store.kv_set(rq.PAUSED_KEY, "1")
                st.rerun()
        else:
            if c2.button("▶ Keep going"):
                store.kv_set(rq.PAUSED_KEY, "0")
                st.rerun()
        if running and c3.button("⏹ Cancel current run"):
            store.request_cancel(running[0]["run_id"])
            store.kv_set(rq.PAUSED_KEY, "1")
            st.rerun()
        policy = store.kv_get(rq.POLICY_KEY) or "stop"
        new_policy = c4.selectbox("If a task fails", ["stop", "continue"], index=["stop", "continue"].index(policy),
                                  format_func=lambda p: {"stop": "Pause the queue (recommended)",
                                                         "continue": "Continue with the next task"}[p])
        if new_policy != policy:
            store.kv_set(rq.POLICY_KEY, new_policy)
        n_total = len(items)
        n_done = sum(1 for i in items if i["status"] == "done")
        if n_total:
            st.progress(n_done / n_total, text=f"{n_done} of {n_total} done · {len(queued)} waiting")

    if not items:
        st.info("The queue is empty. Add a task below or import a backlog file.")
    for i in items:
        icon = QSTATUS.get(i["status"], "")
        label = f"{icon} #{i['id']} {i['title']}  ·  {i['status']}"
        with st.expander(label, expanded=i["status"] == "running"):
            st.caption(f"repo `{i['repo']}` · {i['mode']} · budget ${i['budget'] or cfg.limits.budget_usd:.2f} · "
                       f"repair cycles {i['max_cycles'] if i['max_cycles'] is not None else cfg.limits.max_repair_cycles} · "
                       f"{'chained' if i['chain'] else 'independent'} · "
                       f"{'draft PRs without asking' if i['approval'] == 'yes' else 'ask before pushing'}")
            if i["base_branch"]:
                st.markdown(f"Started from `{i['base_branch']}`")
            if i["result_branch"]:
                st.markdown(f"Result branch: `{i['result_branch']}`")
            if i["note"]:
                st.caption(i["note"])
            if i["run_id"]:
                if st.button("Open run", key=f"q_open_{i['id']}"):
                    st.session_state.sel_run = i["run_id"]
                    st.session_state.goto = "Runs"
                    st.rerun()
            if i["status"] == "queued":
                b1, b2, b3, b4 = st.columns(4)
                if b1.button("⬆ Up", key=f"q_up_{i['id']}"):
                    store.queue_move(i["id"], -1)
                    st.rerun()
                if b2.button("⬇ Down", key=f"q_dn_{i['id']}"):
                    store.queue_move(i["id"], +1)
                    st.rerun()
                if b3.button("🗑 Remove", key=f"q_rm_{i['id']}"):
                    store.queue_remove(i["id"])
                    st.rerun()
                if b4.toggle("Edit", key=f"q_ed_{i['id']}"):
                    with st.form(f"q_form_{i['id']}"):
                        t = st.text_input("Title", i["title"])
                        b = st.text_area("Description", i["body"], height=260)
                        r = st.text_input("Repository", i["repo"])
                        if st.form_submit_button("Save"):
                            store.queue_update(i["id"], title=t, body=b, repo=r)
                            st.rerun()
                else:
                    st.markdown(i["body"][:3000])
            elif i["status"] in ("failed", "cancelled", "skipped"):
                r1, r2, r3 = st.columns([1, 1, 1])
                more = r3.number_input("Time limit (h)", 0.05, 72.0,
                                       _hrs(max(i["max_runtime_s"] or cfg.limits.max_runtime_s,
                                                cfg.limits.max_runtime_s)), 0.5, key=f"q_h_{i['id']}")
                kw = dict(mode=i["mode"], budget=i["budget"], max_cycles=i["max_cycles"], chain=i["chain"],
                          approval=i["approval"], max_runtime_s=int(more * 3600))
                if r1.button("↻ Start again", key=f"q_re_{i['id']}",
                             help="Queue this task again from scratch (same description)."):
                    _requeue_next(i, kw)
                    st.rerun()
                can_continue = bool(i["run_id"]) and run_resumable(i["run_id"])
                if r2.button("⏩ Continue where it stopped", key=f"q_co_{i['id']}", disabled=not can_continue,
                             help="Queue it again starting from the failed run's code, with its unresolved "
                                  "problems added to the task."):
                    _requeue_next(i, {**kw, "seed_run_id": i["run_id"]})
                    st.rerun()
    if any(i["status"] in ("done", "failed", "cancelled", "skipped") for i in items):
        if st.button("Clear finished tasks"):
            store.queue_clear_finished()
            st.rerun()

    st.divider()
    t_add, t_imp = st.tabs(["➕ Add a task", "📥 Import a backlog file"])

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
        chain = st.checkbox("Chain: start from the previous task's result on this repository", value=True,
                            key=f"{prefix}_chain")
        return mode, budget, cycles, approval, chain, int(hours * 3600)

    def checked_repo(repo):
        from swarmforge.repos import normalize_repo, preflight
        from swarmforge.github import get_token
        return preflight(normalize_repo(repo), get_token(cfg.github)).source

    with t_add:
        title = st.text_input("Title", key="qa_title")
        body = st.text_area("Description with acceptance criteria", height=200, key="qa_body")
        repo = st.text_input("Repository", key="qa_repo", value=st.session_state.get("q_last_repo", ""),
                             placeholder="bigdan88/swarmforge   or   C:\\Users\\danie\\Projects\\NiBot")
        mode, budget, cycles, approval, chain, runtime = common("qa")
        if st.button("Add to queue", type="primary"):
            try:
                if not title.strip() or not body.strip():
                    raise ValueError("title and description are required")
                src = checked_repo(repo)
                store.queue_add(title.strip(), body.strip(), src, mode=mode, budget=budget, max_cycles=int(cycles),
                                chain=int(chain), approval=approval, max_runtime_s=runtime)
                st.session_state.q_last_repo = repo
                st.success(f"Queued: {title}")
                st.rerun()
            except Exception as e:
                st.error(str(e))

    with t_imp:
        default = str((cfg.source_path.parent / "docs" / "SELF_IMPROVEMENT_BACKLOG.md")) if cfg.source_path else ""
        path = st.text_input("Backlog file (Markdown with **Title:** / **Description:** sections)", value=default,
                             key="qi_path")
        repo_i = st.text_input("Repository for all imported tasks", key="qi_repo",
                               value=st.session_state.get("q_last_repo", ""),
                               placeholder="bigdan88/swarmforge   or   C:\\Users\\danie\\Projects\\NiBot")
        mode, budget, cycles, approval, chain, runtime = common("qi")
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
                                    chain=int(chain), approval=approval, max_runtime_s=runtime)
                st.session_state.q_last_repo = repo_i
                st.success(f"Imported {len(found)} task(s). Press ▶ Start queue.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

    if auto and (pid or running):
        time.sleep(3)
        st.rerun()



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


{"Runs": page_runs, "New run": page_new_run, "Queue": page_queue, "Knowledge": page_knowledge,
 "Benchmarks": page_benchmarks}[page]()
