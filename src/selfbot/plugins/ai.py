"""AI commands backed by OpenRouter."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ..errors import FeatureDisabledError, ProviderError, UsageError
from ..registry import Context, command
from ..utils.text import truncate

logger = logging.getLogger(__name__)

CATEGORY = "AI"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_REFERER = "https://github.com/ImTheAlireza/SelfBot"


@command(
    "gpt",
    category=CATEGORY,
    min_args=1,
    usage="gpt <prompt>",
    examples=("gpt What is the meaning of life?",),
)
async def cmd_gpt(ctx: Context) -> None:
    """Ask a GPT model a question through OpenRouter."""
    settings = ctx.config.openrouter
    if not settings.enabled:
        raise FeatureDisabledError("Set `OPENROUTER_API_KEY` in your `.env`, then restart the bot.")

    prompt = ctx.raw_args.strip()
    if not prompt:
        raise UsageError(f"Usage: `{ctx.config.command_prefix}gpt <prompt>`")

    status = await ctx.reply("🤖 Thinking…")
    try:
        answer = await _openrouter_completion(
            ctx.bot.http,
            api_key=settings.api_key,
            model=settings.model,
            prompt=prompt,
        )
    finally:
        await _delete_status(status)

    # Model output is untrusted text. Disabling Telegram's Markdown parser
    # prevents unmatched formatting characters from making delivery fail.
    await ctx.reply(answer, parse_mode=None)


async def _openrouter_completion(
    http: Any,
    *,
    api_key: str,
    model: str,
    prompt: str,
) -> str:
    """Request one non-streaming chat completion and return its text."""
    response = await http.request(
        "POST",
        OPENROUTER_CHAT_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": OPENROUTER_REFERER,
            "X-OpenRouter-Title": "SelfBot",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        },
        timeout=120,
        # Retrying a timed-out generation could duplicate a billable request.
        # OpenRouter already handles provider fallbacks, so fail safely instead.
        retries=0,
    )

    try:
        payload = await response.json(content_type=None)
    except Exception as exc:
        raise ProviderError(
            f"OpenRouter sent an invalid response (HTTP {response.status})."
        ) from exc

    if response.status >= 400:
        raise ProviderError(_format_openrouter_error(payload, response.status))

    # OpenRouter can report an error in an HTTP 200 response when generation
    # fails after processing has started.
    if isinstance(payload, Mapping) and payload.get("error"):
        raise ProviderError(_format_openrouter_error(payload, response.status))

    return _extract_answer(payload)


def _extract_answer(payload: Any) -> str:
    """Validate a chat-completion response and collect text content parts."""
    if not isinstance(payload, Mapping):
        raise ProviderError("OpenRouter returned an unexpected response.")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise ProviderError("OpenRouter returned no completion choices.")

    choice = choices[0]
    message = choice.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None

    pieces: list[str] = []
    if isinstance(content, str):
        pieces.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
                pieces.append(part["text"])

    # A few OpenAI-compatible providers still use the legacy choice.text field.
    if not pieces and isinstance(choice.get("text"), str):
        pieces.append(choice["text"])

    answer = "\n".join(pieces).strip()
    if not answer:
        raise ProviderError("OpenRouter returned an empty completion.")
    return answer


def _format_openrouter_error(payload: Any, status: int) -> str:
    """Turn OpenRouter's documented error envelope into an actionable message."""
    code = status
    detail = ""

    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            raw_code = error.get("code")
            if isinstance(raw_code, int):
                code = raw_code
            elif isinstance(raw_code, str) and raw_code.isdigit():
                code = int(raw_code)
            if isinstance(error.get("message"), str):
                detail = error["message"].strip()
        elif isinstance(error, str):
            detail = error.strip()

    friendly = {
        401: "OpenRouter rejected the API key. Check `OPENROUTER_API_KEY`.",
        402: "Your OpenRouter account has insufficient credits.",
        408: "OpenRouter timed out. Try again.",
        429: "OpenRouter's rate limit was reached. Try again later.",
        502: "The selected model is temporarily unavailable on OpenRouter.",
        503: "OpenRouter has no available provider for the selected model.",
    }.get(code)
    if friendly:
        return friendly

    if detail:
        return f"OpenRouter error (HTTP {code}): {truncate(detail, 300)}"
    return f"OpenRouter returned HTTP {code}."


async def _delete_status(message: Any) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete the GPT status message", exc_info=True)
