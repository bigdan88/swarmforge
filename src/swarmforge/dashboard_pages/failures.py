"""Page renderer with explicit dependencies supplied by the entrypoint."""

def render(alt, cfg, diagnose_now, fmt_ts, open_run_button, pd, st, store):
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
    if cfg:
        try:
            from swarmforge.postmortem import chain_usage
            cu = chain_usage(store, cfg, f["run_id"])
        except Exception:  # noqa: BLE001 - a broken chain must not crash the page
            cu = None
        if cu:
            label = "chain" if len(cu["chain"]) > 1 else "attempt"
            st.markdown(f"**{label}:** {cu['elapsed_s'] / 3600:.1f}h / {cu['runtime_cap_s'] / 3600:.0f}h · "
                        f"${cu['cost_usd']:.2f} / ${cu['budget_cap_usd']:.0f}")
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
