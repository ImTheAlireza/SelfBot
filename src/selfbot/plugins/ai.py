"""AI commands backed by AnyAPI (OpenAI-compatible), with a RapidAPI fallback."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ..errors import FeatureDisabledError, ProviderError, UsageError
from ..registry import Context, command
from ..utils.text import truncate

logger = logging.getLogger(__name__)

CATEGORY = "AI"
SYSTEM_PROMPT = "Format your replies with markdown when applicable"

# AnyAPI — primary provider (OpenAI-compatible /v1/chat/completions).
# Base URL and model come from config (ANYAPI_BASE_URL / ANYAPI_MODEL), so the
# defaults here are only used when config values are absent.
ANYAPI_DEFAULT_BASE_URL = "https://api.anyapi.ai/v1"
ANYAPI_DEFAULT_MODEL = "openai/gpt-4o"

# RapidAPI — legacy fallback used when AnyAPI is rate-limited and a
# RAPIDAPI_KEY is still configured.
RAPIDAPI_HOST = "chatgpt-api8.p.rapidapi.com"
RAPIDAPI_CHAT_URL = f"https://{RAPIDAPI_HOST}/chato"
RAPIDAPI_MODEL = "GPT_5_4_high"

BACKUP_RAPIDAPI_HOST = "adult-gpt.p.rapidapi.com"
BACKUP_RAPIDAPI_CHAT_URL = f"https://{BACKUP_RAPIDAPI_HOST}/adultgpt"
BACKUP_RAPIDAPI_GENERE = "ai-gf-1"

# HTTP statuses that mean "try the next provider" instead of "user error".
_FALLBACK_STATUSES = frozenset({408, 429, 502, 503, 504})


class _ProviderStatusError(ProviderError):
    """ProviderError that carries the HTTP status for fallback decisions."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@command(
    "gpt",
    category=CATEGORY,
    min_args=1,
    usage="gpt <prompt>",
    examples=("gpt What is the meaning of life?",),
)
async def cmd_gpt(ctx: Context) -> None:
    """Ask GPT through AnyAPI, falling back to RapidAPI when configured."""
    ai = ctx.config.ai
    if not ai.enabled:
        raise FeatureDisabledError(
            "No AI provider is configured. Set `ANYAPI_KEY` (or `RAPIDAPI_KEY`) "
            "in your `.env`."
        )

    prompt = ctx.raw_args.strip()
    if not prompt:
        raise UsageError(f"Usage: `{ctx.config.command_prefix}gpt <prompt>`")

    status = await ctx.reply("🤖 Thinking…")
    try:
        if ai.anyapi_key:
            try:
                answer = await _anyapi_completion(
                    ctx.bot.http,
                    prompt=prompt,
                    api_key=ai.anyapi_key,
                    base_url=ai.anyapi_base_url,
                    model=ai.anyapi_model,
                )
            except ProviderError as exc:
                if isinstance(exc, _ProviderStatusError) and exc.status in _FALLBACK_STATUSES:
                    if ai.rapidapi_key:
                        logger.warning(
                            "AnyAPI unavailable (HTTP %s); falling back to RapidAPI", exc.status
                        )
                        answer = await _rapidapi_completion(
                            ctx.bot.http, prompt=prompt, api_key=ai.rapidapi_key
                        )
                    else:
                        raise
                else:
                    raise
        else:
            answer = await _rapidapi_completion(ctx.bot.http, prompt=prompt, api_key=ai.rapidapi_key)
    finally:
        await _delete_status(status)

    await ctx.reply(answer)


# --------------------------------------------------------------------------
# AnyAPI (OpenAI-compatible)
# --------------------------------------------------------------------------


async def _anyapi_completion(
    http: Any,
    *,
    prompt: str,
    api_key: str,
    base_url: str,
    model: str,
) -> str:
    """Request one chat completion from AnyAPI and return its text."""
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
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=120,
        # A retry after an uncertain timeout can duplicate a billable request.
        retries=0,
    )

    try:
        payload = await response.json(content_type=None)
    except Exception as exc:
        raise _ProviderStatusError(
            f"AnyAPI sent an invalid response (HTTP {response.status}).", response.status
        ) from exc

    if response.status >= 400:
        raise _ProviderStatusError(
            _format_provider_error(payload, response.status, "AnyAPI"), response.status
        )

    if isinstance(payload, Mapping) and payload.get("error"):
        raise _ProviderStatusError(
            _format_provider_error(payload, response.status, "AnyAPI"),
            response.status or 400,
        )

    return _extract_answer(payload, "AnyAPI")


# --------------------------------------------------------------------------
# RapidAPI (legacy)
# --------------------------------------------------------------------------


async def _rapidapi_completion(http: Any, *, prompt: str, api_key: str) -> str:
    """Request one chat completion from RapidAPI and return its text."""
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
            return await _backup_rapidapi_completion(http, prompt=prompt, api_key=api_key)
        raise ProviderError(
            f"RapidAPI sent an invalid response (HTTP {response.status})."
        ) from exc

    if response.status in _FALLBACK_STATUSES:
        logger.warning("Primary RapidAPI quota reached; trying the backup API")
        return await _backup_rapidapi_completion(http, prompt=prompt, api_key=api_key)

    if response.status >= 400:
        raise ProviderError(_format_provider_error(payload, response.status, "RapidAPI"))

    if isinstance(payload, Mapping) and payload.get("error"):
        raise ProviderError(_format_provider_error(payload, response.status, "RapidAPI"))

    return _extract_answer(payload, "RapidAPI")


async def _backup_rapidapi_completion(http: Any, *, prompt: str, api_key: str) -> str:
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
        raise ProviderError(_format_provider_error(payload, response.status, "RapidAPI"))

    if isinstance(payload, Mapping) and payload.get("error"):
        raise ProviderError(_format_provider_error(payload, response.status, "RapidAPI"))

    return _extract_answer(payload, "RapidAPI")


# --------------------------------------------------------------------------
# Response parsing & error formatting
# --------------------------------------------------------------------------


def _extract_answer(payload: Any, provider: str = "AnyAPI") -> str:
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
        answer = _content_text(content)
        if not answer and isinstance(choice.get("text"), str):
            answer = choice["text"].strip()
        if answer:
            return answer

    message = payload.get("message")
    if isinstance(message, Mapping):
        answer = _content_text(message.get("content"))
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
            return _extract_answer(data, provider)
        except ProviderError:
            pass

    raise ProviderError(f"{provider} returned an empty completion.")


def _content_text(content: Any) -> str:
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


def _format_provider_error(payload: Any, status: int, provider: str) -> str:
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
        return f"{provider} error (HTTP {status}): {truncate(detail, 300)}"
    return f"{provider} returned HTTP {status}."


def _format_rapidapi_error(payload: Any, status: int) -> str:
    """Backwards-compatible wrapper used by older callers/tests."""
    return _format_provider_error(payload, status, "RapidAPI")


async def _delete_status(message: Any) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete the GPT status message", exc_info=True)
