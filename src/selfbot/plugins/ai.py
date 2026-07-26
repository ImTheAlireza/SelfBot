"""AI commands: chat, web-search chat, reasoning and image generation."""

from __future__ import annotations

import logging

from ..errors import ValidationError
from ..registry import Context, command
from ..services.ai import ChatMessage
from ..utils.files import temp_workspace
from ..utils.text import truncate

logger = logging.getLogger(__name__)

CATEGORY = "AI"

SYSTEM_PROMPT = (
    "You are a helpful assistant replying inside a Telegram chat. "
    "Be concise and use Telegram-flavoured Markdown sparingly."
)


async def _chat(ctx: Context, *, web_search: bool, reasoning: bool) -> None:
    prompt = ctx.raw_args.strip()

    # Fall back to the replied-to message so `gpt` alone summarises a reply.
    if not prompt and ctx.event.is_reply:
        replied = await ctx.get_reply_message()
        prompt = (replied.raw_text or "").strip()

    if not prompt:
        raise ValidationError(f"Give me a prompt: `{ctx.command} <your question>`")

    label = "🔎 Searching" if web_search else "🧠 Thinking" if reasoning else "💭 Thinking"
    status = await ctx.reply(f"{label}…")

    model = ctx.config.ai.reasoning_model if reasoning else None
    if ctx.bot.ai.name == "rapidapi" and reasoning:
        model = "reasoning"  # legacy endpoint selector

    answer = await ctx.bot.ai.complete(
        [
            ChatMessage("system", SYSTEM_PROMPT),
            ChatMessage("user", prompt),
        ],
        model=model,
        web_search=web_search,
    )

    if not answer:
        await ctx.bot.edit(status, "⚠️ The model returned an empty response.")
        return

    header = "🔎 **Web**" if web_search else "🧠 **Reasoning**" if reasoning else "🤖 **AI**"
    body = f"{header}\n\n{answer}"

    # Long answers get split into follow-ups rather than truncated.
    if len(body) <= 4096:
        await ctx.bot.edit(status, body)
    else:
        await status.delete()
        await ctx.reply(body)


@command(
    "gpt",
    category=CATEGORY,
    usage="gpt <prompt>",
    examples=("gpt explain async/await in one paragraph",),
)
async def cmd_gpt(ctx: Context) -> None:
    """Ask the configured AI model a question."""
    await _chat(ctx, web_search=False, reasoning=False)


@command("gpts", category=CATEGORY, usage="gpts <prompt>")
async def cmd_gpts(ctx: Context) -> None:
    """Ask the AI with web search enabled."""
    await _chat(ctx, web_search=True, reasoning=False)


@command("gptr", category=CATEGORY, usage="gptr <prompt>")
async def cmd_gptr(ctx: Context) -> None:
    """Ask the AI using its reasoning model."""
    await _chat(ctx, web_search=False, reasoning=True)


@command(
    "imagine",
    category=CATEGORY,
    min_args=1,
    usage="imagine <prompt>",
    examples=("imagine a neon city in the rain",),
)
async def cmd_imagine(ctx: Context) -> None:
    """Generate an image from a text prompt."""
    prompt = ctx.raw_args.strip()
    status = await ctx.reply(f"🎨 Generating…\n`{truncate(prompt, 100)}`")

    result = await ctx.bot.image_ai.generate(prompt)

    if isinstance(result, str):  # provider returned a URL
        data = await ctx.bot.http.get_bytes(
            result, timeout=120, max_bytes=ctx.config.max_file_size_bytes
        )
    else:
        data = result

    with temp_workspace(parent=ctx.config.downloads_dir) as workspace:
        path = workspace / "image.png"
        path.write_bytes(data)
        await ctx.client.send_file(
            ctx.chat_id,
            str(path),
            caption=f"🎨 `{truncate(prompt, 200)}`",
            reply_to=ctx.event.id,
        )
    await status.delete()
