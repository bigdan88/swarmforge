"""Acceptance tests for multimodal (list-content) message support in llm.py.

Maps to the issue's acceptance criteria:
  AC1 - LLMClient.chat accepts list-content messages; vision-capable model
        forwards the blocks unchanged (mock transport asserts payload shape).
  AC2 - base64 data-URL images round-trip byte-identically into the provider
        payload (exact url string preserved, including '+', '/' and '=').
  AC3 - non-vision model: image blocks are stripped, a short text note is
        prepended to that message's content, no exception is raised; the
        capability decision is explicit (config `vision_models` list and/or
        model-name heuristic) and is re-evaluated per resolved model.
  AC4 - existing text-only call sites are unaffected: string-content messages
        pass through unchanged (regression).
"""

from __future__ import annotations

import base64
import json
import os

import httpx
import pytest

from swarmforge.llm import (
    LLMClient,
    messages_contain_images,
    model_supports_vision,
    normalize_messages,
)


# --------------------------------------------------------------- helpers
def _ok_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
    )


def _capture_transport(captured: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.setdefault("raw", []).append(bytes(request.content))
        captured.setdefault("bodies", []).append(json.loads(request.content))
        return _ok_response()

    return httpx.MockTransport(handler)


def _image_message(b64: str, *, text: str = "what is in this image?") -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ],
    }


# --------------------------------------------------------------- AC1
async def test_ac1_list_content_forwarded_unchanged_for_vision_model(cfg):
    """AC1: list-content messages are accepted; for a vision-capable model the
    provider payload contains the blocks unchanged (mock transport asserts shape)."""
    cfg.vision_models = [
        "qwen-fake"
    ]  # explicit config list marks the model vision-capable
    b64 = base64.b64encode(os.urandom(2048)).decode()
    msg = _image_message(b64)
    captured: dict = {}
    llm = LLMClient(cfg, transport=_capture_transport(captured))

    res = await llm.chat("planner", [msg])

    assert res.content == "ok"
    body = captured["bodies"][0]
    assert body["model"] == "qwen-fake"
    # payload shape: messages[0].content is the original list of blocks, in order
    assert body["messages"][0]["content"] == [
        {"type": "text", "text": "what is in this image?"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]
    # caller's input is not mutated
    assert msg["content"][1]["type"] == "image_url"


async def test_ac1_vision_model_name_heuristic_forwards_image_blocks(cfg):
    """AC1: vision capability can also be decided by the model-name heuristic
    (no config entry needed). A role whose model name contains a vision hint
    forwards image blocks unchanged."""
    cfg.vision_models = []  # no explicit config entry
    cfg.roles["planner"].model = "openai/gpt-4o-mini"  # matches the "4o" hint
    assert model_supports_vision("openai/gpt-4o-mini", cfg.vision_models)

    b64 = base64.b64encode(os.urandom(64)).decode()
    msg = _image_message(b64)
    captured: dict = {}
    llm = LLMClient(cfg, transport=_capture_transport(captured))

    await llm.chat("planner", [msg])

    content = captured["bodies"][0]["messages"][0]["content"]
    assert content == [
        {"type": "text", "text": "what is in this image?"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]


async def test_ac1_multiple_messages_with_mixed_string_and_list_content(cfg):
    """AC1: a multi-message conversation (string content + list content) is
    forwarded with the list-content message's blocks intact."""
    cfg.vision_models = ["qwen-fake"]
    b64 = base64.b64encode(os.urandom(128)).decode()
    msgs = [
        {"role": "system", "content": "you are a helpful assistant"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
            ],
        },
        {"role": "assistant", "content": "ok"},
    ]
    captured: dict = {}
    llm = LLMClient(cfg, transport=_capture_transport(captured))

    await llm.chat("planner", msgs)

    sent = captured["bodies"][0]["messages"]
    assert sent[0] == msgs[0]
    assert sent[1] == msgs[1]  # list content forwarded unchanged
    assert sent[2] == msgs[2]


# --------------------------------------------------------------- AC2
async def test_ac2_base64_data_url_roundtrips_byte_identically(cfg):
    """AC2: base64 data-URL images are forwarded without re-encoding corruption.
    The exact url string round-trips byte-identically into the provider payload,
    including characters that could be mangled by re-encoding ('+', '/', '=')."""
    cfg.vision_models = ["qwen-fake"]
    # Construct bytes whose base64 encoding deterministically contains '+', '/'
    # and '=' padding (34 bytes -> 2 '=' padding chars; b'\\xfb\\xff\\xef' encodes to "+//v").
    payload_bytes = b"\xfb\xff\xef" * 11 + b"\x7f"  # 34 bytes
    b64 = base64.b64encode(payload_bytes).decode()
    assert "+" in b64 and "/" in b64 and "=" in b64, (
        "test fixture must exercise special chars"
    )
    url = f"data:image/png;base64,{b64}"
    msg = _image_message(b64)

    captured: dict = {}
    llm = LLMClient(cfg, transport=_capture_transport(captured))
    await llm.chat("planner", [msg])

    sent_url = captured["bodies"][0]["messages"][0]["content"][1]["image_url"]["url"]
    # exact string equality
    assert sent_url == url
    # raw request bytes contain the exact URL (no JSON-escape corruption)
    assert url.encode("ascii") in captured["raw"][0]
    # decoded payload equals the original bytes (no re-encoding)
    sent_b64 = sent_url.split(",", 1)[1]
    assert base64.b64decode(sent_b64) == payload_bytes


# --------------------------------------------------------------- AC3
async def test_ac3_non_vision_model_strips_images_and_prepends_note(cfg):
    """AC3: when the resolved model is NOT vision-capable and image blocks were
    passed, the call succeeds without exception; image blocks are stripped and a
    short text note is prepended to that message's content."""
    cfg.vision_models = []  # no explicit config entry
    # planner model "qwen-fake" does not match any vision hint
    assert not model_supports_vision("qwen-fake", cfg.vision_models)

    b64_a = base64.b64encode(os.urandom(32)).decode()
    b64_b = base64.b64encode(os.urandom(48)).decode()
    user_msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "first text"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64_a}"},
            },
            {"type": "text", "text": "second text"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64_b}"},
            },
        ],
    }
    system_msg = {"role": "system", "content": "you are a helpful assistant"}
    msgs = [system_msg, user_msg]
    captured: dict = {}
    llm = LLMClient(cfg, transport=_capture_transport(captured))

    # must not raise
    res = await llm.chat("planner", msgs)
    assert res.content == "ok"

    sent = captured["bodies"][0]["messages"]
    # string-content message passes through unchanged
    assert sent[0] == system_msg
    # list-content message: image blocks stripped, note prepended, other blocks kept in order
    content = sent[1]["content"]
    assert all(b.get("type") != "image_url" for b in content), (
        "image blocks must be stripped"
    )
    # note is the FIRST block (prepended)
    assert content[0]["type"] == "text"
    note_text = content[0]["text"]
    assert "image" in note_text.lower() and "removed" in note_text.lower()
    assert "2" in note_text  # count of stripped images
    # remaining blocks are the original non-image blocks, in original order
    assert {"type": "text", "text": "first text"} in content
    assert {"type": "text", "text": "second text"} in content
    # caller's input is not mutated
    assert user_msg["content"][1]["type"] == "image_url"
    assert user_msg["content"][3]["type"] == "image_url"


async def test_ac3_capability_decision_uses_config_list_and_heuristic(cfg):
    """AC3: the vision-capability decision is explicit: a model listed in
    `vision_models` is vision-capable even if its name doesn't match the
    heuristic; a model matching the heuristic is vision-capable even without
    a config entry; a model matching neither is non-vision."""
    # explicit config list marks a non-heuristic model as vision-capable
    assert model_supports_vision("qwen-fake", ["qwen-fake"])
    # heuristic marks a model as vision-capable without any config entry
    assert model_supports_vision("Qwen/Qwen2.5-VL-72B-Instruct", [])
    assert model_supports_vision("openai/gpt-4o", [])
    # neither -> non-vision
    assert not model_supports_vision("qwen-fake", [])
    assert not model_supports_vision("deepseek-fake", [])


async def test_ac3_fallback_vision_model_receives_image_blocks(cfg):
    """AC3: the capability decision is re-evaluated per resolved model. If the
    primary model is unavailable and the fallback is vision-capable, the
    fallback receives the image blocks intact (not the stripped text-only form)."""
    cfg.vision_models = []
    cfg.roles["planner"].model = "qwen-fake"  # non-vision
    cfg.roles["planner"].fallback_models = [
        "Qwen/Qwen2.5-VL-72B-Instruct"
    ]  # vision via "-vl" hint
    assert not model_supports_vision("qwen-fake", cfg.vision_models)
    assert model_supports_vision("Qwen/Qwen2.5-VL-72B-Instruct", cfg.vision_models)

    b64 = base64.b64encode(os.urandom(64)).decode()
    msg = _image_message(b64)
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if body["model"] == "qwen-fake":
            # busy/unavailable -> triggers fallback
            return httpx.Response(503, json={"error": "busy"})
        return _ok_response()

    # busy_wait_s=0 so the busy path raises immediately and falls back
    llm = LLMClient(cfg, transport=httpx.MockTransport(handler), busy_wait_s=0.0)
    res = await llm.chat("planner", [msg])
    assert res.content == "ok"

    assert len(bodies) >= 2
    # first attempt: non-vision model -> image blocks stripped
    first_content = bodies[0]["messages"][0]["content"]
    assert all(b.get("type") != "image_url" for b in first_content)
    # fallback attempt: vision model -> image blocks forwarded unchanged
    fallback_body = bodies[-1]
    assert fallback_body["model"] == "Qwen/Qwen2.5-VL-72B-Instruct"
    assert fallback_body["messages"][0]["content"] == [
        {"type": "text", "text": "what is in this image?"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]


async def test_ac3_messages_contain_images_helper():
    """AC3 helper: messages_contain_images detects list-content messages with
    image_url blocks and ignores string-content messages."""
    b64 = base64.b64encode(os.urandom(8)).decode()
    img_msg = _image_message(b64)
    assert messages_contain_images([img_msg])
    assert messages_contain_images([{"role": "user", "content": "hi"}, img_msg])
    assert not messages_contain_images([{"role": "user", "content": "hi"}])
    # list content with only text blocks -> no images
    assert not messages_contain_images(
        [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    )


# --------------------------------------------------------------- AC4
async def test_ac4_string_content_passthrough_unchanged_for_vision_and_non_vision(cfg):
    """AC4 regression: existing text-only call sites are unaffected. String
    content passes through the provider payload unchanged for both vision and
    non-vision models."""
    msgs = [
        {"role": "system", "content": "you are a helpful assistant"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "bye"},
    ]
    for vision in (True, False):
        cfg.vision_models = ["qwen-fake"] if vision else []
        captured: dict = {}
        llm = LLMClient(cfg, transport=_capture_transport(captured))
        await llm.chat("planner", msgs)
        # payload messages equal the input list exactly (same dicts, same order)
        assert captured["bodies"][0]["messages"] == msgs


async def test_ac4_list_content_without_images_untouched_for_non_vision(cfg):
    """AC4 regression: list content with only text blocks (no images) passes
    through unchanged even for a non-vision model."""
    cfg.vision_models = []
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "world"},
            ],
        },
    ]
    captured: dict = {}
    llm = LLMClient(cfg, transport=_capture_transport(captured))
    await llm.chat("planner", msgs)
    assert captured["bodies"][0]["messages"] == msgs


def test_ac4_normalize_messages_passes_strings_through():
    """AC4 regression: normalize_messages leaves string-content messages
    untouched regardless of vision capability."""
    msgs = [{"role": "user", "content": "hello"}]
    out_v, n_v = normalize_messages(msgs, True)
    out_nv, n_nv = normalize_messages(msgs, False)
    assert out_v == msgs and n_v == 0
    assert out_nv == msgs and n_nv == 0
