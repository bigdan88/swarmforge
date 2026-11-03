"""Project cards, kickoff interview, rendered spec approval and milestone reports."""
from __future__ import annotations

import asyncio
import copy
import json

from swarmforge.projects import lead, service
from swarmforge.projects.store import Projects
from swarmforge.dashboard_pages.project_activity import describe


def edit_definition(st, definition, key, *, revision=False):
    d = copy.deepcopy(definition)
    labels = {"name": "Project name", "goal": "What should the app achieve?", "users": "Who is it for?",
              "out_of_scope": "Out of scope", "stack": "Technology constraints", "hosting": "Hosting",
              "deadline": "Deadline", "latitude": "Where may the team choose freely?", "ask_before": "Ask before changing…"}
    with st.form(key):
        for field, label in labels.items():
            d[field] = st.text_area(label, d[field], key=key + field)
        d["repo"] = st.text_input("GitHub repository (owner/name)", d["repo"], disabled=revision)
        d["repository_kind"] = st.selectbox("Repository", ["new", "existing"], index=0 if d["repository_kind"] == "new" else 1, disabled=revision)
        d["budget_usd"] = st.number_input("Total project budget ($)", min_value=.01, value=float(d["budget_usd"]))
        for i, m in enumerate(d["milestones"]):
            st.markdown(f"**Milestone {i + 1}**")
            m["name"] = st.text_input("Name", m["name"], key=f"{key}_name_{i}")
            m["goal"] = st.text_area("Goal", m["goal"], key=f"{key}_goal_{i}")
            checks = st.text_area("Done when (one check per line)", "\n".join(m["checks"]), key=f"{key}_checks_{i}")
            m["checks"] = [c.strip() for c in checks.splitlines() if c.strip()]
            a, b = st.columns(2)
            m["estimate_usd"] = a.number_input("Estimated cost ($)", min_value=.01, value=float(m["estimate_usd"]), key=f"{key}_cost_{i}")
            m["estimate_hours"] = b.number_input("Estimated hours", min_value=.01, value=float(m["estimate_hours"]), key=f"{key}_hours_{i}")
        confirmed = st.checkbox("I approve these revised fixed requirements") if revision else True
        saved = st.form_submit_button("Approve revised fixed spec" if revision else "Save proposal")
    return d if saved and confirmed else None


def render(cfg, st, store):
    st.header("Projects")
    st.caption("Turn one approved spec into a complete app, one milestone at a time.")
    if cfg is None:
        st.info("Load a configuration to start a project.")
        return
    ps = Projects(store)
    if st.button("Start a project", type="primary", icon=":material/add:"):
        st.session_state.project_id = ps.create()["id"]
        st.rerun()
    items = ps.list()
    if not items:
        st.info("Start with a short conversation. The swarm will propose a spec for you to approve before it touches a repository.")
        return
    if len(items) > 1:
        with st.expander("All projects", expanded=False):
            for project in items:
                with st.container(border=True):
                    st.markdown("**" + project["name"] + "**")
                    milestones = ps.milestones(project["id"])
                    strip = " → ".join(("✓ " if m["status"] == "done" else "● " if m["idx"] == project["current_milestone"] else "○ ") + m["name"] for m in milestones)
                    st.caption(project["status"].replace("_", " ").title() + f" · ${ps.spent(project['id']):.2f} / ${project['budget_usd']:.2f}")
                    if strip:
                        st.caption(strip)
                    if st.button("Open project", key="open_" + project["id"]):
                        st.session_state.project_id = project["id"]
                        st.rerun()
    names = {p["id"]: p["name"] for p in items}
    chosen = st.session_state.get("project_id")
    pid = st.selectbox("Project", list(names), index=list(names).index(chosen) if chosen in names else 0, format_func=names.get)
    st.session_state.project_id = pid
    p = ps.get(pid)
    st.subheader(p["name"])
    a, b, c = st.columns(3)
    a.metric("Status", p["status"].replace("_", " ").title())
    b.metric("Project spend", f"${ps.spent(pid):.2f}", help="Includes lead calls and project task runs.")
    c.metric("Approved budget", f"${p['budget_usd']:.2f}" if p["approved_at"] else "Awaiting approval")
    milestones = ps.milestones(pid)
    if milestones:
        st.progress(sum(m["status"] == "done" for m in milestones) / len(milestones))
        st.caption(" → ".join(("✓ " if m["status"] == "done" else "● " if m["idx"] == p["current_milestone"] else "○ ") + m["name"] for m in milestones))
        st.link_button("Open repository", "https://github.com/" + p["repo"])
    @st.fragment(run_every=3 if p["status"] == "running" else None)
    def activity():
        current = ps.get(pid)
        tasks = ps.items(pid)
        message = describe(store, current, tasks)
        if message:
            st.info(message)
        if tasks and st.button("View tasks on Work", key=f"project_work_{pid}"):
            st.session_state.goto = "Work"
            st.rerun()
        if current["status"] == "running":
            from swarmforge.runqueue import PAUSED_KEY, start_worker, worker_pid
            if not worker_pid(store) or store.kv_get(PAUSED_KEY) == "1":
                if st.button("Start queue", key=f"project_start_queue_{pid}"):
                    store.kv_set(PAUSED_KEY, "0")
                    start_worker(cfg, store)
            if st.button("Refresh project", key=f"project_refresh_{pid}"):
                st.rerun()
        elif current["status"] != p["status"]:
            st.rerun()

    activity()
    if p["status"] == "drafting" and p["definition"]:
        definition = p["definition"]
        options = {"new": "Create a private GitHub repository", "existing": "Use an existing GitHub repository"}
        with st.form(f"project_repository_{pid}"):
            kind = st.radio("Where should the app live?", list(options),
                            index=0 if definition["repository_kind"] == "new" else 1,
                            format_func=options.get)
            owner, _, name = definition["repo"].partition("/")
            a, b = st.columns(2)
            owner = a.text_input("GitHub account or organization", owner)
            name = b.text_input("Repository name", name)
            st.caption("SwarmForge creates the private repository when you approve and launch the project.")
            if st.form_submit_button("Save repository choice"):
                try:
                    ps.draft(pid, {**definition, "repository_kind": kind, "repo": f"{owner.strip()}/{name.strip()}"})
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
    chat, spec, plan, reports = st.tabs(["Project chat", "Approved spec", "Plan & decisions", "Milestone reports"])
    with chat:
        for m in store.conversation_messages(p["conversation_id"]):
            with st.chat_message(m["role"]):
                st.markdown(m["content"])
        if p["status"] == "drafting":
            st.caption("Tell the kickoff chat your GitHub account and the name you want for a new repo. You can change the repository choice before launch.")
            question = st.chat_input("Describe your app or answer the kickoff questions", key="project_kickoff_" + pid)
            if question:
                try:
                    with st.spinner("Working on your project spec…"):
                        asyncio.run(lead.interview(cfg, store, pid, question))
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
        elif p["status"] in ("at_checkpoint", "waiting_for_you"):
            with st.form("project_reply_" + pid):
                reply = st.text_area("Reply to the lead", value="continue", help="Continue after reviewing the report, or describe a correction.")
                confirm = st.checkbox("I verified the checks marked ‘needs you’")
                sent = st.form_submit_button("Send reply and resume", type="primary")
            if sent:
                try:
                    service.resume(store, pid, reply, confirm_manual=confirm)
                    from swarmforge.runqueue import start_worker
                    start_worker(cfg, store)
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
        elif p["status"] == "running":
            if st.button("Pause project", key="pause_" + pid):
                service.pause(store, pid)
                st.rerun()
            st.caption("The lead replans from the repository after each merge. Other project lanes keep working when this one pauses.")
        if p["status"] not in ("done", "cancelled"):
            with st.expander("Cancel this project"):
                if st.checkbox("Stop pending work and request cancellation of running work", key="cancel_confirm_" + pid) and st.button("Cancel project", key="cancel_" + pid):
                    service.cancel(store, pid)
                    st.rerun()
    with spec:
        if not p["spec"]:
            st.info("The kickoff chat will write a spec here once the requirements are clear.")
        else:
            st.markdown(p["spec"])
            st.download_button("Download spec", p["spec"], "project-spec.md", mime="text/markdown")
            if p["status"] == "drafting":
                with st.expander("Edit the proposal before approval"):
                    definition = edit_definition(st, p["definition"], "draft_" + pid)
                    if definition:
                        try:
                            ps.draft(pid, definition)
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))
                action = "Create private repository" if p["definition"]["repository_kind"] == "new" else "Use existing repository"
                st.caption(f"{action} · {p['repo']}. After launch, the first tasks appear on Work as soon as planning finishes.")
                st.caption("Launch approves task runs and pull requests within this spec and budget. The existing merge rules apply.")
                if st.button("Approve and launch", type="primary", key="launch_" + pid):
                    try:
                        service.approve(store, pid)
                        from swarmforge.runqueue import start_worker
                        start_worker(cfg, store)
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
            elif p["status"] in ("waiting_for_you", "at_checkpoint"):
                with st.expander("Approve a change to the fixed spec"):
                    revised = edit_definition(st, p["definition"], "amend_" + pid, revision=True)
                    if revised:
                        try:
                            service.amend(store, pid, revised)
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))
    with plan:
        for q in ps.items(pid):
            if q["status"] == "cancelled" and q["project_processed"]:
                continue
            with st.container(border=True):
                st.markdown(f"**{q['title']}**")
                st.caption(f"{q['status'].title()} · Milestone {q['milestone'] + 1} · ${q['budget'] or 0:.2f} budget")
                if q["status"] == "tentative":
                    st.caption("Tentative — the lead will refine this before work starts.")
                with st.expander("Task details"):
                    st.markdown(q["body"].split("Approved project spec", 1)[0])
        for e in reversed(ps.events(pid)[-30:]):
            with st.expander(e["kind"].replace("_", " ").title()):
                st.json(json.loads(e["detail"]))
    with reports:
        for m in milestones:
            if not m["report"]:
                continue
            report = json.loads(m["report"])
            st.subheader(m["name"])
            st.write(report["summary"])
            st.caption(f"${report['spent_usd']:.2f} / ${report['estimate_usd']:.2f} estimated · {report['elapsed_hours']:.1f}h / {report['estimate_hours']:.1f}h estimated")
            for check in report["checks"]:
                st.markdown(f"**{check['status'].title()}** — {check['text']}")
                if check["evidence"]:
                    st.json(check["evidence"])
            for path in report["screenshots"]:
                from pathlib import Path
                if Path(path).is_file():
                    st.image(path, caption=Path(path).name)
            if not report["screenshots"]:
                st.caption("Light/dark screenshots were not captured by the runs; visual verification needs you.")
            with st.expander("Plan changes and proposed next milestone"):
                st.markdown(report["plan_changes"])
                st.write(report["next_milestone"])
