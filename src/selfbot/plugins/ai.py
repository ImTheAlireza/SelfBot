"""AI commands backed by the ChatGPT API8 service on RapidAPI."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ..errors import FeatureDisabledError, ProviderError, UsageError
from ..registry import Context, command
from ..utils.text import truncate

logger = logging.getLogger(__name__)

CATEGORY = "AI"
RAPIDAPI_HOST = "chatgpt-api8.p.rapidapi.com"
RAPIDAPI_CHAT_URL = f"https://{RAPIDAPI_HOST}/chato"
RAPIDAPI_MODEL = "GPT_5_4_high"
SYSTEM_PROMPT = "Format your relies with markdown when applicable"

BACKUP_RAPIDAPI_HOST = "adult-gpt.p.rapidapi.com"
BACKUP_RAPIDAPI_CHAT_URL = f"https://{BACKUP_RAPIDAPI_HOST}/adultgpt"
BACKUP_RAPIDAPI_GENERE = "ai-gf-1"


@command(
    "gpt",
    category=CATEGORY,
    min_args=1,
    usage="gpt <prompt>",
    examples=("gpt What is the meaning of life?",),
)
async def cmd_gpt(ctx: Context) -> None:
    """Ask GPT through RapidAPI, with an automatic quota fallback."""
    api_key = getattr(ctx.config.ai, "rapidapi_key", "") if hasattr(ctx.config, "ai") else ""
    if not api_key:
        raise FeatureDisabledError(
            "RapidAPI key is not configured. Set `RAPIDAPI_KEY` in your `.env`."
        )

    prompt = ctx.raw_args.strip()
    if not prompt:
        raise UsageError(f"Usage: `{ctx.config.command_prefix}gpt <prompt>`")

    status = await ctx.reply("🤖 Thinking…")
    try:
        answer = await _rapidapi_completion(ctx.bot.http, prompt=prompt, api_key=api_key)
    finally:
        await _delete_status(status)

    await ctx.reply(answer)


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
        # A retry after an uncertain timeout can duplicate a billable request.
        retries=0,
    )

    try:
        payload = await response.json(content_type=None)
    except Exception as exc:
        if response.status == 429:
            logger.warning("Primary RapidAPI quota reached; trying the backup API")
            return await _backup_rapidapi_completion(http, prompt=prompt, api_key=api_key)
        raise ProviderError(
            f"RapidAPI sent an invalid response (HTTP {response.status})."
        ) from exc

    if response.status == 429:
        logger.warning("Primary RapidAPI quota reached; trying the backup API")
        return await _backup_rapidapi_completion(http, prompt=prompt, api_key=api_key)

    if response.status >= 400:
        raise ProviderError(_format_rapidapi_error(payload, response.status))

    if isinstance(payload, Mapping) and payload.get("error"):
        raise ProviderError(_format_rapidapi_error(payload, response.status))

    return _extract_answer(payload)


async def _backup_rapidapi_completion(http: Any, *, prompt: str, api_key: str) -> str:
    """Use Adult GPT when the primary RapidAPI plan returns HTTP 429."""
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
        raise ProviderError(_format_rapidapi_error(payload, response.status))

    if isinstance(payload, Mapping) and payload.get("error"):
        raise ProviderError(_format_rapidapi_error(payload, response.status))

    return _extract_answer(payload)


def _extract_answer(payload: Any) -> str:
    """Extract text from common chat-completion response shapes."""
    if isinstance(payload, str):
        answer = payload.strip()
        if answer:
            return answer
        raise ProviderError("RapidAPI returned an empty completion.")

    if not isinstance(payload, Mapping):
        raise ProviderError("RapidAPI returned an unexpected response.")

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
            return _extract_answer(data)
        except ProviderError:
            pass

    raise ProviderError("RapidAPI returned an empty completion.")


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


def _format_rapidapi_error(payload: Any, status: int) -> str:
    """Turn a RapidAPI error response into a short, actionable message."""
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
        401: "RapidAPI rejected the API key.",
        403: "RapidAPI denied access. Check the API subscription.",
        408: "The RapidAPI request timed out. Try again.",
        429: "The RapidAPI quota or rate limit was reached. Try again later.",
        502: "The AI service behind RapidAPI is temporarily unavailable.",
        503: "The AI service behind RapidAPI is temporarily unavailable.",
    }.get(status)
    if friendly:
        return friendly

    if detail:
        return f"RapidAPI error (HTTP {status}): {truncate(detail, 300)}"
    return f"RapidAPI returned HTTP {status}."


async def _delete_status(message: Any) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete the GPT status message", exc_info=True)
