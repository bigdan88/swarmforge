"""Feature highlights, grouped by day with search and links to the implementation."""
from swarmforge.history import entries, filter_entries


def render(st, store):
    st.header("What's new")
    st.caption("A plain-language history of what SwarmForge can do and where to find it.")
    items = entries()
    seen = set((store.kv_get("feature_history_seen") or "").split(","))
    unread = sum(e["id"] not in seen for e in items)
    if unread:
        st.info(f"{unread} feature highlights you haven't marked as read.")
        if st.button("Mark all as read"):
            store.kv_set("feature_history_seen", ",".join(e["id"] for e in items))
            st.rerun()
    a, b = st.columns([3, 1])
    query = a.text_input("Search features", placeholder="Try chat, screenshots, benchmarks…")
    category = b.selectbox("Category", ["All"] + sorted({e["category"] for e in items}))
    filtered = filter_entries(items, query, category)
    if not filtered:
        st.info("No highlights match. Try a different search or category.")
    day = None
    for e in filtered:
        if day != e["date"]:
            day = e["date"]
            st.subheader(day)
        with st.container(border=True):
            st.caption(e["category"] + (" · New to you" if e["id"] not in seen else "") + (" · Preview on this branch" if e.get("preview") else ""))
            st.markdown("### " + e["title"])
            st.write(e["summary"])
            st.markdown("**Why it helps:** " + e["benefit"])
            st.caption("Find it: " + e["where"])
            if e.get("page"):
                if st.button("Open " + e["page"], key="feature_" + e["id"]):
                    st.session_state.goto = e["page"]
                    st.rerun()
            if e.get("url"):
                st.link_button("View change", e["url"])
    st.caption("This is a curated feature history for the installed build. A completed task is not listed as a released feature until its change is included. New entries belong in resources/feature-history.json with each feature PR.")
