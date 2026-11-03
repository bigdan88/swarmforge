"""Page renderer with explicit dependencies supplied by the entrypoint."""

def render(cfg, page_new_run, queue_forms, st):
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
