"""Extracted dashboard page; dependencies are passed by the entrypoint."""


def _suite_table(pd, rows):
    """One line per team / mode for a `bench-suite` comparison (rows carry case + repeat)."""
    from swarmforge.evaluation import summarize
    summary = summarize(rows)
    table = []
    for key, s in summary.items():
        team, _, mode = key.rpartition(" / ")
        table.append({"team": team or "(default)", "mode": mode, "runs": s["runs"],
                      "successful": s["successful"], "delivery %": round(100 * s["delivery_rate"]),
                      "median min": round(s["median_elapsed_s"] / 60, 1),
                      "p95 min": round(s["p95_elapsed_s"] / 60, 1), "cost $": s["cost_usd"],
                      "$ per success": s["cost_per_success_usd"], "repair cycles": s["repair_cycles"],
                      "unpriced calls": s["unpriced_calls"]})
    return pd.DataFrame(table)


def render(st, store, pd):
    st.header("Benchmarks")
    st.caption("Compare workflows and model teams on the same pinned tasks: "
               "`swarmforge bench-suite --teams chinese american-open american-premium --budget 20` "
               "(the budget then applies to each team).")
    b = store.benchmarks()
    if not b:
        st.info("No benchmarks yet. Run `swarmforge bench-suite bench/suite.yaml --budget 20` "
                "(add `--teams ...` to compare model teams), or `swarmforge bench ISSUE --repo PATH --acceptance DIR`.")
    for row in b:
        st.subheader(f"{row['label']} — {row['issue']}")
        results = row["results"] or []
        if results and all(isinstance(r, dict) and "case" in r for r in results):
            df = _suite_table(pd, results)
            st.dataframe(df, hide_index=True, width="stretch")
            if df["team"].nunique() > 1:
                chart = df.assign(group=df["team"] + " / " + df["mode"]).set_index("group")
                c1, c2 = st.columns(2)
                c1.caption("Delivery rate (% of runs that passed every hidden test)")
                c1.bar_chart(chart["delivery %"], horizontal=True)
                c2.caption("Cost per successful run ($)")
                c2.bar_chart(chart["$ per success"].fillna(0), horizontal=True)
            continue
        df = pd.DataFrame(results).set_index("mode").T
        st.dataframe(df.astype(str), width="stretch")
