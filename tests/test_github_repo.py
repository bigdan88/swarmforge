"""Tests for swarmforge.tools.github_repo (offline, httpx.MockTransport)."""

import base64

import httpx
import pytest

from swarmforge.tools.github_repo import (
    GitHubRepoError,
    repo_file,
    repo_issues,
    repo_search,
    repo_tree,
)


def _transport(handler):
    return httpx.MockTransport(handler)


def _json_response(payload, status=200):
    return httpx.Response(status, json=payload)


@pytest.fixture(autouse=True)
def _no_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


async def test_tree_list_response():
    def handler(request):
        assert request.url.path == "/repos/octocat/hello/contents"
        assert "ref" not in request.url.params
        return _json_response(
            [
                {"name": "src", "type": "dir", "size": 0},
                {"name": "README.md", "type": "file", "size": 12},
            ]
        )

    out = await repo_tree("octocat", "hello", transport=_transport(handler))
    assert out.splitlines()[0] == "Contents of octocat/hello/@HEAD:"
    assert "- src [dir] 0 bytes" in out
    assert "- README.md [file] 12 bytes" in out


async def test_tree_dict_response_and_ref_param():
    def handler(request):
        assert request.url.path == "/repos/octocat/hello/contents/src"
        assert request.url.params["ref"] == "dev"
        return _json_response({"name": "src", "type": "dir", "size": 0})

    out = await repo_tree(
        "octocat", "hello", "src", "dev", transport=_transport(handler)
    )
    assert out.splitlines()[0] == "Contents of octocat/hello/src@dev:"
    assert "- src [dir] 0 bytes" in out


async def test_tree_404():
    def handler(request):
        return _json_response({"message": "Not Found"}, status=404)

    with pytest.raises(GitHubRepoError, match="not found"):
        await repo_tree("octocat", "hello", transport=_transport(handler))


async def test_file_happy_path():
    def handler(request):
        assert request.url.path == "/repos/octocat/hello/contents/src/main.py"
        return _json_response(
            {
                "name": "main.py",
                "type": "file",
                "size": 5,
                "content": base64.b64encode(b"hello").decode(),
            }
        )

    out = await repo_file(
        "octocat", "hello", "src/main.py", transport=_transport(handler)
    )
    assert out == "hello"


async def test_file_directory_guard():
    def handler(request):
        return _json_response([{"name": "src", "type": "dir"}])

    with pytest.raises(GitHubRepoError, match="directory"):
        await repo_file("octocat", "hello", "src", transport=_transport(handler))


async def test_file_truncated_at_50k():
    big = b"x" * (50_000 + 100)

    def handler(request):
        return _json_response(
            {
                "name": "big.txt",
                "type": "file",
                "size": len(big),
                "content": base64.b64encode(big).decode(),
            }
        )

    out = await repo_file("octocat", "hello", "big.txt", transport=_transport(handler))
    assert out.endswith("\n...[truncated at 50 KB]...")
    body = out.removesuffix("\n...[truncated at 50 KB]...")
    assert len(body.encode("utf-8")) == 50_000


async def test_file_missing_content():
    def handler(request):
        return _json_response(
            {
                "name": "big.bin",
                "type": "file",
                "size": 10**7,
                "download_url": "https://raw.example/x",
            }
        )

    with pytest.raises(GitHubRepoError, match="download_url"):
        await repo_file("octocat", "hello", "big.bin", transport=_transport(handler))


async def test_token_sent_and_public_fallback_retry(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "sekret")
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            assert request.headers.get("Authorization") == "Bearer sekret"
            return _json_response({"message": "bad token"}, status=403)
        assert "Authorization" not in request.headers
        return _json_response([{"name": "a.py", "type": "file", "size": 1}])

    out = await repo_tree("octocat", "hello", transport=_transport(handler))
    assert "- a.py [file] 1 bytes" in out
    assert len(calls) == 2


async def test_no_token_sends_no_authorization():
    def handler(request):
        assert "Authorization" not in request.headers
        return _json_response([])

    out = await repo_tree("octocat", "hello", transport=_transport(handler))
    assert out.startswith("Contents of")


async def test_search_formats_paths_and_matches():
    def handler(request):
        assert request.url.params["q"] == "def main repo:octocat/hello"
        return _json_response(
            {
                "items": [
                    {
                        "path": "src/main.py",
                        "text_matches": [{"fragment": "def main():\n    pass"}],
                    },
                    {"path": "other.py"},
                ]
            }
        )

    out = await repo_search(
        "octocat", "hello", "def main", transport=_transport(handler)
    )
    assert "- src/main.py :: def main(): pass" in out
    assert "- other.py" in out


async def test_search_empty_result():
    def handler(request):
        return _json_response({"items": []})

    out = await repo_search("octocat", "hello", "zzz", transport=_transport(handler))
    assert out.startswith("No code matches")


async def test_search_requires_token():
    def handler(request):
        return _json_response({"message": "Requires authentication"}, status=401)

    with pytest.raises(GitHubRepoError, match="requires a GitHub token"):
        await repo_search("octocat", "hello", "x", transport=_transport(handler))


async def test_issues_skips_prs_and_honors_state():
    def handler(request):
        assert request.url.params["state"] == "all"
        assert request.url.params["per_page"] == "30"
        return _json_response(
            [
                {
                    "number": 1,
                    "state": "open",
                    "title": "Bug",
                    "labels": [{"name": "bug"}],
                    "html_url": "https://github.com/o/r/issues/1",
                },
                {
                    "number": 2,
                    "state": "closed",
                    "title": "PR thing",
                    "labels": [],
                    "html_url": "https://github.com/o/r/pull/2",
                    "pull_request": {},
                },
            ]
        )

    out = await repo_issues("octocat", "hello", "all", transport=_transport(handler))
    assert out == "#1 [open] Bug (labels: bug) <https://github.com/o/r/issues/1>"


async def test_issues_empty():
    def handler(request):
        return _json_response([])

    out = await repo_issues("octocat", "hello", "closed", transport=_transport(handler))
    assert out == "No closed issues in octocat/hello"


async def test_issues_invalid_state():
    with pytest.raises(GitHubRepoError, match="invalid state"):
        await repo_issues(
            "octocat",
            "hello",
            "bogus",
            transport=_transport(lambda r: _json_response([])),
        )


async def test_validation_rejects_bad_inputs():
    for kwargs in (
        {"owner": "..", "repo": "r"},
        {"owner": "o", "repo": "bad name"},
        {"owner": "o", "repo": "r", "path": "../secret"},
        {"owner": "o", "repo": "r", "path": "a/./b"},
        {"owner": "o", "repo": "r", "path": "a\\b"},
        {"owner": "o", "repo": "r", "path": "a\nb"},
    ):
        with pytest.raises(GitHubRepoError):
            await repo_tree(
                transport=_transport(lambda r: _json_response([])), **kwargs
            )


async def test_path_quoted_in_url():
    def handler(request):
        assert request.url.path == "/repos/octocat/hello/contents/src/main.py"
        return _json_response(
            {
                "name": "main.py",
                "type": "file",
                "size": 1,
                "content": base64.b64encode(b"x").decode(),
            }
        )

    out = await repo_file(
        "octocat", "hello", "src/main.py", transport=_transport(handler)
    )
    assert out == "x"


async def test_path_with_space_percent_encoded():
    def handler(request):
        assert request.url.raw_path.decode() == "/repos/octocat/hello/contents/a%20b.py"
        return _json_response(
            {
                "name": "a b.py",
                "type": "file",
                "size": 1,
                "content": base64.b64encode(b"x").decode(),
            }
        )

    out = await repo_file("octocat", "hello", "a b.py", transport=_transport(handler))
    assert out == "x"


async def test_http_error_wrapped():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(GitHubRepoError):
        await repo_tree("octocat", "hello", transport=_transport(handler))
