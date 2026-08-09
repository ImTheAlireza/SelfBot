"""Tests for the OpenRouter-backed GPT command."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from conftest import FakeEvent
from selfbot.config import OpenRouterConfig
from selfbot.errors import ProviderError
from selfbot.plugins.ai import (
    OPENROUTER_CHAT_URL,
    _extract_answer,
    _format_openrouter_error,
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
async def test_gpt_sends_prompt_to_openrouter(bot, registry, config):
    bot.config = replace(
        config,
        openrouter=OpenRouterConfig(
            api_key="test-openrouter-key",
            model="~openai/gpt-latest",
        ),
    )
    bot.http = StubHttp({"choices": [{"message": {"role": "assistant", "content": "Forty-two."}}]})
    event = FakeEvent(raw_text="gpt What is the meaning of life?")

    assert await registry.dispatch(bot, event, event.raw_text)

    assert event.replies == ["🤖 Thinking…", "Forty-two."]
    method, url, kwargs = bot.http.calls[0]
    assert method == "POST"
    assert url == OPENROUTER_CHAT_URL
    assert kwargs["headers"]["Authorization"] == "Bearer test-openrouter-key"
    assert kwargs["headers"]["X-OpenRouter-Title"] == "SelfBot"
    assert kwargs["json"] == {
        "model": "~openai/gpt-latest",
        "messages": [{"role": "user", "content": "What is the meaning of life?"}],
        "stream": False,
    }
    assert kwargs["timeout"] == 120
    assert kwargs["retries"] == 0


@pytest.mark.asyncio
async def test_gpt_uses_configured_model(bot, registry, config):
    bot.config = replace(
        config,
        openrouter=OpenRouterConfig(
            api_key="key",
            model="openai/gpt-5-mini",
        ),
    )
    bot.http = StubHttp({"choices": [{"message": {"content": "Hi"}}]})
    event = FakeEvent(raw_text="gpt   Keep   these spaces")

    await registry.dispatch(bot, event, event.raw_text)

    request = bot.http.calls[0][2]["json"]
    assert request["model"] == "openai/gpt-5-mini"
    assert request["messages"][0]["content"] == "Keep   these spaces"


@pytest.mark.asyncio
async def test_gpt_explains_how_to_enable_openrouter(bot, registry):
    event = FakeEvent(raw_text="gpt hello")

    await registry.dispatch(bot, event, event.raw_text)

    assert len(event.replies) == 1
    assert "OPENROUTER_API_KEY" in event.replies[0]
    assert "restart" in event.replies[0]


@pytest.mark.asyncio
async def test_gpt_requires_a_prompt(bot, registry):
    event = FakeEvent(raw_text="gpt")

    await registry.dispatch(bot, event, event.raw_text)

    assert any("gpt <prompt>" in reply for reply in event.replies)


@pytest.mark.asyncio
async def test_gpt_surfaces_openrouter_credit_error(bot, registry, config):
    bot.config = replace(
        config,
        openrouter=OpenRouterConfig(api_key="key"),
    )
    bot.http = StubHttp(
        {"error": {"code": 402, "message": "Insufficient credits"}},
        status=402,
    )
    event = FakeEvent(raw_text="gpt hello")

    await registry.dispatch(bot, event, event.raw_text)

    assert event.replies[0] == "🤖 Thinking…"
    assert "insufficient credits" in event.replies[-1].lower()


@pytest.mark.asyncio
async def test_gpt_handles_error_embedded_in_http_200(bot, registry, config):
    bot.config = replace(
        config,
        openrouter=OpenRouterConfig(api_key="key"),
    )
    bot.http = StubHttp(
        {"error": {"code": 503, "message": "No available providers"}},
        status=200,
    )
    event = FakeEvent(raw_text="gpt hello")

    await registry.dispatch(bot, event, event.raw_text)

    assert "no available provider" in event.replies[-1].lower()


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
    "payload",
    [None, {}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {}}]}],
)
def test_extract_answer_rejects_malformed_or_empty_responses(payload):
    with pytest.raises(ProviderError):
        _extract_answer(payload)


def test_openrouter_errors_are_truncated():
    message = _format_openrouter_error(
        {"error": {"code": 400, "message": "x" * 1000}},
        400,
    )
    assert len(message) < 400
    assert message.endswith("…")
