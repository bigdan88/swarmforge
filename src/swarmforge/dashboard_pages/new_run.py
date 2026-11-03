"""Page renderer with explicit dependencies supplied by the entrypoint."""

def render(Path, _hrs, cfg, dataclasses, heal_input, pd, re, repo_picker, select_item, st, store, time, vault):
    if not cfg:
        st.error("Configuration not loaded; cannot start runs.")
        return
    from swarmforge.launcher import start_run
    sample_issue = Path(__file__).resolve().parents[2] / "samples" / "issues" / "export-csv-json.md"
    sample_repo = Path("~/swarmforge-sample").expanduser()
    with st.expander("🧪 Try the bundled sample (inventory app, CSV/JSON export issue)"):
        st.write(f"Creates a small git repository at `{sample_repo}` and fills the form below.")
        if st.button("Prepare sample"):
            if not sample_repo.exists():
                import subprocess
                import sys
                subprocess.run([sys.executable, str(sample_issue.parents[1] / "make_sample_repo.py"), str(sample_repo)],
                               check=False, capture_output=True)
            if sample_issue.is_file():
                text = sample_issue.read_text(encoding="utf-8")
                m = re.match(r"#\s+(.+)\n", text)
                st.session_state.nr_source = "Describe it here"
                st.session_state.nr_title = m.group(1) if m else "Sample issue"
                st.session_state.nr_body = text[m.end():].strip() if m else text
                store.remember_repo(str(sample_repo))
                st.session_state.nr_repo = str(sample_repo)
                st.rerun()

    source = st.radio("What should the team work on?", ["Describe it here", "GitHub issue"],
                      key="nr_source", horizontal=True)
    url = title = body = None
    if source == "GitHub issue":
        url = st.text_input("Issue URL", placeholder="https://github.com/owner/repo/issues/42", key="nr_url")
        repo = st.text_input("Repository override (optional)", key="nr_repo_gh",
                             help="Leave empty to clone the issue's repository.")
    else:
        title = st.text_input("Title", key="nr_title", placeholder="Add CSV export to the reports page")
        body = st.text_area("Description, requirements and acceptance criteria", height=260, key="nr_body",
                            placeholder="Describe what you want built. Be specific about interfaces and "
                                        "acceptance criteria; the planner turns this into a task graph.")
        repo = repo_picker("Repository", "nr_repo")

    c1, c2, c3, c4, c5 = st.columns(5)
    mode = c1.selectbox("Team", ["swarm", "reviewed", "single"], format_func=lambda m: {
        "swarm": "Swarm (5 roles)", "reviewed": "One developer + test/review",
        "single": "Single agent (baseline)"}[m])
    budget = c2.number_input("Budget (USD)", 0.5, 500.0, float(cfg.limits.budget_usd), 0.5, key="nr_budget")
    cycles = c3.number_input("Max repair cycles", 0, 10, int(cfg.limits.max_repair_cycles), key="nr_cycles")
    hours = c4.number_input("Time limit (hours)", 0.05, 72.0, _hrs(cfg.limits.max_runtime_s), 0.5,
                            help="The whole run stops after this long. Default from swarmforge.yaml "
                                 "(limits.max_runtime_s).")
    approval = c5.selectbox("Before pushing to GitHub", ["dashboard", "yes"], format_func=lambda a: {
        "dashboard": "Ask me here", "yes": "Pre-approve"}[a])
    heal = heal_input(st.columns(5)[0], "nr_heal")

    with st.expander("Advanced: test command and sandbox image"):
        st.caption("Leave blank to use the repository's `swarmforge.project.yml` (if it has one), "
                   f"otherwise your defaults: `{cfg.project.test_command}` in `{cfg.sandbox.image}`.")
        test_cmd = st.text_input("Test command", key="nr_test_cmd")
        image = st.text_input("Sandbox image (must already be built)", key="nr_image",
                              placeholder="e.g. swarmforge-dev-sandbox:latest")

    from swarmforge.effective_config import effective_config, policy_summary, resolve_variant
    with st.expander("Run policy"):
        preview_cfg = dataclasses.replace(
            cfg, limits=dataclasses.replace(cfg.limits, budget_usd=budget,
                                           max_repair_cycles=int(cycles), max_runtime_s=int(hours * 3600)),
            sandbox=dataclasses.replace(cfg.sandbox, image=(image or "").strip() or cfg.sandbox.image))
        preview_cfg, _ = resolve_variant(preview_cfg, store)
        st.caption(policy_summary(preview_cfg))
        st.json({"mode": mode, **effective_config(preview_cfg)}, expanded=False)
    with st.expander("Team roster for this run"):
        roster = [{"role": r, "model": cfg.roles[r].model} for r in
                  ["planner", *cfg.developer_roles, "tester", "reviewer", cfg.librarian_role] if r in cfg.roles]
        st.dataframe(pd.DataFrame(roster).drop_duplicates(), hide_index=True, width="stretch")
        st.caption("Change models in swarmforge.yaml (then restart the dashboard).")

    if vault and (body or title):
        found = vault.search(f"{title or ''}\n{body or ''}", k=cfg.knowledge.max_notes_in_prompt)
        if found:
            with st.expander(f"📚 {len(found)} knowledge note(s) the team will start from"):
                for sc, n in found:
                    st.markdown(f"- **{n.title}** · _{n.type}_ · {n.repo or ''} (score {sc})")

    if st.button("🚀 Start run", type="primary"):
        from swarmforge.github import get_token, parse_issue_url
        from swarmforge.repos import RepoError, normalize_repo, preflight
        try:
            base = None
            repo_arg = None
            if source == "GitHub issue":
                if not (url or "").strip():
                    raise ValueError("paste the GitHub issue URL (https://github.com/owner/repo/issues/N)")
                try:
                    parse_issue_url(url.strip())
                except Exception:
                    raise ValueError(f"'{url.strip()}' is not a GitHub issue URL. To work on a repository "
                                     "without an issue, choose 'Describe it here' and put the repository URL "
                                     "in the Repository field.")
                url = url.strip()
            elif not (title or "").strip() or not (body or "").strip():
                raise ValueError("give the run a title and a description")
            from swarmforge.preflight import environment_problems
            with st.spinner("Checking Docker, keys and the repository…"):
                problems = environment_problems(cfg, (image or "").strip() or None)
            if problems:
                raise ValueError("\n\n".join(problems))
            if repo and repo.strip():
                with st.spinner("Checking the repository…"):
                    ref = preflight(normalize_repo(repo), get_token(cfg.github))
                repo_arg, base = ref.source, ref.branch
                store.remember_repo(repo.strip() if ref.branch else ref.source)
                if ref.source != repo.strip():
                    st.info(f"Using {ref.source}" + (f" (branch {ref.branch})" if ref.branch else ""))
            elif source != "GitHub issue":
                raise ValueError("give a repository: a local folder, owner/repo, or a GitHub URL")
            run_id, log = start_run(cfg, repo=repo_arg, issue_url=url, issue_title=title, issue_body=body,
                                    mode=mode, budget=budget, max_cycles=int(cycles), approval=approval,
                                    test_command=(test_cmd or "").strip() or None,
                                    sandbox_image=(image or "").strip() or None, base=base,
                                    max_runtime_s=int(hours * 3600), heal_attempts=heal)
        except (ValueError, RepoError) as e:
            st.error(str(e))
            return
        select_item(f"r:{run_id}")
        st.session_state.goto = "Work"
        time.sleep(1.5)  # give the process a moment to register the run
        st.rerun()
