"""Page renderer with explicit dependencies supplied by the entrypoint."""

def render(auto_merge_panel, cfg, do_update_and_restart, open_run_button, st, store, time):
    from swarmforge import prs as P
    from swarmforge import selfupdate as su
    from swarmforge.github import GitHubClient, GitHubError
    st.header("Pull requests")
    if not cfg:
        st.error("Configuration not loaded.")
        return
    gh = GitHubClient(cfg.github)
    if not gh.token:
        st.warning(f"No GitHub token: add `{cfg.github.token_env}=...` to `.env` (or run `gh auth login`) and "
                   "restart the dashboard. Merging needs Contents + Pull requests: write.")
    repos = P.known_repos(cfg, store)
    if not repos:
        st.info("No GitHub repositories yet: pull requests appear here once a run opens one.")
        return
    c1, c2, c3 = st.columns([3, 2, 1])
    chosen = c1.multiselect("Repositories", repos, default=repos, key="pr_repos")
    method = c2.selectbox("How to merge", ["squash", "merge", "rebase"], key="pr_method",
                          format_func={"squash": "Squash (one commit per PR)", "merge": "Merge commit",
                                       "rebase": "Rebase"}.get)
    if c3.button("🔄 Refresh", key="pr_refresh"):
        st.session_state.pop("pr_cache", None)
    delete = st.checkbox("Delete the swarmforge/* branch after merging", value=True, key="pr_delete")
    cache = st.session_state.get("pr_cache")
    if not cache or time.time() - cache["t"] > 60 or cache["repos"] != chosen:
        data, errors = {}, {}
        with st.spinner("Loading pull requests from GitHub…"):
            for repo in chosen:
                try:
                    data[repo] = P.list_prs(gh, repo, store)
                except (GitHubError, Exception) as e:  # noqa: BLE001
                    errors[repo] = str(e)
        cache = {"t": time.time(), "repos": chosen, "data": data, "errors": errors}
        st.session_state.pr_cache = cache
    for repo, err in cache["errors"].items():
        st.warning(f"{repo}: {err}")
    all_prs = [pr for prs in cache["data"].values() for pr in prs]
    flash = st.session_state.pop("pr_flash", None)
    if flash:
        for ok, msg in flash["results"]:
            (st.success if ok else st.error)(msg)
        own = su.own_repo()
        if own and any(ok and own.lower() in msg.lower() for ok, msg in flash["results"]):
            with st.container(border=True):
                st.markdown("**You merged changes to SwarmForge itself.** Update now to start using them.")
                if st.button("⬆️ Update & restart SwarmForge now", type="primary", key="pr_update"):
                    st.session_state.upd_status = None
                    do_update_and_restart()
    auto_merge_panel(gh, {pr.url for pr in all_prs} if not cache["errors"] else None)
    if not all_prs:
        st.success("No open pull requests. 🎉")
        return
    ready = [pr for pr in all_prs if pr.ready]
    with st.container(border=True):
        a, b = st.columns([3, 2])
        a.markdown(f"**{len(all_prs)} open** · **{len(ready)} ready to merge** · "
                   f"{sum(1 for p in all_prs if p.state == 'dirty')} with conflicts")
        sure = a.checkbox("I've reviewed these changes", key="pr_sure",
                          help="Merging changes the main branch on GitHub. Draft PRs are marked ready first.")
        if b.button(f"Merge all ready ({len(ready)})", type="primary", disabled=not ready or not sure,
                    key="pr_merge_all", width="stretch"):
            with st.spinner("Merging, oldest first…"):
                res = P.merge_all(gh, ready, method, delete)
            st.session_state.pr_flash = {"results": [
                (ok, f"{pr.repo} #{pr.number} {pr.title}: {msg}") for pr, ok, msg in res]}
            st.session_state.pop("pr_cache", None)
            st.session_state.upd_status = None
            st.rerun()
    for repo, prs in cache["data"].items():
        if not prs:
            continue
        st.subheader(repo)
        for pr in prs:
            icon, text = pr.label
            with st.container(border=True):
                h1, h2 = st.columns([5, 2])
                h1.markdown(f"**[#{pr.number} {pr.title}]({pr.url})**  \n"
                            f"`{pr.head}` → `{pr.base}` · by {pr.author} · {pr.created_at[:10]} · "
                            f"+{pr.additions} −{pr.deletions} in {pr.changed_files} file(s)")
                h2.markdown(f"{icon} {text}")
                b1, b2, b3 = st.columns([1.2, 1.4, 3])
                if b1.button("Merge", key=f"pr_m_{repo}_{pr.number}", type="primary", disabled=not pr.ready or not sure,
                             help="Tick “I've reviewed these changes” above first." if not sure else None):
                    try:
                        P.merge(gh, pr, method, delete)
                        st.session_state.pr_flash = {"results": [(True, f"{repo} #{pr.number} {pr.title}: merged")]}
                    except GitHubError as e:
                        st.session_state.pr_flash = {"results": [(False, str(e))]}
                    st.session_state.pop("pr_cache", None)
                    st.session_state.upd_status = None
                    st.rerun()
                if pr.state == "behind" and b2.button("↻ Update branch", key=f"pr_u_{repo}_{pr.number}",
                                                      help="Merge the latest main into this PR (no conflicts)."):
                    try:
                        P.update_branch(gh, pr)
                        st.session_state.pr_flash = {"results": [(True, f"#{pr.number}: branch updated")]}
                    except GitHubError as e:
                        st.session_state.pr_flash = {"results": [(False, str(e))]}
                    st.session_state.pop("pr_cache", None)
                    st.rerun()
                if pr.state == "dirty":
                    b2.caption("Resolve on GitHub, or restart its task so it rebuilds on the latest main.")
                if pr.run_id:
                    with b3.expander("What the swarm did"):
                        try:
                            from swarmforge.report import build_summary
                            st.markdown(build_summary(store, pr.run_id).split("### What you need to do")[0])
                        except Exception as e:  # noqa: BLE001
                            st.caption(f"summary unavailable: {e}")
                        open_run_button("Open the run", pr.run_id, key=f"pr_o_{repo}_{pr.number}")
