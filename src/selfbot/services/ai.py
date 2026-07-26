"""Pluggable AI providers.

The original bot hardwired three RapidAPI endpoints. If that key expired or the
endpoint changed shape, ``gpt``/``gpts``/``gptr`` all broke with an opaque
error. Here each backend implements a small interface, is chosen by
``AI_PROVIDER``, and reports a clear "not configured" message when unset.

Supported: ``openai`` (and any OpenAI-compatible gateway via ``AI_BASE_URL`` —
OpenRouter, Together, Groq, LM Studio, Ollama, vLLM), ``anthropic``, and the
legacy ``rapidapi`` endpoints.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..config import AIConfig, ImageConfig
from ..errors import FeatureDisabledError, ProviderError
from ..utils.http import get_client

logger = logging.getLogger(__name__)

__all__ = ["AIProviderBase", "ChatMessage", "build_image_provider", "build_provider"]


@dataclass(slots=True)
class ChatMessage:
    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class AIProviderBase(ABC):
    """Interface every chat backend implements."""

    name: str = "unknown"

    @abstractmethod
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        web_search: bool = False,
    ) -> str:
        """Return the assistant's reply text."""


class DisabledProvider(AIProviderBase):
    """Stand-in used when no provider is configured."""

    name = "none"

    def __init__(self, reason: str = "") -> None:
        self._reason = reason

    async def complete(self, *_args: Any, **_kwargs: Any) -> str:
        raise FeatureDisabledError(
            self._reason
            or "AI is not configured. Set `AI_PROVIDER` and `AI_API_KEY` in your .env "
            "(supports openai, openrouter, anthropic, or a local OpenAI-compatible server)."
        )


class OpenAICompatibleProvider(AIProviderBase):
    """Works with OpenAI and every OpenAI-compatible gateway."""

    name = "openai"
    default_base_url = "https://api.openai.com/v1"

    def __init__(self, config: AIConfig, *, base_url: str | None = None, name: str | None = None) -> None:
        self._config = config
        self._base_url = (base_url or config.base_url or self.default_base_url).rstrip("/")
        if name:
            self.name = name

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        web_search: bool = False,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model or self._config.model,
            "messages": [m.to_dict() for m in messages],
            "max_tokens": max_tokens or self._config.max_tokens,
        }
        # OpenRouter exposes web search by appending :online to the model slug.
        if web_search and "openrouter" in self._base_url:
            payload["model"] = f"{payload['model']}:online"

        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        if "openrouter" in self._base_url:
            headers["HTTP-Referer"] = "https://github.com/ImTheAlireza/SelfBot"
            headers["X-Title"] = "SelfBot"

        data = await get_client().post_json(
            f"{self._base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=self._config.timeout,
        )

        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError, TypeError) as exc:
            error = _extract_error(data)
            raise ProviderError(error or f"Unexpected response shape from {self.name}") from exc


class AnthropicProvider(AIProviderBase):
    """Anthropic Messages API."""

    name = "anthropic"
    base_url = "https://api.anthropic.com/v1"

    def __init__(self, config: AIConfig) -> None:
        self._config = config

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        web_search: bool = False,
    ) -> str:
        system_parts = [m.content for m in messages if m.role == "system"]
        chat = [m.to_dict() for m in messages if m.role != "system"]

        payload: dict[str, Any] = {
            "model": model or self._config.model,
            "messages": chat,
            "max_tokens": max_tokens or self._config.max_tokens,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)

        data = await get_client().post_json(
            f"{(self._config.base_url or self.base_url).rstrip('/')}/messages",
            json=payload,
            headers={
                "x-api-key": self._config.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            timeout=self._config.timeout,
        )

        try:
            blocks = data["content"]
            return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        except (KeyError, TypeError) as exc:
            error = _extract_error(data)
            raise ProviderError(error or "Unexpected response shape from Anthropic") from exc


class RapidAPIProvider(AIProviderBase):
    """The original chatgpt-42 RapidAPI endpoints, kept for compatibility."""

    name = "rapidapi"
    chat_url = "https://chatgpt-42.p.rapidapi.com/gpt4"
    reasoning_url = "https://chatgpt-42.p.rapidapi.com/o3mini"

    def __init__(self, config: AIConfig) -> None:
        self._config = config

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        web_search: bool = False,
    ) -> str:
        url = self.reasoning_url if model == "reasoning" else self.chat_url
        data = await get_client().post_json(
            url,
            json={
                "messages": [m.to_dict() for m in messages],
                "web_access": web_search,
            },
            headers={
                "x-rapidapi-key": self._config.api_key,
                "x-rapidapi-host": "chatgpt-42.p.rapidapi.com",
                "Content-Type": "application/json",
            },
            timeout=self._config.timeout,
        )

        result = data.get("result") if isinstance(data, dict) else None
        if not result:
            raise ProviderError(_extract_error(data) or "RapidAPI returned no result.")
        return str(result).strip()


def build_provider(config: AIConfig) -> AIProviderBase:
    """Instantiate the configured chat provider."""
    if config.provider == "none":
        return DisabledProvider()

    if not config.api_key and not config.base_url:
        return DisabledProvider(
            f"`AI_PROVIDER` is set to `{config.provider}` but `AI_API_KEY` is empty."
        )

    if config.provider == "openai":
        return OpenAICompatibleProvider(config)
    if config.provider == "openrouter":
        return OpenAICompatibleProvider(
            config,
            base_url=config.base_url or "https://openrouter.ai/api/v1",
            name="openrouter",
        )
    if config.provider == "anthropic":
        return AnthropicProvider(config)
    if config.provider == "rapidapi":
        return RapidAPIProvider(config)
    return DisabledProvider(f"Unknown AI provider: {config.provider}")


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------


class ImageProviderBase(ABC):
    name = "unknown"

    @abstractmethod
    async def generate(self, prompt: str, *, size: str = "1024x1024") -> bytes | str:
        """Return raw image bytes, or a URL string."""


class DisabledImageProvider(ImageProviderBase):
    name = "none"

    async def generate(self, prompt: str, *, size: str = "1024x1024") -> bytes | str:
        raise FeatureDisabledError(
            "Image generation is not configured. Set `IMAGE_PROVIDER` and "
            "`IMAGE_API_KEY` in your .env."
        )


class OpenAIImageProvider(ImageProviderBase):
    name = "openai"

    def __init__(self, config: ImageConfig) -> None:
        self._config = config

    async def generate(self, prompt: str, *, size: str = "1024x1024") -> bytes | str:
        import base64

        data = await get_client().post_json(
            "https://api.openai.com/v1/images/generations",
            json={"model": self._config.model, "prompt": prompt, "size": size, "n": 1},
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            },
            timeout=180,
        )
        try:
            entry = data["data"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(_extract_error(data) or "No image returned.") from exc

        if entry.get("b64_json"):
            return base64.b64decode(entry["b64_json"])
        if entry.get("url"):
            return str(entry["url"])
        raise ProviderError("Image response contained neither data nor a URL.")


class RapidAPIImageProvider(ImageProviderBase):
    name = "rapidapi"
    url = "https://open-ai21.p.rapidapi.com/texttoimage2"

    def __init__(self, config: ImageConfig) -> None:
        self._config = config

    async def generate(self, prompt: str, *, size: str = "1024x1024") -> bytes | str:
        data = await get_client().post_json(
            self.url,
            json={"text": prompt},
            headers={
                "x-rapidapi-key": self._config.api_key,
                "x-rapidapi-host": "open-ai21.p.rapidapi.com",
                "Content-Type": "application/json",
            },
            timeout=180,
        )
        url = data.get("generated_image") if isinstance(data, dict) else None
        if not url:
            raise ProviderError(_extract_error(data) or "No image returned.")
        return str(url)


def build_image_provider(config: ImageConfig) -> ImageProviderBase:
    if config.provider == "none" or not config.api_key:
        return DisabledImageProvider()
    if config.provider == "openai":
        return OpenAIImageProvider(config)
    if config.provider == "rapidapi":
        return RapidAPIImageProvider(config)
    return DisabledImageProvider()


def _extract_error(data: Any) -> str | None:
    """Pull a human-readable error out of a provider's error envelope."""
    if not isinstance(data, dict):
        return None
    error = data.get("error") or data.get("message") or data.get("detail")
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error) if error else None
