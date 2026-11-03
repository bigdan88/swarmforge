"""Tests for swarmforge.chat_quick quick-action helpers."""

from swarmforge.chat_quick import (
    detect_urls,
    expand_prompt,
    hint_for,
    parse_slash,
)


class TestDetectUrls:
    def test_multiple_and_dedupe(self):
        text = "see https://a.example/x and http://b.example/y plus https://a.example/x again"
        assert detect_urls(text) == ["https://a.example/x", "http://b.example/y"]

    def test_trailing_punctuation_stripped(self):
        assert detect_urls("go to https://example.com/page.") == [
            "https://example.com/page"
        ]
        assert detect_urls("(https://example.com/a), then stop!") == [
            "https://example.com/a"
        ]

    def test_no_scheme_no_match(self):
        assert detect_urls("visit example.com or www.example.com") == []
        assert detect_urls("plain text, no links") == []

    def test_case_insensitive_scheme(self):
        assert detect_urls("HTTPS://Example.COM/x") == ["HTTPS://Example.COM/x"]


class TestParseSlash:
    def test_repo(self):
        assert parse_slash("/repo owner/repo") == ("/repo", "owner/repo")

    def test_code_with_spaces(self):
        assert parse_slash("/code how does the parser work?") == (
            "/code",
            "how does the parser work?",
        )

    def test_issues_case_insensitive(self):
        assert parse_slash("/ISSUES owner/repo") == ("/issues", "owner/repo")

    def test_no_argument(self):
        assert parse_slash("/repo") is None
        assert parse_slash("/repo ") is None

    def test_not_a_command(self):
        assert parse_slash("hello /repo x") is None
        assert parse_slash("/repositories owner/repo") is None
        assert parse_slash("/other thing") is None


class TestExpandRepo:
    def test_repo_slug(self):
        out = expand_prompt("/repo owner/repo")
        assert "https://github.com/owner/repo" in out
        assert "fetch_url" in out
        assert "structured summary of the repository owner/repo" in out

    def test_repo_github_url(self):
        out = expand_prompt("/repo https://github.com/owner/repo.git")
        assert "https://github.com/owner/repo " in out
        assert "repository owner/repo" in out

    def test_repo_unparseable_falls_back(self):
        out = expand_prompt("/repo just-a-name")
        assert out.startswith("Use the fetch_url tool on just-a-name and")


class TestExpandCode:
    def test_code(self):
        out = expand_prompt("/code the URL parser")
        assert out.startswith('Explain the code relevant to "the URL parser":')
        assert "fetch_url" in out
        assert "file paths and short excerpts" in out


class TestExpandIssues:
    def test_issues_slug(self):
        out = expand_prompt("/issues owner/repo")
        assert "https://github.com/owner/repo/issues" in out
        assert "open issues of owner/repo" in out

    def test_issues_unparseable(self):
        out = expand_prompt("/issues https://example.com/list")
        assert out.startswith("Use the fetch_url tool on https://example.com/list and")


class TestExpandUrls:
    def test_url_hint_appended(self):
        text = "what is https://example.com/doc about?"
        out = expand_prompt(text)
        assert out.startswith(text)
        assert "[chat quick-action] Detected link(s): https://example.com/doc." in out
        assert "Use the fetch_url tool on each URL" in out

    def test_url_hint_caps_at_three(self):
        text = " ".join(f"https://example.com/{i}" for i in range(5))
        out = expand_prompt(text)
        hint = out[len(text) :]
        assert (
            "https://example.com/0, https://example.com/1, https://example.com/2."
            in hint
        )
        assert "https://example.com/3" not in hint
        assert "https://example.com/4" not in hint

    def test_no_double_hint(self):
        text = "use fetch_url on https://example.com/a"
        assert expand_prompt(text) == text

    def test_idempotent(self):
        once = expand_prompt("read https://example.com/a please")
        assert "fetch_url" in once
        assert expand_prompt(once) == once


class TestPassthrough:
    def test_plain_text_unchanged(self):
        assert expand_prompt("hello there") == "hello there"

    def test_input_not_mutated(self):
        original = "check https://example.com/x"
        expand_prompt(original)
        assert original == "check https://example.com/x"


class TestHintFor:
    def test_url_hint(self):
        assert hint_for("see https://example.com/a") == "🔗 fetch_url hint added"

    def test_repo_hint(self):
        assert hint_for("/repo owner/repo") == "🔗 fetch_url hint added"

    def test_code_hint(self):
        assert hint_for("/code parser") == "💻 expanded to a code-explainer prompt"

    def test_issues_hint(self):
        assert hint_for("/issues owner/repo") == "🐙 expanded to an issue-triage prompt"

    def test_none(self):
        assert hint_for("no quick action here") is None
