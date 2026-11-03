"""Fix-up round regressions for the Chat quick-actions + 'Tools used' widget.

Targets the three findings that blocked PR #28:

* [major] the collapsible 'Tools used' widget must render on a *fresh page
  load* from persisted history (demo seed now stores tool_calls; here the
  documented ``{"name","args","output"}`` ``add_message`` shape, which the
  live ``ok``/``preview`` tests never exercise).
* [minor] ``_repo_slug`` lost its redundant ``all(parts)`` check -> pin its
  edge-case behaviour directly.
* [minor] URL-hint idempotency keys off the exact
  ``[chat quick-action] Detected link(s):`` marker, not the generic
  ``fetch_url`` word -> pin the marker literal and the once-only expansion.
"""

from streamlit.testing.v1 import AppTest

from swarmforge import chatbot
from swarmforge.chat_quick import URL_HINT_MARKER, _repo_slug, expand_prompt
from swarmforge.db import Store

from .test_dashboard import APP, _go, _write_cfg

MARKER_LITERAL = "[chat quick-action] Detected link(s):"


class TestUrlHintMarkerIdempotency:
    """AC1: precise marker replaces the generic 'fetch_url' suppression."""

    def test_marker_is_the_documented_literal(self):
        assert URL_HINT_MARKER == MARKER_LITERAL

    def test_url_expansion_appends_marker_once(self):
        out = expand_prompt("what does https://a.example/x say?")
        assert MARKER_LITERAL in out
        assert out.count(MARKER_LITERAL) == 1

    def test_fetch_url_word_does_not_suppress_hint(self):
        # Regression for the old generic "fetch_url" substring check: a
        # legitimate question merely *mentioning* the tool still gets the hint.
        text = "Does fetch_url support POST? Here is the endpoint: https://example.com/api"
        out = expand_prompt(text)
        assert MARKER_LITERAL in out
        assert out.count(MARKER_LITERAL) == 1

    def test_marker_present_means_no_second_hint(self):
        text = "read https://a.example/x " + MARKER_LITERAL + " https://a.example/x."
        assert expand_prompt(text) == text


class TestRepoSlug:
    """AC2: behaviour pinned after removing the redundant all(parts) check."""

    def test_plain_slug(self):
        assert _repo_slug("owner/repo") == "owner/repo"

    def test_extra_path_segments_take_first_two(self):
        assert _repo_slug("owner/repo/issues/5") == "owner/repo"
        assert _repo_slug("https://github.com/owner/repo/pull/28") == "owner/repo"

    def test_trailing_slash_and_dot_git(self):
        assert _repo_slug("https://github.com/owner/repo/") == "owner/repo"
        assert _repo_slug("owner/repo.git") == "owner/repo"

    def test_too_few_segments_returns_empty(self):
        assert _repo_slug("") == ""
        assert _repo_slug("/") == ""
        assert _repo_slug("just-a-name") == ""


class TestFreshLoadToolsUsedWidget:
    """AC3: expander on a brand-new AppTest session from persisted history,
    using the documented {"name","args","output"} add_message shape."""

    def test_fresh_page_load_renders_expander_for_output_form_calls(self, tmp_path,
                                                                      monkeypatch):
        cfg = _write_cfg(tmp_path, monkeypatch)
        store = Store(cfg.db_path)
        try:
            conv = chatbot.ensure_default_conversation(store)
            chatbot.add_message(store, conv["id"], "user", "search the docs for x")
            chatbot.add_message(
                store, conv["id"], "assistant", "Found it: # Doc",
                tool_calls=[{"name": "search_docs", "args": {"q": "x"}, "output": "# Doc"}],
            )
        finally:
            store.close()

        at = AppTest.from_file(str(APP), default_timeout=120)
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        _go(at, "Chat")
        assert not at.exception, [e.value for e in at.exception]

        expanders = [e for e in at.expander if "Tools used" in e.label]
        assert len(expanders) == 1
        assert "1" in expanders[0].label
        body = " ".join(m.value for m in expanders[0].markdown)
        assert "search_docs" in body
