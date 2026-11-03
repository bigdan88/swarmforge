"""Acceptance tests for the GitHub repo-analysis tools (issue: tools/github_repo.py).

Covers AC1..AC7 of the "GitHub repo-analysis tool" issue:
repo_tree / repo_file / repo_search / repo_issues, token reuse with public
fallback, offline httpx.MockTransport coverage and chat-registry wiring.
No test in this file may touch the network: every HTTP call goes through an
httpx.MockTransport (directly or via a patched ``_client``).
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from swarmforge.chat_tools import ToolRegistry, chat_with_tools
from swarmforge.chatbot import build_chat_registry
from swarmforge.tools import github_repo
from swarmforge.tools.github_repo import (
    MAX_FILE_BYTES,
    TRUNCATION_MARKER,
    GitHubRepoError,
    repo_file,
    repo_issues,
    repo_search,
    repo_tree,
)


def mock_transport(handler):
    return httpx.MockTransport(handler)


def json_response(payload, status=200):
    return httpx.Response(status, json=payload)


@pytest.fixture(autouse=True)
def no_token(monkeypatch):
    """Deterministic 'no GitHub token' unless a test sets one explicitly."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    # get_token() also shells out to `gh auth token`; pretend the CLI is absent.
    monkeypatch.setattr("swarmforge.github.shutil.which", lambda name: None)


# --------------------------------------------------------------------- AC 1
async def test_ac1_repo_tree_lists_directory_entries():
    def handler(request):
        assert request.url.path == "/repos/octocat/Hello/contents/src"
        assert request.url.params["ref"] == "main"
        return json_response(
            [
                {"name": "main.py", "type": "file", "size": 321},
                {"name": "util", "type": "dir", "size": 0},
            ]
        )

    out = await repo_tree(
        "octocat",
        "Hello",
        "src",
        "main",
        transport=mock_transport(handler),
    )
    lines = out.splitlines()
    assert lines[0] == "Contents of octocat/Hello/src@main:"
    assert "- main.py [file] 321 bytes" in lines
    assert "- util [dir] 0 bytes" in lines


async def test_ac1_repo_tree_handles_single_dict_response():
    def handler(request):
        return json_response({"name": "README.md", "type": "file", "size": 9})

    out = await repo_tree("o", "r", "README.md", transport=mock_transport(handler))
    assert "- README.md [file] 9 bytes" in out.splitlines()


async def test_ac1_repo_tree_404_raises_clear_error():
    def handler(request):
        return json_response({"message": "Not Found"}, status=404)

    with pytest.raises(GitHubRepoError, match="not found"):
        await repo_tree("octocat", "nosuch", transport=mock_transport(handler))


# --------------------------------------------------------------------- AC 2
async def test_ac2_repo_file_returns_decoded_content():
    def handler(request):
        assert request.url.path == "/repos/o/r/contents/docs/guide.md"
        return json_response(
            {
                "name": "guide.md",
                "type": "file",
                "content": base64.b64encode(b"# Guide\nline2").decode(),
            }
        )

    out = await repo_file("o", "r", "docs/guide.md", transport=mock_transport(handler))
    assert out == "# Guide\nline2"


async def test_ac2_repo_file_caps_at_50kb_with_truncation_marker():
    big = b"a" * (MAX_FILE_BYTES + 7)

    def handler(request):
        return json_response(
            {"name": "big", "type": "file", "content": base64.b64encode(big).decode()}
        )

    out = await repo_file("o", "r", "big", transport=mock_transport(handler))
    body, _, marker = out.rpartition("\n")
    assert marker == "...[truncated at 50 KB]..."
    assert out == body + TRUNCATION_MARKER
    assert len(body.encode("utf-8")) == 50_000
    # The cap really is <= 50 KB and the docstring documents it accurately.
    assert MAX_FILE_BYTES <= 50 * 1024
    assert "50,000" in repo_file.__doc__
    assert "...[truncated at 50 KB]..." in repo_file.__doc__


async def test_ac2_repo_file_not_truncated_at_cap_boundary():
    exact = b"b" * MAX_FILE_BYTES

    def handler(request):
        return json_response({"content": base64.b64encode(exact).decode()})

    out = await repo_file("o", "r", "f", transport=mock_transport(handler))
    assert out == "b" * MAX_FILE_BYTES
    assert "truncated" not in out


async def test_ac2_repo_file_directory_raises_clear_error():
    def handler(request):
        return json_response([{"name": "src", "type": "dir"}])

    with pytest.raises(GitHubRepoError, match="is a directory"):
        await repo_file("o", "r", "src", transport=mock_transport(handler))


# --------------------------------------------------------------------- AC 3
async def test_ac3_repo_search_scopes_query_and_formats_hits():
    def handler(request):
        assert request.url.path == "/search/code"
        assert request.url.params["q"] == "def parse repo:octocat/hello"
        return json_response(
            {
                "items": [
                    {
                        "path": "src/parser.py",
                        "text_matches": [{"fragment": "def parse(\n  x):"}],
                    },
                    {"path": "tests/test_parser.py", "text_matches": []},
                ]
            }
        )

    out = await repo_search(
        "octocat", "hello", "def parse", transport=mock_transport(handler)
    )
    assert "- src/parser.py :: def parse( x):" in out
    assert "- tests/test_parser.py" in out


async def test_ac3_repo_search_requires_token_message():
    def handler(request):
        return json_response({"message": "Requires authentication"}, status=401)

    with pytest.raises(GitHubRepoError, match="requires a GitHub token"):
        await repo_search("o", "r", "needle", transport=mock_transport(handler))


async def test_ac3_repo_search_empty_query_rejected():
    with pytest.raises(GitHubRepoError, match="query must not be empty"):
        await repo_search(
            "o", "r", "  ", transport=mock_transport(lambda request: json_response({}))
        )


# --------------------------------------------------------------------- AC 4
async def test_ac4_repo_issues_summaries_skip_pull_requests():
    def handler(request):
        assert request.url.path == "/repos/acme/widgets/issues"
        assert request.url.params["state"] == "closed"
        return json_response(
            [
                {
                    "number": 12,
                    "state": "closed",
                    "title": "Fix crash",
                    "labels": [{"name": "bug"}, {"name": "P1"}],
                    "html_url": "https://github.com/acme/widgets/issues/12",
                },
                {
                    "number": 13,
                    "state": "closed",
                    "title": "Docs PR",
                    "labels": [],
                    "html_url": "https://github.com/acme/widgets/pull/13",
                    "pull_request": {"url": "x"},
                },
            ]
        )

    out = await repo_issues(
        "acme", "widgets", "closed", transport=mock_transport(handler)
    )
    assert out == (
        "#12 [closed] Fix crash (labels: bug, P1) "
        "<https://github.com/acme/widgets/issues/12>"
    )
    assert "Docs PR" not in out


async def test_ac4_repo_issues_defaults_to_open_and_rejects_other_states():
    seen = {}

    def handler(request):
        seen["state"] = request.url.params["state"]
        return json_response([])

    out = await repo_issues("o", "r", transport=mock_transport(handler))
    assert seen["state"] == "open"
    assert out == "No open issues in o/r"

    with pytest.raises(GitHubRepoError, match="invalid state"):
        await repo_issues("o", "r", "wontfix", transport=mock_transport(handler))


# --------------------------------------------------------------------- AC 5
@pytest.mark.parametrize("blocked_status", [401, 403])
async def test_ac5_token_used_and_public_fallback_on_rejection(
    monkeypatch, blocked_status
):
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            assert request.headers["Authorization"] == "Bearer tok-123"
            return json_response({"message": "nope"}, status=blocked_status)
        assert "Authorization" not in request.headers
        return json_response([{"name": "a.py", "type": "file", "size": 1}])

    out = await repo_tree("o", "r", transport=mock_transport(handler))
    assert len(calls) == 2, "second attempt must retry anonymously"
    assert "- a.py [file] 1 bytes" in out


async def test_ac5_no_token_means_no_authorization_header():
    def handler(request):
        assert "Authorization" not in request.headers
        return json_response([])

    await repo_tree("o", "r", transport=mock_transport(handler))


async def test_ac5_fallback_still_failing_surfaces_friendly_error(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")

    def handler(request):
        return json_response({"message": "Forbidden"}, status=403)

    with pytest.raises(GitHubRepoError, match="HTTP 403"):
        await repo_tree("o", "r", transport=mock_transport(handler))


async def test_ac5_unauthenticated_error_hints_at_missing_token():
    def handler(request):
        return json_response({"message": "Forbidden"}, status=403)

    with pytest.raises(GitHubRepoError, match="no GitHub token set"):
        await repo_tree("o", "r", transport=mock_transport(handler))


# --------------------------------------------------------------------- AC 6
async def test_ac6_all_http_goes_through_mock_transport_only(monkeypatch):
    """Prove the tools never build a real (networking) client when a transport is given."""
    real_async_client = httpx.AsyncClient

    def guard(*args, **kwargs):
        assert kwargs.get("transport") is not None, (
            "refusing to create a network client"
        )
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", guard)

    def handler(request):
        assert request.url.host == "api.github.com"
        return json_response(
            [
                {
                    "number": 1,
                    "state": "open",
                    "title": "t",
                    "labels": [],
                    "html_url": "u",
                }
            ]
        )

    t = mock_transport(handler)
    await repo_tree("o", "r", transport=t)
    await repo_issues("o", "r", transport=t)


@pytest.mark.parametrize(
    ("owner", "repo", "path"),
    [
        ("..", "r", ""),
        ("o", "..", ""),
        ("bad owner", "r", ""),
        ("o", "r", "../secret"),
        ("o", "r", "src/../../etc/passwd"),
        ("o", "r", "a/./b"),
        ("o", "r", "a\\b"),
        ("o", "r", "a\nb"),
    ],
)
async def test_ac6_rejects_unsafe_owner_and_path_segments(owner, repo, path):
    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("validation must reject before any HTTP request")

    with pytest.raises(GitHubRepoError):
        await repo_tree(owner, repo, path, transport=mock_transport(handler))


async def test_ac6_url_encodes_valid_paths_but_keeps_slashes():
    def handler(request):
        assert request.url.raw_path.decode() == "/repos/o/r/contents/my%20dir/a%26b.py"
        return json_response({"content": base64.b64encode(b"ok").decode()})

    out = await repo_file("o", "r", "my dir/a&b.py", transport=mock_transport(handler))
    assert out == "ok"


# --------------------------------------------------------------------- AC 7
def test_ac7_build_chat_registry_registers_all_four_repo_tools(cfg):
    registry = build_chat_registry(cfg)
    assert isinstance(registry, ToolRegistry)
    names = registry.names()
    assert names[0] == "fetch_url"
    assert {"repo_tree", "repo_file", "repo_search", "repo_issues"} <= set(names)

    by_name = {}
    for schema in registry.schemas():
        assert schema["type"] == "function"
        fn = schema["function"]
        assert {"name", "description", "parameters"} <= set(fn)
        by_name[fn["name"]] = fn["parameters"]
    for tool in ("repo_tree", "repo_file", "repo_search", "repo_issues"):
        params = by_name[tool]
        assert params["type"] == "object"
        assert "owner" in params["properties"] and "repo" in params["properties"]
        assert "owner" in params["required"] and "repo" in params["required"]
    assert by_name["repo_file"]["required"] == ["owner", "repo", "path"]
    assert by_name["repo_search"]["required"] == ["owner", "repo", "query"]
    assert by_name["repo_issues"]["properties"]["state"]["enum"] == [
        "open",
        "closed",
        "all",
    ]


def _route_handler(request):
    """One handler that answers every endpoint the repo tools call."""
    p = request.url.path
    if p == "/repos/o/r/contents" or p.startswith("/repos/o/r/contents/"):
        if request.method == "GET" and p.endswith("f.py"):
            return json_response(
                {
                    "name": "f.py",
                    "type": "file",
                    "content": base64.b64encode(b"xy").decode(),
                }
            )
        return json_response([{"name": "f.py", "type": "file", "size": 2}])
    if p == "/search/code":
        return json_response({"items": [{"path": "src/f.py"}]})
    if p == "/repos/o/r/issues":
        return json_response(
            [
                {
                    "number": 5,
                    "state": "open",
                    "title": "Broken",
                    "labels": [],
                    "html_url": "https://github.com/o/r/issues/5",
                }
            ]
        )
    raise AssertionError(f"unexpected path {p}")


@pytest.mark.parametrize(
    ("tool", "args", "expect"),
    [
        ("repo_tree", {"owner": "o", "repo": "r"}, "- f.py [file] 2 bytes"),
        ("repo_file", {"owner": "o", "repo": "r", "path": "f.py"}, "xy"),
        ("repo_search", {"owner": "o", "repo": "r", "query": "q"}, "- src/f.py"),
        ("repo_issues", {"owner": "o", "repo": "r"}, "#5 [open] Broken"),
    ],
)
async def test_ac7_registry_executes_repo_tools_end_to_end(
    cfg, monkeypatch, tool, args, expect
):
    t = mock_transport(_route_handler)
    monkeypatch.setattr(
        github_repo,
        "_client",
        lambda token, transport=None: httpx.AsyncClient(
            base_url="https://api.github.com",
            headers=github_repo._headers(token),
            timeout=30,
            transport=t,
        ),
    )
    registry = build_chat_registry(cfg)
    out = await registry.execute(tool, args)
    assert expect in out
    assert not out.startswith("ERROR:")


async def test_ac7_tool_loop_runs_repo_tool_and_returns_answer(cfg, monkeypatch):
    t = mock_transport(_route_handler)
    monkeypatch.setattr(
        github_repo,
        "_client",
        lambda token, transport=None: httpx.AsyncClient(
            base_url="https://api.github.com",
            headers=github_repo._headers(token),
            timeout=30,
            transport=t,
        ),
    )
    registry = build_chat_registry(cfg)

    class Resp:
        def __init__(self, message):
            self.message = message

    class FakeLLM:
        def __init__(self):
            self.calls = []
            self.replies = [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "repo_issues",
                                "arguments": json.dumps({"owner": "o", "repo": "r"}),
                            },
                        }
                    ],
                },
                {"role": "assistant", "content": "There is 1 open issue: Broken."},
            ]

        async def chat(self, role, messages, *, tools=None, task_key=None):
            self.calls.append({"messages": messages, "tools": tools})
            return Resp(self.replies.pop(0))

    llm = FakeLLM()
    answer = await chat_with_tools(
        llm, "chatbot", [{"role": "user", "content": "issues?"}], registry
    )
    assert answer == "There is 1 open issue: Broken."
    offered = {s["function"]["name"] for s in llm.calls[0]["tools"]}
    assert {"repo_tree", "repo_file", "repo_search", "repo_issues"} <= offered
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "#5 [open] Broken" in tool_msgs[0]["content"]


async def test_ac7_errors_surface_as_error_strings_never_raise(cfg):
    registry = build_chat_registry(cfg)
    # .. in the owner is rejected by validation before any HTTP happens.
    out = await registry.execute("repo_tree", {"owner": "..", "repo": "r"})
    assert out.startswith("ERROR: GitHubRepoError:")
    assert "invalid owner" in out
    # Empty path for repo_file likewise surfaces instead of raising.
    out2 = await registry.execute("repo_file", {"owner": "o", "repo": "r", "path": ""})
    assert out2.startswith("ERROR: GitHubRepoError:")
