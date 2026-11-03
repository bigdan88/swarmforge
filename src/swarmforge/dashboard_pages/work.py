"""Page renderer with explicit dependencies supplied by the entrypoint."""

def render(_workdir_bytes, auto, cfg, st, store, welcome_panel, work_items, work_list, collapse_attempts):
    from swarmforge import branding
    from swarmforge import runqueue as rq
    from swarmforge.dashboard_pages.project_activity import describe
    from swarmforge.projects.store import Projects
    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))
    h1, h2 = st.columns([4, 1], vertical_alignment="bottom")
    h1.header("Work")
    h1.caption("Track your team’s work from idea to pull request.")
    if h2.button("New task", icon=":material/add:", type="primary", width="stretch"):
        st.session_state.goto = "New task"
        st.rerun()
    ps = Projects(store)
    items = work_items()
    has_active_project = store._one("SELECT id FROM projects WHERE status = 'running' LIMIT 1")

    @st.fragment(run_every=3 if auto and has_active_project else None)
    def project_progress():
        if not items and has_active_project and store._one("SELECT id FROM queue_items LIMIT 1"):
            st.rerun()
        for project in ps.list():
            if project["status"] not in ("running", "waiting_for_you"):
                continue
            message = describe(store, project, ps.items(project["id"]))
            if message:
                st.info(f"{project['name']} · {message}")
                if st.button("Open project", key=f"work_project_{project['id']}"):
                    st.session_state.project_id = project["id"]
                    st.session_state.goto = "Projects"
                    st.rerun()

    project_progress()
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
        current = initial_items.pop() if initial_items else work_items()
        visible = [w for w in current if not w["hidden"] and not w["archived"]]
        st.markdown(branding.work_summary(collapse_attempts(visible)), unsafe_allow_html=True)
        work_list(current, every)
    initial_items = [items]
    listing()
    if cfg is not None:
        st.caption(f"Workspace storage: {_workdir_bytes(str(cfg.workdir)) / 1e6:.1f} MB")
