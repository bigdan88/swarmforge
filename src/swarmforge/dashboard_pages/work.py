"""Page renderer with explicit dependencies supplied by the entrypoint."""

def render(_workdir_bytes, auto, cfg, st, store, welcome_panel, work_items, work_list):
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
