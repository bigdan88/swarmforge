"""Extracted dashboard page; dependencies are passed by the entrypoint."""

def render(st, store, pd, alt):
    """Where the time goes, per run and per agent; compare settings before/after a change."""
    from swarmforge import metrics as mx
    st.header("Speed")
    st.caption("Where each finished run's time went. Change a setting, let a few runs finish, then compare the "
               "groups under **By settings**. `swarmforge metrics --csv FILE` exports the same data.")
    n = st.select_slider("Runs to include", [10, 20, 30, 50, 100, 200], value=30, key="speed_n")
    try:
        rows = mx.collect(store, n)
        agents = mx.agent_rows(store, n)
    except Exception as e:  # noqa: BLE001 - a bad row must not break the page
        st.error(f"Could not compute metrics: {e}")
        return
    if not rows:
        st.info("No finished runs yet. Metrics appear here once a run completes or fails.")
        return
    summ = mx.summarize(rows)["all"]
    k = st.columns(4)
    k[0].metric("Median run", f"{summ['total_min']:.0f} min")
    k[1].metric("Median working time", f"{summ['work_min']:.0f} min", help="run time minus waiting in line")
    k[2].metric("Median repair cycles", f"{summ['repair_cycles']:g}")
    k[3].metric("Median cost", f"${summ['cost_usd']:.2f}")

    st.subheader("Where the time goes")
    long = pd.DataFrame([{"run": f"{r['title'][:38]} · {r['run_id'][-6:]}", "phase": p, "minutes": r[f"{p}_min"],
                          "order": mx.PHASES.index(p), "started": r["started"]}
                         for r in rows for p in mx.PHASES if r[f"{p}_min"] > 0.05])
    runs_order = [f"{r['title'][:38]} · {r['run_id'][-6:]}" for r in rows]
    st.altair_chart(alt.Chart(long).mark_bar().encode(
        x=alt.X("sum(minutes):Q", title="minutes"), y=alt.Y("run:N", sort=runs_order, title=None),
        color=alt.Color("phase:N", sort=mx.PHASES, scale=alt.Scale(domain=mx.PHASES)),
        order=alt.Order("order:Q"), tooltip=["run", "phase", alt.Tooltip("minutes:Q", format=".1f")]
    ).properties(height=max(120, 26 * len(rows))), width="stretch")
    med = pd.DataFrame([{"phase": p, "median minutes": summ.get(f"{p}_min") or 0, "what it is": mx.PHASE_HELP[p]}
                        for p in mx.PHASES])
    st.dataframe(med, hide_index=True, width="stretch",
                 column_config={"median minutes": st.column_config.NumberColumn(format="%.1f")})

    st.subheader("By settings")
    st.caption("Medians per settings fingerprint: developer/reviewer/tester model and max steps, repair cycles, "
               "blocking severities, tester every cycle or once.")
    bys = mx.summarize(rows)["by_settings"]
    st.dataframe(pd.DataFrame([{"settings": k2, **v} for k2, v in bys.items()]), hide_index=True, width="stretch",
                 column_config={c: st.column_config.NumberColumn(format="%.1f")
                                for c in ["total_min", "work_min", *[f"{p}_min" for p in mx.PHASES], "repair_cycles"]}
                 | {"cost_usd": st.column_config.NumberColumn(format="$%.2f")})

    st.subheader("Agents by role and model")
    if agents:
        adf = pd.DataFrame(agents)
        g = adf.groupby(["role", "model"]).agg(sessions=("task", "count"), median_min=("minutes", "median"),
                                               max_min=("minutes", "max"), median_sec_per_call=("sec_per_call", "median"),
                                               retries=("retries", "sum")).reset_index().sort_values("median_min",
                                                                                                    ascending=False)
        st.dataframe(g, hide_index=True, width="stretch",
                     column_config={c: st.column_config.NumberColumn(format="%.1f")
                                    for c in ["median_min", "max_min", "median_sec_per_call"]})
    else:
        st.caption("No agent sessions recorded.")

    st.subheader("Runs")
    df = pd.DataFrame(rows).drop(columns=["started"])
    st.dataframe(df, hide_index=True, width="stretch",
                 column_config={c: st.column_config.NumberColumn(format="%.1f") for c in df.columns if c.endswith("_min")})
    c = st.columns(2)
    c[0].download_button("Download runs (CSV)", mx.to_csv(rows), "swarmforge-runs.csv", "text/csv", key="speed_csv")
    c[1].download_button("Download agent sessions (CSV)", mx.to_csv(agents), "swarmforge-agents.csv", "text/csv",
                         key="speed_csv_agents")
    with st.expander("Counters per run (timeouts, step limits, busy models, tool use)"):
        st.dataframe(df[["run_id", "title", "agent_timeouts", "step_limit_hits", "model_busy_waits", "sf_calls", "browse_calls", "test_runs",
                         "test_suite_min", "approval_wait_min"]], hide_index=True, width="stretch")

    from swarmforge.services.healing import healing_summary
    healing = healing_summary(store)
    if healing:
        st.subheader("Self-healing outcomes")
        st.dataframe(pd.DataFrame(healing), hide_index=True, width="stretch")
