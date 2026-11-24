"""OpenAI-compatible SSE assembly. Only public answer text is sent to chat callbacks."""
from __future__ import annotations

import json
import re

import httpx


class StreamInterrupted(RuntimeError):
    def __init__(self, message, data=None):
        super().__init__(message)
        self.data = data or {}


class CompletionStream:
    def __init__(self, on_delta=None):
        self.on_delta = on_delta
        self.content = ""
        self.tools: dict[int, dict] = {}
        self.usage = None
        self.finish = None
        self.done = False

    def feed(self, event: str):
        if event == "[DONE]":
            self.done = True
            return
        data = json.loads(event)
        if data.get("error"):
            raise StreamInterrupted(str(data["error"])[:500], self.result())
        if data.get("usage"):
            self.usage = data["usage"]
        for choice in data.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if isinstance(text, str) and text:
                self.content += text
                if self.on_delta:
                    self.on_delta(text)
            for fragment in delta.get("tool_calls") or []:
                index = fragment.get("index", 0)
                if type(index) is not int or not 0 <= index < 32:
                    raise StreamInterrupted("Invalid streamed tool call index", self.result())
                tool = self.tools.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if fragment.get("id"):
                    tool["id"] = fragment["id"]
                for key in ("name", "arguments"):
                    tool["function"][key] += (fragment.get("function") or {}).get(key) or ""
            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]

    def result(self):
        message = {"role": "assistant", "content": self.content}
        if self.tools:
            message["tool_calls"] = [self.tools[i] for i in sorted(self.tools)]
        return {"choices": [{"message": message, "finish_reason": self.finish}], "usage": self.usage}


async def completion_request(client, url, headers, body, on_delta=None):
    if on_delta is None:
        return await client.post(url, headers=headers, json=body)
    state = CompletionStream(on_delta)
    request = {**body, "stream": True, "stream_options": {**body.get("stream_options", {}), "include_usage": True}}
    async with client.stream("POST", url, headers=headers, json=request) as response:
        if response.status_code >= 400:
            await response.aread()
            return response
        if "text/event-stream" not in response.headers.get("content-type", ""):
            # Some compatible adapters return ordinary JSON despite stream=True.
            await response.aread()
            data = response.json()
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
            if text:
                on_delta(text)
            return response
        pending = []
        try:
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    pending.append(line[5:].lstrip())
                elif not line and pending:
                    state.feed("\n".join(pending))
                    pending = []
            if pending:
                state.feed("\n".join(pending))
        except (httpx.HTTPError, ValueError) as exc:
            raise StreamInterrupted("The reply stream was interrupted", state.result()) from exc
        if not state.done or not state.finish:
            raise StreamInterrupted("The reply stream ended before completion", state.result())
    return httpx.Response(200, json=state.result())


def text_callback(on_text):
    chunks = []
    def delta(text):
        chunks.append(text)
        on_text("".join(chunks))
    return delta


def public_text(text):
    return text.split("```actions", 1)[0].rstrip()


class JSONReplyStream:
    """Show the `reply` string during a structured project turn, never its JSON specification."""
    def __init__(self, on_text):
        self.on_text, self.buffer, self.last = on_text, "", ""

    def __call__(self, chunk):
        self.buffer += chunk
        match = re.search(r'(?<!\\)"reply"\s*:\s*"', self.buffer)
        if not match:
            return
        raw, i, end = self.buffer[match.end():], 0, 0
        while i < len(raw):
            if raw[i] == '"':
                break
            if raw[i] == "\\":
                size = 6 if raw[i:i+2] == "\\u" else 2
                if i + size > len(raw):
                    break
                i += size
            else:
                i += 1
            end = i
        try:
            value = json.loads('"' + raw[:end] + '"')
        except ValueError:
            return
        if value and 0xD800 <= ord(value[-1]) <= 0xDBFF:
            value = value[:-1]
        if value != self.last:
            self.last = value
            self.on_text(value)
