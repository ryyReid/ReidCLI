"""Streaming: SSE parse, OpenAI chat_stream assembly, session stream modes."""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

from reidx.provider.base import Message, ProviderResponse
from reidx.provider.openai import OpenAICompatibleProvider
from reidx.runtime.agent import Agent
from reidx.runtime.state import RuntimeState
from reidx.session.models import Session
from reidx.config.models import default_config
from reidx.policy.engine import PolicyEngine
from reidx.provider.stub import StubProvider
from reidx.tools import default_registry
from pathlib import Path


def test_iter_sse_json_parses_data_lines() -> None:
    from reidx.provider import _http

    events = [
        b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n',
        b"\n",
        b'data: {"choices":[{"delta":{"content":"!"}}]}\n',
        b"data: [DONE]\n",
    ]

    class _FakeResp:
        def __init__(self) -> None:
            self._i = 0

        def readline(self) -> bytes:
            if self._i >= len(events):
                return b""
            line = events[self._i]
            self._i += 1
            return line

        def close(self) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch.object(_http.urllib.request, "urlopen", return_value=_FakeResp()):
        chunks = list(
            _http.iter_sse_json(
                "https://example.com/v1/chat/completions",
                {"model": "x", "stream": True},
                {},
            )
        )
    assert len(chunks) == 2
    assert chunks[0]["choices"][0]["delta"]["content"] == "Hi"
    assert chunks[1]["choices"][0]["delta"]["content"] == "!"


def test_openai_chat_stream_accumulates_and_callbacks() -> None:
    provider = OpenAICompatibleProvider(api_key="k", base_url="https://example.com/v1")
    deltas: list[str] = []

    def _fake_sse(url, payload, headers, timeout=120):
        assert payload.get("stream") is True
        yield {
            "choices": [
                {"delta": {"content": "Hel"}, "finish_reason": None}
            ]
        }
        yield {
            "choices": [
                {"delta": {"content": "lo"}, "finish_reason": None}
            ]
        }
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }

    with patch("reidx.provider.openai.iter_sse_json", side_effect=_fake_sse):
        resp = provider.chat_stream(
            [Message(role="user", content="hi")],
            on_text_delta=deltas.append,
        )
    assert resp.text == "Hello"
    assert deltas == ["Hel", "lo"]
    assert resp.usage.completion_tokens == 2
    assert provider.supports_streaming is True


def test_openai_chat_stream_tool_calls() -> None:
    provider = OpenAICompatibleProvider(api_key="k", base_url="https://example.com/v1")

    def _fake_sse(url, payload, headers, timeout=120):
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "list_dir", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        }
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": "{}"}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    with patch("reidx.provider.openai.iter_sse_json", side_effect=_fake_sse):
        resp = provider.chat_stream([Message(role="user", content="list")])
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "list_dir"
    assert resp.tool_calls[0].arguments == {}


def test_agent_stream_auto_uses_stream_when_supported(tmp_path: Path) -> None:
    class _StreamStub(StubProvider):
        supports_streaming = True
        streamed = False

        def chat_stream(self, messages, tools=None, model=None, *, on_text_delta=None):
            self.streamed = True
            if on_text_delta:
                on_text_delta("chunk")
            return ProviderResponse(text="chunk-done")

        def chat(self, messages, tools=None, model=None):
            raise AssertionError("should use chat_stream in auto mode")

    cfg = default_config()
    cfg.workspace_root = tmp_path
    agent = Agent(_StreamStub(), default_registry(), PolicyEngine(cfg))
    state = RuntimeState(
        session=Session(workspace=tmp_path, provider="stub", model="stub", stream="auto")
    )
    got: list[str] = []
    text, tools = agent.run_turn(state, "hi", on_text_delta=got.append)
    assert text == "chunk-done"
    assert got == ["chunk"]
    assert tools == []


def test_agent_stream_off_uses_chat(tmp_path: Path) -> None:
    class _StreamStub(StubProvider):
        supports_streaming = True

        def chat_stream(self, *a, **k):
            raise AssertionError("stream off must not call chat_stream")

    cfg = default_config()
    cfg.workspace_root = tmp_path
    agent = Agent(_StreamStub(), default_registry(), PolicyEngine(cfg))
    state = RuntimeState(
        session=Session(workspace=tmp_path, provider="stub", model="stub", stream="off")
    )
    text, _ = agent.run_turn(state, "hello there")
    assert "hello there" in text or "Offline" in text or "stub" in text.lower()


def test_session_stream_default_auto() -> None:
    s = Session(workspace=Path("."))
    assert s.stream == "auto"
