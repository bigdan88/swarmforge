"""Multimodal (list-content) message support and vision-model selection in llm.py."""

import base64
import json
import os

import httpx

from swarmforge.config import parse_config
from swarmforge.llm import (
    LLMClient,
    messages_contain_images,
    model_supports_vision,
    normalize_messages,
)

from .conftest import base_raw


def _image_message(b64: str) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is in this image?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ],
    }


def _capture_transport(captured: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )

    return httpx.MockTransport(handler)


async def test_chat_forwards_image_blocks_for_vision_model(cfg):
    cfg.vision_models = ["qwen-fake"]
    b64 = base64.b64encode(os.urandom(4096)).decode()
    captured: dict = {}
    llm = LLMClient(cfg, transport=_capture_transport(captured))
    res = await llm.chat("planner", [_image_message(b64)])
    assert res.content == "ok"
    content = captured["body"]["messages"][0]["content"]
    assert content == [
        {"type": "text", "text": "what is in this image?"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]  # byte-identical base64 data URL, no re-encoding


async def test_non_vision_model_falls_back_to_text_plus_note(cfg):
    assert not model_supports_vision("qwen-fake", cfg.vision_models)
    b64 = base64.b64encode(os.urandom(64)).decode()
    msg = _image_message(b64)
    captured: dict = {}
    llm = LLMClient(cfg, transport=_capture_transport(captured))
    res = await llm.chat("planner", [msg])
    assert res.content == "ok"  # no exception
    content = captured["body"]["messages"][0]["content"]
    assert all(b.get("type") != "image_url" for b in content)
    notes = [
        b
        for b in content
        if b.get("type") == "text" and "image" in b["text"] and "removed" in b["text"]
    ]
    assert len(notes) == 1
    assert {"type": "text", "text": "what is in this image?"} in content
    # caller's input is not mutated
    assert msg["content"][1]["type"] == "image_url"


async def test_text_only_messages_unchanged(cfg):
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "bye"},
    ]
    for vision in (True, False):
        cfg.vision_models = ["qwen-fake"] if vision else []
        captured: dict = {}
        llm = LLMClient(cfg, transport=_capture_transport(captured))
        await llm.chat("planner", msgs)
        assert captured["body"]["messages"] == msgs


def test_vision_heuristic():
    assert model_supports_vision("Qwen/Qwen-VL-Chat")
    assert model_supports_vision("openai/gpt-4o")
    assert not model_supports_vision("deepseek-ai/DeepSeek-V4-Pro")
    assert not model_supports_vision("qwen-fake")
    # explicit config entries: exact and substring, case-insensitive
    assert model_supports_vision("qwen-fake", ["QWEN-FAKE"])
    assert model_supports_vision("deepseek-ai/DeepSeek-V4-Pro", ["deepseek-v4"])
    assert not model_supports_vision("qwen-fake", ["other-model"])


def test_normalize_messages_helpers():
    b64 = base64.b64encode(os.urandom(32)).decode()
    msg = _image_message(b64)
    assert messages_contain_images([msg])
    assert not messages_contain_images([{"role": "user", "content": "hi"}])
    # vision: pass-through, same dict objects
    out, stripped = normalize_messages([msg], True)
    assert out[0] is msg and stripped == 0
    # non-vision: strip + note, input intact
    out, stripped = normalize_messages([msg], False)
    assert stripped == 1
    assert msg["content"][1]["type"] == "image_url"  # input not mutated
    assert out[0]["content"][0]["text"].startswith("[note: 1 image attachment(s)")
    assert out[0]["content"][1] == {"type": "text", "text": "what is in this image?"}
    # unknown block types are kept
    mixed = {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "u"}},
            {"type": "text", "text": "t"},
            {"type": "audio", "data": "x"},
        ],
    }
    out, stripped = normalize_messages([mixed], False)
    assert stripped == 1
    assert [b["type"] for b in out[0]["content"]] == ["text", "text", "audio"]


def test_config_parses_vision_models(tmp_path):
    raw = base_raw(tmp_path / "work")
    raw["vision_models"] = ["A/B-Vision"]
    cfg = parse_config(raw)
    assert cfg.vision_models == ["A/B-Vision"]
    cfg2 = parse_config(base_raw(tmp_path / "work"))
    assert cfg2.vision_models == []
