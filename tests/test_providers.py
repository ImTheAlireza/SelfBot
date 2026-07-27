"""AI provider wiring: endpoints, auth headers and graceful degradation."""

from __future__ import annotations

import pytest

import selfbot.services.ai as ai_module
from selfbot.config import AIConfig, ImageConfig
from selfbot.errors import FeatureDisabledError, ProviderError
from selfbot.services.ai import (
    AGENTROUTER_BASE_URL,
    ChatMessage,
    build_image_provider,
    build_provider,
)


class RecordingClient:
    """Captures the outbound request instead of performing it."""

    def __init__(self, response=None):
        self.response = response or {"choices": [{"message": {"content": "ok"}}]}
        self.calls: list[dict] = []

    async def post_json(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response

    @property
    def last(self) -> dict:
        return self.calls[-1]


@pytest.fixture
def client(monkeypatch):
    recorder = RecordingClient()
    monkeypatch.setattr(ai_module, "get_client", lambda: recorder)
    return recorder


def chat_config(provider: str, **overrides) -> AIConfig:
    defaults = {
        "provider": provider,
        "api_key": "sk-test",
        "base_url": "",
        "model": "test-model",
        "reasoning_model": "test-reasoning",
        "max_tokens": 256,
        "timeout": 30,
    }
    defaults.update(overrides)
    return AIConfig(**defaults)


# ---------------------------------------------------------------------------
# AgentRouter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agentrouter_uses_documented_endpoint(client):
    provider = build_provider(chat_config("agentrouter"))
    assert provider.name == "agentrouter"

    await provider.complete([ChatMessage("user", "hi")])
    assert client.last["url"] == f"{AGENTROUTER_BASE_URL}/chat/completions"


@pytest.mark.asyncio
async def test_agentrouter_sends_bearer_auth(client):
    await build_provider(chat_config("agentrouter")).complete([ChatMessage("user", "hi")])
    assert client.last["headers"]["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_agentrouter_identifies_the_client(client):
    """The gateway rejects callers it cannot identify, so send attribution."""
    await build_provider(chat_config("agentrouter")).complete([ChatMessage("user", "hi")])
    headers = client.last["headers"]
    assert headers["X-Title"] == "SelfBot"
    assert "HTTP-Referer" in headers


@pytest.mark.asyncio
async def test_agentrouter_passes_model_through_verbatim(client):
    """It is a passthrough router: model slugs must not be rewritten."""
    config = chat_config("agentrouter", model="claude-sonnet-4-5-20250929")
    await build_provider(config).complete([ChatMessage("user", "hi")])
    assert client.last["json"]["model"] == "claude-sonnet-4-5-20250929"


@pytest.mark.asyncio
async def test_agentrouter_respects_custom_base_url(client):
    config = chat_config("agentrouter", base_url="https://mirror.example/v1")
    await build_provider(config).complete([ChatMessage("user", "hi")])
    assert client.last["url"] == "https://mirror.example/v1/chat/completions"


@pytest.mark.asyncio
async def test_agentrouter_reasoning_model_is_selectable(client):
    config = chat_config("agentrouter")
    await build_provider(config).complete(
        [ChatMessage("user", "hi")], model=config.reasoning_model
    )
    assert client.last["json"]["model"] == "test-reasoning"


@pytest.mark.asyncio
async def test_agentrouter_image_endpoint(client):
    client.response = {"data": [{"url": "https://img.example/a.png"}]}
    provider = build_image_provider(
        ImageConfig(provider="agentrouter", api_key="sk-test", model="dall-e-3")
    )
    assert provider.name == "agentrouter"

    result = await provider.generate("a cat")
    assert client.last["url"] == f"{AGENTROUTER_BASE_URL}/images/generations"
    assert result == "https://img.example/a.png"


def test_agentrouter_is_an_accepted_config_value(monkeypatch, tmp_path):
    """The value that originally raised a ConfigError must now load."""
    from selfbot.config import load_config

    for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "SUDO_USER_ID", "AI_PROVIDER"):
        monkeypatch.delenv(key, raising=False)

    env = tmp_path / ".env"
    env.write_text(
        "TELEGRAM_API_ID=1\nTELEGRAM_API_HASH=h\nSUDO_USER_ID=2\n"
        "AI_PROVIDER=agentrouter\nAI_API_KEY=sk-test\n"
    )
    config = load_config(env_file=env)
    assert config.ai.provider == "agentrouter"
    assert config.ai.enabled


# ---------------------------------------------------------------------------
# Other providers keep working
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_endpoint_unchanged(client):
    await build_provider(chat_config("openai")).complete([ChatMessage("user", "hi")])
    assert client.last["url"] == "https://api.openai.com/v1/chat/completions"
    # Router attribution headers must not leak to OpenAI proper.
    assert "X-Title" not in client.last["headers"]


@pytest.mark.asyncio
async def test_openrouter_web_search_suffix(client):
    provider = build_provider(chat_config("openrouter"))
    await provider.complete([ChatMessage("user", "hi")], web_search=True)
    assert client.last["json"]["model"].endswith(":online")


@pytest.mark.asyncio
async def test_anthropic_splits_system_prompt(client):
    client.response = {"content": [{"type": "text", "text": "hello"}]}
    provider = build_provider(chat_config("anthropic"))
    result = await provider.complete(
        [ChatMessage("system", "be terse"), ChatMessage("user", "hi")]
    )
    assert client.last["json"]["system"] == "be terse"
    assert [m["role"] for m in client.last["json"]["messages"]] == ["user"]
    assert result == "hello"


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_without_key_is_disabled():
    provider = build_provider(chat_config("agentrouter", api_key=""))
    assert provider.name == "none"
    with pytest.raises(FeatureDisabledError, match="AI_API_KEY"):
        await provider.complete([ChatMessage("user", "hi")])


@pytest.mark.asyncio
async def test_malformed_response_raises_provider_error(client):
    client.response = {"error": {"message": "insufficient credits"}}
    provider = build_provider(chat_config("agentrouter"))
    with pytest.raises(ProviderError, match="insufficient credits"):
        await provider.complete([ChatMessage("user", "hi")])


@pytest.mark.asyncio
async def test_image_provider_without_key_is_disabled():
    provider = build_image_provider(
        ImageConfig(provider="agentrouter", api_key="", model="m")
    )
    with pytest.raises(FeatureDisabledError):
        await provider.generate("x")
