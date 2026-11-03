"""Extracted dashboard page; dependencies are passed by the entrypoint."""

def render(st, store, pd):
    st.header("Benchmarks: single agent vs swarm")
    b = store.benchmarks()
    if not b:
        st.info("No benchmarks yet. Run `swarmforge bench ISSUE --repo PATH --acceptance DIR`.")
    for row in b:
        st.subheader(f"{row['label']} — {row['issue']}")
        df = pd.DataFrame(row["results"]).set_index("mode").T
        st.dataframe(df.astype(str), width="stretch")
