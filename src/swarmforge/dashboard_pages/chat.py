"""Page renderer with explicit dependencies supplied by the entrypoint."""

def render(CHAT_AVATARS, CHAT_SCROLL_JS, QUICK_PROMPTS, _chat_actions_key, _chat_conv_id, _chat_load, _chat_md, _chat_rail, _chat_save, _chat_save_attachments, _render_chat_attachments, cfg, components, dataclasses, json, render_message_actions, st, store, uuid):
    if cfg is None:
        st.error("Configuration not loaded; the chatbot needs a configured model role.")
        return
    from swarmforge import chatbot, chat_quick

    def render_tools_used(used):
        if not used:
            return
        with st.expander(f"🔧 Tools used ({len(used)})", expanded=False):
            for tool in used:
                output = str(tool.get("output") or "")
                ok = tool.get("ok", not output.startswith("ERROR:"))
                st.markdown(f"**{tool.get('name', '?')}** — " + ("✅ ok" if ok else "⚠️ error"))
                st.code(json.dumps(tool.get("args") or {}, default=str), language="json")
                if tool.get("preview") or output:
                    st.caption(tool.get("preview") or output[:160])
    page = st.container(key="chat_page")
    with page:
        st.header("Chat: improve SwarmForge and fix failed tasks")
        st.caption("Ask about runs, failures and what to improve. Suggestions come with buttons to launch tasks "
                   "or pick how a failed task should be fixed. CLI: `swarmforge ask \"…\"`.")
        if st.button("Start a project", icon=":material/flag:"):
            from swarmforge.projects.store import Projects
            st.session_state.project_id = Projects(store).create()["id"]
            st.session_state.goto = "Projects"
            st.rerun()
        active_id = _chat_conv_id()
        rail, main = page.columns([1, 3.2], gap="large")

    # ------------------------------------------------ conversation rail (left)
    with rail:
        clicked = _chat_rail(active_id, chatbot)

    # rail clicks are handled before the transcript renders; each one reruns
    if clicked["new"]:
        conv = chatbot.create_conversation(store, "New chat")
        st.session_state.chat_conv_id = conv["id"]
        st.session_state.pop("chat_done", None)
        st.session_state["chat_conversation"] = "dash-" + uuid.uuid4().hex[:8]
        st.rerun()
    if clicked["sel"] and clicked["sel"] != active_id:
        st.session_state.chat_conv_id = clicked["sel"]
        st.rerun()
    if clicked["edit"]:
        st.session_state.chat_renaming = clicked["edit"]
        st.rerun()
    if clicked["cancel"]:
        st.session_state.pop("chat_renaming", None)
        st.rerun()
    if clicked["save"]:
        convs = {c["id"]: c for c in chatbot.list_conversations(store)}
        c = convs.get(clicked["save"])
        if c:
            txt = st.session_state.get(f"chat_rename_{clicked['save']}") or ""
            chatbot.rename_conversation(store, clicked["save"], txt.strip() or c["title"])
        st.session_state.pop("chat_renaming", None)
        st.rerun()
    if clicked["del"]:
        del_id = clicked["del"]
        chatbot.delete_conversation(store, del_id)
        if st.session_state.get("chat_renaming") == del_id:
            st.session_state.pop("chat_renaming", None)
        if active_id == del_id:
            rest = chatbot.list_conversations(store)
            st.session_state.chat_conv_id = (rest[0]["id"] if rest
                                             else chatbot.ensure_default_conversation(store)["id"])
        st.rerun()

    # ------------------------------------------------ transcript (right)
    history, actions = _chat_load(active_id)
    needs = chatbot.failures_needing_attention(store)
    active_conv = chatbot.get_conversation(store, active_id) or {}
    with main, st.container(key="chat_main"):
        with st.container(key="chat_topbar", horizontal=True, vertical_alignment="center"):
            st.markdown(active_conv.get("title") or "New chat", width="stretch")
            tools_on = st.toggle("Tools", value=cfg.chat.tools_enabled, key="chat_tools_enabled",
                                 help="Let the assistant call fetch_url / list_files while answering")
            reset = st.button("Clear", icon=":material/restart_alt:", key="chat_reset", type="tertiary",
                              help="Remove every message in this conversation")
        if reset:
            chatbot.clear_messages(store, active_id)
            store.kv_set(_chat_actions_key(active_id), None)
            history.clear()
            actions.clear()
            st.session_state.pop("chat_done", None)
            st.session_state["chat_conversation"] = "dash-" + uuid.uuid4().hex[:8]
            _chat_save(actions, active_id)
        scroll = st.container(key="chat_scroll", height=520)   # the transcript scrolls on its own
        with scroll:
            if not history:
                with st.container(key="chat_empty"):
                    st.markdown("### What should we look at?")
                    st.markdown("Runs and their statuses, failed tasks, cost and speed, or what to build next.")
                if needs:
                    st.info(f"🩺 {len(needs)} failed task(s) are waiting for your decision. "
                            "Press **Review my failed tasks**.")
            for n, m in enumerate(history):
                with st.chat_message(m["role"], avatar=CHAT_AVATARS.get(m["role"])):
                    if m["role"] == "assistant":
                        calls = m.get("tool_calls")
                        if isinstance(calls, str):
                            try:
                                calls = json.loads(calls)
                            except (TypeError, ValueError):
                                calls = None
                        for tc in calls or []:
                            if not isinstance(tc, dict):
                                continue
                            a = tc.get("args") or {}
                            arg = next((str(v) for v in a.values() if v not in (None, "")), "")[:80]
                            st.caption(f"🔧 {tc.get('name')} · {arg}" if arg else f"🔧 {tc.get('name')}")
                        st.markdown(_chat_md(m["content"]))
                        render_tools_used(calls or [])
                        render_message_actions(n, actions.get(n))
                    else:
                        st.write(m["content"])
                    _render_chat_attachments(m.get("attachments"))
            live = st.container()  # the new exchange renders here, at the bottom of the transcript
        pending = None
        with st.container(key="chat_chips", horizontal=True, horizontal_alignment="center" if not history
                          else "left", gap="small"):
            for key, (label, prompt_text) in zip(("improve", "failures", "slow"), QUICK_PROMPTS.items()):
                lab = label + (f" ({len(needs)})" if key == "failures" and needs else "")
                if st.button(lab, key="chat_q_" + key):
                    pending = prompt_text
        enabled = cfg.chatbot.tools
        parts = []
        if tools_on and (enabled is None or "fetch_url" in enabled):
            parts.append("paste a URL and the assistant fetches it with the fetch_url tool")
        parts.append("use /repo <owner/name>, /code <topic>, /issues <owner/name>")
        st.caption("💡 Quick actions: " + " — or ".join(parts) + ".")
        try:
            from swarmforge.attachments import IMAGE_EXTENSIONS, TEXT_EXTENSIONS
            uploaded = st.file_uploader(
                "Attach images or files",
                label_visibility="collapsed",
                type=sorted(TEXT_EXTENSIONS | IMAGE_EXTENSIONS),
                accept_multiple_files=True,
                key="chat_files",
                help="Images and text/code files are attached to your next question.",
            )
        except Exception:
            uploaded = None
        typed = st.chat_input("Ask about runs, failures, or what to improve…")
    components.html(CHAT_SCROLL_JS.replace("__N__", str(len(history)) + "-" + uuid.uuid4().hex[:6]), height=0)
    prompt = typed or pending
    if not prompt:
        return
    question = chat_quick.expand_prompt(prompt)
    hint = chat_quick.hint_for(prompt)
    att_meta, att_paths = _chat_save_attachments(uploaded)
    msg = {"role": "user", "content": question}
    if att_meta:
        msg["attachments"] = att_meta
    history.append(msg)
    chatbot.add_message(store, active_id, "user", question)
    with live:
        with st.chat_message("user", avatar=CHAT_AVATARS["user"]):
            st.write(prompt)
            if hint:
                st.caption(hint)
            _render_chat_attachments(att_meta)
        conv = st.session_state.setdefault("chat_conversation", "dash-" + uuid.uuid4().hex[:8])
        kwargs = {"history": history[:-1], "conversation": conv}
        if att_paths:
            kwargs["attachments"] = att_paths
        with st.chat_message("assistant", avatar=CHAT_AVATARS["assistant"]):
            tools_box = st.container()
            answer_slot = st.empty()

            calls: list[dict] = []

            def _on_tool(name, args, output):
                a = args or {}
                arg = str(a.get("url") or a.get("path") or next(iter(a.values()), ""))[:80]
                tools_box.caption(f"🔧 {name} · {arg}" if arg else f"🔧 {name}")
                calls.append({"name": name, "args": a, "output": output})

            try:
                with st.spinner("Thinking…"):
                    ask_cfg = dataclasses.replace(
                        cfg, chat=dataclasses.replace(cfg.chat, tools_enabled=tools_on))
                    res = chatbot.ask(ask_cfg, store, question, on_tool=_on_tool, **kwargs)
            except chatbot.ChatError as e:
                answer_slot.error(f"Chatbot: {e}")
                _chat_save(actions, active_id)
                return
            if not calls:
                calls.extend(res.get("tools_used") or [])
            entry = {"role": "assistant", "content": res["answer"]}
            if calls:
                entry["tool_calls"] = calls
            history.append(entry)
            chatbot.add_message(store, active_id, "assistant", res["answer"],
                                tool_calls=calls or None)
            if res.get("actions") and (res["actions"].get("tasks") or res["actions"].get("fixes")):
                actions[len(history) - 1] = res["actions"]
            _chat_save(actions, active_id)
            answer_slot.markdown(_chat_md(res["answer"]))
            render_tools_used(calls)
            st.caption(f"answered by **{res['role']}** · `{res['model']}` · "
                       f"conversation {res.get('conversation', conv)} · "
                       f"cost ${res.get('usage', {}).get('cost_usd', 0):.4f}")
            render_message_actions(len(history) - 1, actions.get(len(history) - 1))
