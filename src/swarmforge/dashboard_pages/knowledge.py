"""Extracted dashboard page; dependencies are passed by the entrypoint."""
from ..knowledge import NOTE_TYPES

def render(st, vault, wikilinks_to_md, _esc, TYPE_COLORS):
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
