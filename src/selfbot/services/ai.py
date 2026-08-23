"""AI provider service.

The :class:`AIManager` owns everything the ``gpt`` family of commands needs:

* database-backed providers (OpenAI-compatible and legacy RapidAPI),
* ordered fallback across enabled providers,
* automatic cooldown after quota/rate-limit errors,
* success/failure counters persisted per provider,
* per-chat conversation memory (rolled out from here in phase 3),
* live ``/models`` discovery for ``gptmodel list``.

The low-level HTTP call shapes (URLs, headers, JSON bodies) are kept identical
to the original ``plugins/ai.py`` implementation so existing behaviour and the
tests that pin it continue to pass.
"""

from __future__ import annotations

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
    "AIManager",
    "ProviderStatus",
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
_COOLDOWN_BASE = 60.0
_MODELS_CACHE_TTL = 600.0


class ProviderStatusError(ProviderError):
    """ProviderError that carries the HTTP status for fallback decisions."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


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
    messages: Sequence[Mapping[str, str]],
    api_key: str,
    base_url: str,
    model: str,
    timeout: float = 120.0,
) -> str:
    """Request a chat completion from an OpenAI-compatible endpoint."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    response = await http.request(
        "POST",
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "temperature": 1,
            "messages": list(messages),
        },
        timeout=timeout,
        # A retry after an uncertain timeout can duplicate a billable request.
        retries=0,
    )

    try:
        payload = await response.json(content_type=None)
    except Exception as exc:
        raise ProviderStatusError(
            f"Provider sent an invalid response (HTTP {response.status}).",
            response.status,
        ) from exc

    if response.status >= 400:
        raise ProviderStatusError(
            format_provider_error(payload, response.status, "AI provider"),
            response.status,
        )

    if isinstance(payload, Mapping) and payload.get("error"):
        raise ProviderStatusError(
            format_provider_error(payload, response.status or 400, "AI provider"),
            response.status or 400,
        )

    return extract_answer(payload, "AI provider")


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
    if isinstance(payload, Mapping):
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
        self._consecutive_failures: dict[str, int] = {}
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
        """Cheap connectivity probe. Returns ``(ok, detail)``."""
        provider = await self.db.get_provider(name)
        if provider is None:
            return False, "not configured"
        if provider.kind != "openai":
            return True, "key present (live test skipped for this provider type)"
        if not provider.api_key:
            return False, "no API key"
        url = f"{provider.base_url.rstrip('/')}/models"
        try:
            response = await self.http.request(
                "GET",
                url,
                headers={"Authorization": f"Bearer {provider.api_key}"},
                timeout=15,
                retries=0,
            )
            if response.status >= 400:
                body = (await response.text())[:120]
                return False, f"HTTP {response.status}: {body}"
            payload = await response.json(content_type=None)
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
            return True, f"ok — {len(models)} models available"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    # -- completion -------------------------------------------------------

    async def chat(
        self,
        prompt: str,
        *,
        chat_id: int | None = None,
        history: bool = False,
        model: str | None = None,
        system: str | None = None,
    ) -> str:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system or SYSTEM_PROMPT}
        ]
        used_provider = ""

        if history and chat_id is not None:
            turns = max(1, self.config.memory_turns)
            for stored in await self.db.recent_ai_messages(
                chat_id, limit=turns * 2
            ):
                messages.append({"role": stored.role, "content": stored.content})

        messages.append({"role": "user", "content": prompt})
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
        messages: list[dict[str, str]],
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

        for provider in providers:
            if not provider.api_key:
                continue
            if await self._in_cooldown(provider):
                errors.append(f"{provider.name}: cooling down")
                continue

            use_model = model or provider.model or ""
            try:
                answer = await self._call(
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
            self._consecutive_failures.pop(provider.name, None)
            self._last_provider = provider.name
            self._last_model = use_model
            return answer

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
        messages: list[dict[str, str]],
        model: str,
    ) -> str:
        if provider.kind in ("rapidapi", "rapidapi_backup"):
            prompt = next(
                (m["content"] for m in reversed(messages) if m["role"] == "user"),
                "",
            )
            return await rapidapi_completion(
                self.http, prompt=prompt, api_key=provider.api_key
            )
        if not model:
            model = ANYAPI_DEFAULT_MODEL
        return await openai_completion(
            self.http,
            messages=messages,
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
            count = self._consecutive_failures.get(provider.name, 0) + 1
            self._consecutive_failures[provider.name] = count
            seconds = min(
                _COOLDOWN_BASE * (2 ** (count - 1)),
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


def _dt_from_timestamp(ts: float) -> Any:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _apply_budget(
    messages: list[dict[str, str]], budget: int
) -> list[dict[str, str]]:
    """Drop the oldest non-system turns until the payload fits ``budget``."""
    if budget <= 0 or len(messages) <= 2:
        return messages
    total = sum(len(m["content"]) for m in messages)
    result = list(messages)
    while total > budget and len(result) > 2:
        # Index 1 is the oldest non-system message.
        removed = result.pop(1)
        total -= len(removed["content"])
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
