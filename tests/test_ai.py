"""Tests for the RapidAPI-backed GPT command."""

from __future__ import annotations

from typing import Any

import pytest

from conftest import FakeEvent
from selfbot.errors import ProviderError
from selfbot.plugins.ai import (
    RAPIDAPI_CHAT_URL,
    RAPIDAPI_HOST,
    RAPIDAPI_KEY,
    RAPIDAPI_MODEL,
    SYSTEM_PROMPT,
    _extract_answer,
    _format_rapidapi_error,
)


class StubResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    async def json(self, *, content_type: Any = None) -> Any:
        return self.payload


class StubHttp:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self.response = StubResponse(payload, status)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append((method, url, kwargs))
        return self.response


@pytest.mark.asyncio
async def test_gpt_sends_prompt_to_rapidapi(bot, registry):
    bot.http = StubHttp(
        {"choices": [{"message": {"role": "assistant", "content": "Forty-two."}}]}
    )
    event = FakeEvent(raw_text="gpt What is the meaning of life?")

    assert await registry.dispatch(bot, event, event.raw_text)

    assert event.replies == ["🤖 Thinking…", "Forty-two."]
    method, url, kwargs = bot.http.calls[0]
    assert method == "POST"
    assert url == RAPIDAPI_CHAT_URL
    assert kwargs["headers"] == {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
        "Content-Type": "application/json",
    }
    assert RAPIDAPI_MODEL == "GPT_5_4_high"
    assert kwargs["json"] == {
        "model": RAPIDAPI_MODEL,
        "temperature": 1,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "What is the meaning of life?"},
        ],
    }
    assert kwargs["timeout"] == 120
    assert kwargs["retries"] == 0


@pytest.mark.asyncio
async def test_gpt_preserves_prompt_spacing_without_environment_config(bot, registry):
    bot.http = StubHttp({"response": "Hi"})
    event = FakeEvent(raw_text="gpt   Keep   these spaces")

    await registry.dispatch(bot, event, event.raw_text)

    request = bot.http.calls[0][2]["json"]
    assert request["messages"][-1]["content"] == "Keep   these spaces"
    assert event.replies[-1] == "Hi"


@pytest.mark.asyncio
async def test_gpt_requires_a_prompt(bot, registry):
    event = FakeEvent(raw_text="gpt")

    await registry.dispatch(bot, event, event.raw_text)

    assert any("gpt <prompt>" in reply for reply in event.replies)


@pytest.mark.asyncio
async def test_gpt_surfaces_rapidapi_quota_error(bot, registry):
    bot.http = StubHttp(
        {"message": "You have exceeded the rate limit"},
        status=429,
    )
    event = FakeEvent(raw_text="gpt hello")

    await registry.dispatch(bot, event, event.raw_text)

    assert event.replies[0] == "🤖 Thinking…"
    assert "quota or rate limit" in event.replies[-1].lower()


@pytest.mark.asyncio
async def test_gpt_handles_error_embedded_in_http_200(bot, registry):
    bot.http = StubHttp(
        {"error": {"message": "Upstream model failed"}},
        status=200,
    )
    event = FakeEvent(raw_text="gpt hello")

    await registry.dispatch(bot, event, event.raw_text)

    assert "upstream model failed" in event.replies[-1].lower()


def test_extract_answer_supports_text_content_parts():
    payload = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "First"},
                        {"type": "text", "text": "Second"},
                    ]
                }
            }
        ]
    }
    assert _extract_answer(payload) == "First\nSecond"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"response": "Response text"}, "Response text"),
        ({"answer": "Answer text"}, "Answer text"),
        ({"data": {"content": "Nested text"}}, "Nested text"),
        ("Plain text", "Plain text"),
    ],
)
def test_extract_answer_supports_common_rapidapi_shapes(payload, expected):
    assert _extract_answer(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {}}]}],
)
def test_extract_answer_rejects_malformed_or_empty_responses(payload):
    with pytest.raises(ProviderError):
        _extract_answer(payload)


def test_rapidapi_errors_are_truncated():
    message = _format_rapidapi_error(
        {"error": {"message": "x" * 1000}},
        400,
    )
    assert len(message) < 400
    assert message.endswith("…")
