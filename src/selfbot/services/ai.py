"""AI provider service.

The :class:`AIManager` owns everything the ``gpt`` family of commands needs:

* database-backed providers (OpenAI-compatible and legacy RapidAPI),
* ordered fallback across enabled providers,
* automatic cooldown after quota/rate-limit errors,
* success/failure counters persisted per provider,
* per-chat conversation memory (rolled out from here in phase 3),
* live ``/models`` discovery for ``ai model list``.

The low-level HTTP layer handles both ordinary JSON completions and OpenAI-style
SSE streams while keeping provider errors, fallback and cooldown behaviour
consistent across gateways.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..config import AIConfig
from ..db import AIProvider, utcnow
from ..errors import FeatureDisabledError, ProviderError

__all__ = [
    "ANYAPI_DEFAULT_BASE_URL",
    "ANYAPI_DEFAULT_MODEL",
    "BACKUP_RAPIDAPI_CHAT_URL",
    "BACKUP_RAPIDAPI_GENERE",
    "BACKUP_RAPIDAPI_HOST",
    "BLUESMINDS_DEFAULT_BASE_URL",
    "RAPIDAPI_CHAT_URL",
    "RAPIDAPI_HOST",
    "RAPIDAPI_MODEL",
    "SYSTEM_PROMPT",
    "AIImage",
    "AIManager",
    "CompletionResult",
    "ProviderStatus",
    "extract_stream_answer",
    "seed_providers_from_env",
]

logger = logging.getLogger(__name__)

CATEGORY = "AI"
SYSTEM_PROMPT = "Format your replies with markdown when applicable"

ANYAPI_DEFAULT_BASE_URL = "https://api.anyapi.ai/v1"
ANYAPI_DEFAULT_MODEL = "anthropic/claude-sonnet-5"
BLUESMINDS_DEFAULT_BASE_URL = "https://api.bluesminds.com/v1"

RAPIDAPI_HOST = "chatgpt-api8.p.rapidapi.com"
RAPIDAPI_CHAT_URL = f"https://{RAPIDAPI_HOST}/chato"
RAPIDAPI_MODEL = "GPT_5_4_high"

BACKUP_RAPIDAPI_HOST = "adult-gpt.p.rapidapi.com"
BACKUP_RAPIDAPI_CHAT_URL = f"https://{BACKUP_RAPIDAPI_HOST}/adultgpt"
BACKUP_RAPIDAPI_GENERE = "ai-gf-1"

#: HTTP statuses that mean "try the next provider" instead of "user error".
_FALLBACK_STATUSES = frozenset({408, 429, 502, 503, 504})
_MODELS_CACHE_TTL = 600.0


class ProviderStatusError(ProviderError):
    """ProviderError that carries the HTTP status for fallback decisions."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class AIImage:
    """One prepared image attachment for an OpenAI multimodal message."""

    data: bytes
    mime_type: str

    def content_part(self) -> dict[str, Any]:
        encoded = base64.b64encode(self.data).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{self.mime_type};base64,{encoded}"},
        }


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """A completion plus the model identifier reported by the API response."""

    text: str
    reported_model: str = ""


@dataclass(slots=True)
class ProviderStatus:
    provider: AIProvider
    cooldown_remaining: int
    available: bool


# --------------------------------------------------------------------------
# Low-level HTTP calls (kept compatible with the original implementation)
# --------------------------------------------------------------------------


async def openai_completion(
    http: Any,
    *,
    messages: Sequence[Mapping[str, Any]],
    api_key: str,
    base_url: str,
    model: str,
    timeout: float = 120.0,
    temperature: float = 0.7,
    max_tokens: int = 1000,
) -> CompletionResult:
    """Request an OpenAI-compatible completion, accepting JSON or SSE.

    Streaming is requested because some compatible gateways document that as
    their primary mode. The shared HTTP client buffers the finite response and
    this function joins the SSE deltas before returning the final Telegram
    message. If a gateway explicitly rejects the ``stream`` parameter, one
    safe non-stream retry is made after the 4xx response.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
    }
    body = {
        "model": model,
        "messages": list(messages),
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    for stream in (True, False):
        body["stream"] = stream
        response = await http.request(
            "POST",
            url,
            headers=headers,
            json=dict(body),
            timeout=timeout,
            # A retry after an uncertain timeout can duplicate a billable request.
            retries=0,
        )
        try:
            return await _openai_response_result(response)
        except ProviderStatusError as exc:
            # Retry only when the server rejected streaming before generating a
            # completion. Authentication, quota and upstream errors must not be
            # repeated, and a malformed successful stream may already be billed.
            if stream and _stream_parameter_rejected(exc):
                logger.info("Provider rejected SSE streaming; retrying as JSON")
                continue
            raise

    raise ProviderError("AI provider did not return a completion.")


async def _openai_response_result(response: Any) -> CompletionResult:
    """Decode one OpenAI response without trusting its Content-Type header."""
    raw = ""
    try:
        payload = await response.json(content_type=None)
    except Exception:
        try:
            raw = await response.text()
        except Exception:
            raw = ""
        payload = _json_body(raw)

    status = int(getattr(response, "status", 0) or 0)
    if status >= 400:
        error_payload = payload if payload is not None else raw
        raise ProviderStatusError(
            format_provider_error(error_payload, status, "AI provider"), status
        )

    if isinstance(payload, Mapping) and payload.get("error"):
        raise ProviderStatusError(
            format_provider_error(payload, status or 400, "AI provider"),
            status or 400,
        )

    # Some test doubles and permissive gateways return the SSE body as a JSON
    # string. Detect it before treating it as an ordinary plain-text answer.
    if isinstance(payload, str) and _looks_like_event_stream(payload):
        return _extract_stream_result(payload)
    if payload is not None:
        return CompletionResult(
            extract_answer(payload, "AI provider"),
            _response_model(payload),
        )
    if _looks_like_event_stream(raw):
        return _extract_stream_result(raw)

    preview = raw.strip()[:160]
    detail = f": {preview}" if preview else ""
    raise ProviderStatusError(
        f"AI provider sent an invalid response (HTTP {status}){detail}.",
        status,
    )


def _json_body(raw: str) -> Any | None:
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _response_model(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    model = payload.get("model")
    return model.strip() if isinstance(model, str) else ""


def _looks_like_event_stream(raw: str) -> bool:
    stripped = raw.lstrip("\ufeff \t\r\n")
    return stripped.startswith(("data:", "event:", ":"))


def _stream_parameter_rejected(exc: ProviderStatusError) -> bool:
    if exc.status not in {400, 415, 422}:
        return False
    message = str(exc).lower()
    return "stream" in message and any(
        word in message
        for word in ("unsupported", "unknown", "invalid", "not allowed", "unrecognized")
    )


def extract_stream_answer(raw: str, provider: str = "AI provider") -> str:
    """Join and return text deltas from an OpenAI-compatible SSE response."""
    return _extract_stream_result(raw, provider).text


def _extract_stream_result(
    raw: str, provider: str = "AI provider"
) -> CompletionResult:
    """Decode SSE text while retaining its API-reported model identifier.

    Handles standard ``data: {...}`` server-sent events, multi-line SSE data,
    comment/metadata fields and newline-delimited JSON used by a few gateways.
    Whitespace inside deltas is preserved so ``"Hello"`` + ``" world"`` does
    not accidentally become ``"Helloworld"``.
    """
    pieces: list[str] = []
    data_lines: list[str] = []
    reported_model = ""

    def consume(data: str) -> bool:
        nonlocal reported_model
        data = data.strip("\r\n")
        if not data:
            return False
        if data.strip() == "[DONE]":
            return True
        try:
            chunk = json.loads(data)
        except (TypeError, ValueError):
            # A few proxies emit a plain text delta after ``data:``.
            pieces.append(data)
            return False
        if not isinstance(chunk, Mapping):
            return False
        if chunk.get("error"):
            raise ProviderError(format_provider_error(chunk, 400, provider))
        reported_model = _response_model(chunk) or reported_model
        piece = _stream_chunk_text(chunk)
        if piece:
            pieces.append(piece)
        return False

    for raw_line in raw.lstrip("\ufeff").splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines and consume("\n".join(data_lines)):
                break
            data_lines.clear()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
            continue
        if line.startswith(("event:", "id:", "retry:", ":")):
            continue
        # Tolerate NDJSON in addition to strict SSE.
        if (
            not data_lines
            and line.lstrip().startswith(("{", "["))
            and consume(line.strip())
        ):
            break

    if data_lines:
        consume("\n".join(data_lines))

    answer = "".join(pieces).strip()
    if not answer:
        raise ProviderError(f"{provider} returned an empty streamed completion.")
    return CompletionResult(answer, reported_model)


def _stream_chunk_text(chunk: Mapping[str, Any]) -> str:
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, Mapping):
        return ""

    delta = choice.get("delta")
    if isinstance(delta, Mapping):
        return _raw_content_text(delta.get("content"))
    message = choice.get("message")
    if isinstance(message, Mapping):
        return _raw_content_text(message.get("content"))
    text = choice.get("text")
    return text if isinstance(text, str) else ""


def _raw_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces: list[str] = []
    for part in content:
        if isinstance(part, str):
            pieces.append(part)
        elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
            pieces.append(part["text"])
    return "".join(pieces)


async def rapidapi_completion(http: Any, *, prompt: str, api_key: str) -> str:
    """Request a completion from the primary RapidAPI host, with backup."""
    response = await http.request(
        "POST",
        RAPIDAPI_CHAT_URL,
        headers={
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": RAPIDAPI_HOST,
            "Content-Type": "application/json",
        },
        json={
            "model": RAPIDAPI_MODEL,
            "temperature": 1,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=120,
        retries=0,
    )

    try:
        payload = await response.json(content_type=None)
    except Exception as exc:
        if response.status in _FALLBACK_STATUSES:
            logger.warning("Primary RapidAPI quota reached; trying the backup API")
            return await backup_rapidapi_completion(http, prompt=prompt, api_key=api_key)
        raise ProviderError(
            f"RapidAPI sent an invalid response (HTTP {response.status})."
        ) from exc

    if response.status in _FALLBACK_STATUSES:
        logger.warning("Primary RapidAPI quota reached; trying the backup API")
        return await backup_rapidapi_completion(http, prompt=prompt, api_key=api_key)

    if response.status >= 400:
        raise ProviderError(format_provider_error(payload, response.status, "RapidAPI"))

    if isinstance(payload, Mapping) and payload.get("error"):
        raise ProviderError(format_provider_error(payload, response.status, "RapidAPI"))

    return extract_answer(payload, "RapidAPI")


async def backup_rapidapi_completion(
    http: Any, *, prompt: str, api_key: str
) -> str:
    """Use Adult GPT when the primary RapidAPI plan returns an error status."""
    response = await http.request(
        "POST",
        BACKUP_RAPIDAPI_CHAT_URL,
        headers={
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": BACKUP_RAPIDAPI_HOST,
            "Content-Type": "application/json",
        },
        json={
            "messages": [{"role": "user", "content": prompt}],
            "genere": BACKUP_RAPIDAPI_GENERE,
            "bot_name": "",
            "temperature": 0.9,
            "top_k": 10,
            "top_p": 0.9,
            "max_tokens": 200,
        },
        timeout=120,
        retries=0,
    )

    try:
        payload = await response.json(content_type=None)
    except Exception as exc:
        raise ProviderError(
            f"Backup RapidAPI sent an invalid response (HTTP {response.status})."
        ) from exc

    if response.status >= 400:
        raise ProviderError(format_provider_error(payload, response.status, "RapidAPI"))

    if isinstance(payload, Mapping) and payload.get("error"):
        raise ProviderError(format_provider_error(payload, response.status, "RapidAPI"))

    return extract_answer(payload, "RapidAPI")


# --------------------------------------------------------------------------
# Response parsing & error formatting
# --------------------------------------------------------------------------


def extract_answer(payload: Any, provider: str = "AI provider") -> str:
    """Extract text from common chat-completion response shapes."""
    if isinstance(payload, str):
        answer = payload.strip()
        if answer:
            return answer
        raise ProviderError(f"{provider} returned an empty completion.")

    if not isinstance(payload, Mapping):
        raise ProviderError(f"{provider} returned an unexpected response.")

    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        choice = choices[0]
        message = choice.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        answer = content_text(content)
        if not answer and isinstance(choice.get("text"), str):
            answer = choice["text"].strip()
        if answer:
            return answer

    message = payload.get("message")
    if isinstance(message, Mapping):
        answer = content_text(message.get("content"))
        if answer:
            return answer
    elif isinstance(message, str) and message.strip():
        return message.strip()

    for key in ("response", "content", "text", "answer", "result"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    data = payload.get("data")
    if isinstance(data, (str, Mapping)):
        try:
            return extract_answer(data, provider)
        except ProviderError:
            pass

    raise ProviderError(f"{provider} returned an empty completion.")


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    pieces: list[str] = []
    for part in content:
        if isinstance(part, str):
            pieces.append(part)
        elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
            pieces.append(part["text"])
    return "\n".join(pieces).strip()


def format_provider_error(payload: Any, status: int, provider: str) -> str:
    """Turn a provider error response into a short, actionable message."""
    detail = ""
    if isinstance(payload, str):
        detail = payload.strip()
    elif isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            for key in ("message", "detail", "error"):
                if isinstance(error.get(key), str):
                    detail = error[key].strip()
                    break
        elif isinstance(error, str):
            detail = error.strip()

        if not detail:
            for key in ("message", "detail"):
                if isinstance(payload.get(key), str):
                    detail = payload[key].strip()
                    break

    friendly = {
        401: f"{provider} rejected the API key.",
        403: f"{provider} denied access. Check the subscription or model access.",
        408: f"The {provider} request timed out. Try again.",
        429: f"The {provider} quota or rate limit was reached. Try again later.",
        502: f"The AI service behind {provider} is temporarily unavailable.",
        503: f"The AI service behind {provider} is temporarily unavailable.",
    }.get(status)
    if friendly:
        return friendly

    if detail:
        from ..utils.text import truncate

        return f"{provider} error (HTTP {status}): {truncate(detail, 300)}"
    return f"{provider} returned HTTP {status}."


# Backwards-compatible alias used by older callers/tests.
def format_rapidapi_error(payload: Any, status: int) -> str:
    return format_provider_error(payload, status, "RapidAPI")


# --------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------


class AIManager:
    """Routes chat requests across the configured providers."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._cache: list[AIProvider] | None = None
        self._models_cache: dict[str, tuple[float, list[str]]] = {}

    # -- providers --------------------------------------------------------

    @property
    def db(self) -> Any:
        return self.bot.db

    @property
    def http(self) -> Any:
        return self.bot.http

    @property
    def config(self) -> AIConfig:
        return self.bot.config.ai

    def invalidate(self) -> None:
        self._cache = None

    async def providers(self, *, enabled_only: bool = False) -> list[AIProvider]:
        if self._cache is None:
            rows = await self.db.list_providers()
            if not rows:
                # No DB configuration yet — synthesise from the environment so
                # legacy deployments and the test suite keep working.
                rows = _config_providers(self.config)
            self._cache = rows
        if enabled_only:
            return [p for p in self._cache if p.enabled]
        return list(self._cache)

    async def status(self) -> list[ProviderStatus]:
        result: list[ProviderStatus] = []
        for provider in await self.providers():
            remaining = 0
            if provider.cooldown_until is not None:
                remaining = max(
                    0, int((provider.cooldown_until - utcnow()).total_seconds())
                )
            available = provider.enabled and remaining == 0 and bool(provider.api_key)
            result.append(
                ProviderStatus(
                    provider=provider,
                    cooldown_remaining=remaining,
                    available=available,
                )
            )
        return result

    async def add_openai_provider(
        self,
        name: str,
        base_url: str,
        api_key: str,
        *,
        model: str = "",
        is_default: bool = False,
    ) -> AIProvider:
        provider = await self.db.add_provider(
            name,
            base_url.rstrip("/"),
            api_key,
            model=model,
            kind="openai",
            is_default=is_default,
        )
        self.invalidate()
        return provider

    async def remove_provider(self, name: str) -> bool:
        removed = bool(await self.db.delete_provider(name))
        self.invalidate()
        return removed

    async def set_default(self, name: str) -> None:
        provider = await self.db.get_provider(name)
        if provider is None:
            raise KeyError(name)
        await self.db.set_default_provider(name)
        self.invalidate()

    async def set_provider_model(self, name: str, model: str) -> None:
        await self.db.update_provider(name, model=model)
        self.invalidate()
        self._models_cache.pop(name, None)

    async def test_provider(self, name: str) -> tuple[bool, str]:
        """Connectivity probe. Returns ``(ok, detail)``.

        Tries the configured base URL first, then common variants (``/v1``)
        so a provider added without the path suffix is detected and
        corrected automatically.
        """
        provider = await self.db.get_provider(name)
        if provider is None:
            return False, "not configured"
        if provider.kind != "openai":
            return True, "key present (live test skipped for this provider type)"
        if not provider.api_key:
            return False, "no API key"

        base = provider.base_url.rstrip("/")
        candidates = [base]
        if not base.endswith("/v1"):
            candidates.append(f"{base}/v1")

        last_error = ""
        for candidate in candidates:
            url = f"{candidate}/models"
            try:
                response = await self.http.request(
                    "GET",
                    url,
                    headers={"Authorization": f"Bearer {provider.api_key}"},
                    timeout=30,
                    retries=0,
                )
                if response.status >= 400:
                    body = (await response.text())[:160].strip()
                    last_error = f"HTTP {response.status}: {body or '(no body)'}"
                    if response.status in (404, 405) and candidate != candidates[-1]:
                        continue
                    return False, last_error
                payload = await response.json(content_type=None)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if candidate != candidates[-1]:
                    continue
                hint = ""
                if "timed out" in last_error.lower():
                    hint = (
                        " — the host did not respond. Check the base URL "
                        "(OpenAI-compatible routers usually need /v1) and "
                        "that the API is reachable from this server."
                    )
                return False, last_error + hint

            models = []
            if isinstance(payload, Mapping):
                data = payload.get("data")
                if isinstance(data, list):
                    models = [
                        str(m.get("id"))
                        for m in data
                        if isinstance(m, Mapping) and m.get("id")
                    ]
            self._models_cache[name] = (time.time(), models)

            if candidate != base:
                await self.db.update_provider(name, base_url=candidate)
                self.invalidate()
                return (
                    True,
                    f"ok - {len(models)} models. Auto-corrected base URL "
                    f"to `{candidate}` (added /v1).",
                )
            return True, f"ok - {len(models)} models available"

        return False, last_error or "unknown error"

    # -- completion -------------------------------------------------------

    async def chat(
        self,
        prompt: str,
        *,
        chat_id: int | None = None,
        history: bool = False,
        model: str | None = None,
        system: str | None = None,
        images: Sequence[AIImage] | None = None,
    ) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system or SYSTEM_PROMPT}
        ]
        used_provider = ""

        if history and chat_id is not None:
            turns = max(1, self.config.memory_turns)
            for stored in await self.db.recent_ai_messages(
                chat_id, limit=turns * 2
            ):
                messages.append({"role": stored.role, "content": stored.content})

        user_content: str | list[dict[str, Any]] = prompt
        if images:
            user_content = [
                {"type": "text", "text": prompt},
                *(image.content_part() for image in images),
            ]
        messages.append({"role": "user", "content": user_content})
        messages = _apply_budget(messages, self.config.memory_budget)

        metrics = getattr(self.bot, "metrics", None)
        if metrics is not None:
            metrics.incr("ai_requests")
        try:
            answer = await self._complete_chain(messages, model=model)
        except Exception:
            if metrics is not None:
                metrics.incr("ai_failures")
            raise
        # Determine who answered (best effort) for memory attribution.
        used_provider = self._last_provider or ""
        self._last_model = getattr(self, "_last_model", "") or model or ""

        if history and chat_id is not None:
            try:
                await self.db.add_ai_message(
                    chat_id, "user", prompt, provider=used_provider or None
                )
                await self.db.add_ai_message(
                    chat_id, "assistant", answer, provider=used_provider or None
                )
                await self.db.prune_ai_messages(
                    chat_id, keep=max(1, self.config.memory_turns) * 2
                )
            except Exception:
                logger.debug("Could not persist AI memory", exc_info=True)

        return answer

    async def _complete_chain(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> str:
        providers = await self._ordered_providers()
        if not providers:
            raise FeatureDisabledError(
                "No AI provider is configured. Add one with "
                "`provider add <name> <base_url> <api_key>` or set "
                "ANYAPI_KEY / BLUESMINDS_API_KEY / RAPIDAPI_KEY in your `.env`."
            )

        errors: list[str] = []
        self._last_provider = ""
        self._last_model = ""
        self._last_reported_model = ""

        for provider in providers:
            if not provider.api_key:
                continue
            if await self._in_cooldown(provider):
                errors.append(f"{provider.name}: cooling down")
                continue

            use_model = model or provider.model or ""
            if not use_model and provider.kind == "openai":
                use_model = ANYAPI_DEFAULT_MODEL
            try:
                result = await self._call(
                    provider, messages=messages, model=use_model
                )
            except ProviderStatusError as exc:
                await self._record_failure(provider, exc, fallback=True)
                errors.append(f"{provider.name}: {exc}")
                if exc.status in _FALLBACK_STATUSES:
                    continue
                raise
            except ProviderError as exc:
                await self._record_failure(provider, exc, fallback=False)
                errors.append(f"{provider.name}: {exc}")
                continue

            await self.db.record_provider_result(provider.name, success=True)
            self._last_provider = provider.name
            self._last_model = use_model
            self._last_reported_model = result.reported_model
            return result.text

        detail = "; ".join(errors[-3:]) if errors else "no providers available"
        raise ProviderError(f"All AI providers are unavailable ({detail}).")

    async def _ordered_providers(self) -> list[AIProvider]:
        providers = await self.providers(enabled_only=True)
        default = next((p for p in providers if p.is_default), None)
        openai = [
            p for p in providers if p.kind == "openai" and p is not default
        ]
        rapidapi = [p for p in providers if p.kind == "rapidapi"]
        ordered: list[AIProvider] = []
        if default is not None:
            ordered.append(default)
        ordered.extend(openai)
        ordered.extend(rapidapi)
        return ordered

    async def _call(
        self,
        provider: AIProvider,
        *,
        messages: list[dict[str, Any]],
        model: str,
    ) -> CompletionResult:
        if provider.kind in ("rapidapi", "rapidapi_backup"):
            if _messages_have_images(messages):
                raise ProviderError("This AI provider does not support image input.")
            prompt = next(
                (
                    _message_text(m.get("content"))
                    for m in reversed(messages)
                    if m["role"] == "user"
                ),
                "",
            )
            answer = await rapidapi_completion(
                self.http, prompt=prompt, api_key=provider.api_key
            )
            return CompletionResult(answer)
        routed_messages = _with_route_context(
            messages, provider=provider.name, model=model
        )
        return await openai_completion(
            self.http,
            messages=routed_messages,
            api_key=provider.api_key,
            base_url=provider.base_url,
            model=model,
        )

    async def _in_cooldown(self, provider: AIProvider) -> bool:
        if provider.cooldown_until is None:
            return False
        if provider.cooldown_until <= utcnow():
            await self.db.clear_provider_cooldown(provider.name)
            self.invalidate()
            return False
        return True

    async def _record_failure(
        self,
        provider: AIProvider,
        exc: Exception,
        *,
        fallback: bool,
    ) -> None:
        message = str(exc)
        metrics = getattr(self.bot, "metrics", None)
        if metrics is not None:
            status = getattr(exc, "status", None)
            metrics.record_failure(f"ai:{provider.name}", message, status=status)
        await self.db.record_provider_result(
            provider.name, success=False, error=message
        )
        if fallback:
            # A short fixed cooldown lets the provider recover without making
            # every consecutive temporary failure wait exponentially longer.
            seconds = min(
                float(self.config.cooldown_seconds),
                float(self.config.cooldown_max),
            )
            until = utcnow().timestamp() + seconds
            await self.db.set_provider_cooldown(
                provider.name,
                _dt_from_timestamp(until),
                error=message,
            )
            logger.warning(
                "AI provider %s unavailable (%s); cooling down %.0fs",
                provider.name,
                message,
                seconds,
            )
        self.invalidate()

    # -- model discovery --------------------------------------------------

    async def list_models(self) -> list[dict[str, Any]]:
        """Return merged model entries from every reachable OpenAI provider.

        Each entry is ``{"id": str, "providers": [str,...], "current": bool}``.
        Failures are per-provider and never abort the whole listing.
        """
        current, current_provider = await self.current_model()
        models: dict[str, dict[str, Any]] = {}
        for provider in await self.providers(enabled_only=True):
            if provider.kind != "openai":
                continue
            ids = await self._provider_models(provider)
            if ids is None:
                # Fall back to the provider's configured model only.
                if provider.model:
                    ids = [provider.model]
                else:
                    continue
            for mid in ids:
                entry = models.setdefault(
                    mid, {"id": mid, "providers": [], "current": False}
                )
                entry["providers"].append(provider.name)
        for entry in models.values():
            # Mark current when the id matches and the active provider serves it.
            entry["current"] = (
                entry["id"] == current and current_provider in entry["providers"]
            )
        return sorted(models.values(), key=lambda e: e["id"].lower())

    async def _provider_models(self, provider: AIProvider) -> list[str] | None:
        cached = self._models_cache.get(provider.name)
        if cached and (time.time() - cached[0]) < _MODELS_CACHE_TTL:
            return cached[1]
        ok, _ = await self.test_provider(provider.name)
        if not ok:
            return None
        cached = self._models_cache.get(provider.name)
        return cached[1] if cached else None

    async def current_model(self) -> tuple[str, str]:
        """Return ``(model, provider_name)`` the next call will actually use."""
        default = await self.db.get_default_provider()
        if default and default.model:
            return default.model, default.name
        if default:
            return f"(provider default · {default.name})", default.name
        # No DB default at all — synthesised from config.
        if self.config.anyapi_model:
            return self.config.anyapi_model, "anyapi"
        return ANYAPI_DEFAULT_MODEL, "anyapi"

    async def set_active_model(self, model: str) -> tuple[str, str]:
        """Set the model on the current default provider.

        ``provider/model`` targets a specific provider instead of the default.
        Returns ``(model, provider_name)`` actually set.
        """
        model = model.strip()
        if not model:
            raise ValueError("model must not be empty")

        provider_name: str | None = None
        if "/" in model:
            provider_name, _, model = model.partition("/")
            provider_name = provider_name.strip().lower()
            model = model.strip()
            existing = await self.db.get_provider(provider_name)
            if existing is None:
                raise KeyError(provider_name)
        else:
            default = await self.db.get_default_provider()
            if default is not None:
                provider_name = default.name

        if provider_name is None:
            # No provider configured in the DB yet — store as a setting that
            # seed_providers_from_env will apply on first connection.
            await self.db.set_setting("ai.initial_model", model)
            return model, "(pending)"

        await self.set_provider_model(provider_name, model)
        return model, provider_name


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _with_route_context(
    messages: Sequence[Mapping[str, Any]],
    *,
    provider: str,
    model: str,
) -> list[dict[str, Any]]:
    """Tell the model which API route was requested without claiming proof.

    Language models cannot reliably introspect their own deployment identity.
    This runtime context makes identity answers report the selected API model
    instead of guessing a vendor from training-time persona data.
    """
    routed = [dict(message) for message in messages]
    context = (
        "\n\nRuntime routing context: this application requested model identifier "
        f"'{model}' through provider '{provider}'. You cannot independently "
        "inspect the provider's underlying deployment. If asked which model "
        "you are, report this requested API identifier and do not guess a "
        "different vendor or model identity."
    )
    for message in routed:
        if message.get("role") == "system":
            message["content"] = message.get("content", "") + context
            break
    else:
        routed.insert(0, {"role": "system", "content": context.lstrip()})
    return routed


def _dt_from_timestamp(ts: float) -> Any:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(part.get("text", ""))
        for part in content
        if isinstance(part, Mapping) and part.get("type") == "text"
    ).strip()


def _messages_have_images(messages: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        isinstance(message.get("content"), list)
        and any(
            isinstance(part, Mapping) and part.get("type") == "image_url"
            for part in message["content"]
        )
        for message in messages
    )


def _content_budget_size(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    size = len(_message_text(content))
    # Reserve a modest token-equivalent budget for each visual input without
    # counting the much larger base64 transport representation.
    size += 4000 * sum(
        1
        for part in content
        if isinstance(part, Mapping) and part.get("type") == "image_url"
    )
    return size


def _apply_budget(
    messages: list[dict[str, Any]], budget: int
) -> list[dict[str, Any]]:
    """Drop the oldest non-system turns until the payload fits ``budget``."""
    if budget <= 0 or len(messages) <= 2:
        return messages
    total = sum(_content_budget_size(m.get("content")) for m in messages)
    result = list(messages)
    while total > budget and len(result) > 2:
        # Index 1 is the oldest non-system message.
        removed = result.pop(1)
        total -= _content_budget_size(removed.get("content"))
    return result


def _config_providers(ai: AIConfig) -> list[AIProvider]:
    """Build in-memory providers from legacy environment configuration."""
    providers: list[AIProvider] = []
    if ai.anyapi_key:
        providers.append(
            AIProvider(
                name="anyapi",
                base_url=ai.anyapi_base_url,
                api_key=ai.anyapi_key,
                model=ai.anyapi_model,
                kind="openai",
                is_default=True,
            )
        )
    if ai.bluesminds_key:
        providers.append(
            AIProvider(
                name="bluesminds",
                base_url=ai.bluesminds_base_url,
                api_key=ai.bluesminds_key,
                model=ai.bluesminds_model,
                kind="openai",
                is_default=not providers,
            )
        )
    if ai.rapidapi_key:
        providers.append(
            AIProvider(
                name="rapidapi",
                base_url=RAPIDAPI_CHAT_URL,
                api_key=ai.rapidapi_key,
                model=RAPIDAPI_MODEL,
                kind="rapidapi",
                is_default=not providers,
            )
        )
    return providers


async def seed_providers_from_env(db: Any, ai: AIConfig) -> int:
    """Copy legacy environment keys into the ``ai_providers`` table.

    Runs once, only when the table is empty. Existing operator configuration
    in the database always wins — we never overwrite a row that is already
    there. Returns the number of providers seeded.
    """
    existing = await db.list_providers()
    if existing:
        return 0

    seeded = 0
    initial_model = await db.get_setting("ai.initial_model")
    if ai.anyapi_key:
        await db.add_provider(
            "anyapi",
            ai.anyapi_base_url,
            ai.anyapi_key,
            model=ai.anyapi_model,
            kind="openai",
            is_default=True,
        )
        if initial_model:
            await db.update_provider("anyapi", model=str(initial_model))
        seeded += 1
    if ai.bluesminds_key:
        await db.add_provider(
            "bluesminds",
            ai.bluesminds_base_url,
            ai.bluesminds_key,
            model=ai.bluesminds_model,
            kind="openai",
            is_default=seeded == 0,
        )
        seeded += 1
    if ai.rapidapi_key:
        await db.add_provider(
            "rapidapi",
            RAPIDAPI_CHAT_URL,
            ai.rapidapi_key,
            model=RAPIDAPI_MODEL,
            kind="rapidapi",
            is_default=seeded == 0,
        )
        seeded += 1
    if seeded:
        logger.info("Seeded %d AI provider(s) from environment", seeded)
    return seeded
