"""Automation tools: reactions and per-chat auto-replies."""

from __future__ import annotations

import logging

from ..errors import UsageError, ValidationError
from ..registry import Context, command
from ..utils.text import truncate

logger = logging.getLogger(__name__)

CATEGORY = "Automation"

_AUTO_REPLY_MODES = {"contain", "match"}


@command(
    "setautoreply",
    category=CATEGORY,
    sudo_only=True,
    min_args=3,
    usage='setautoreply <contain|match> "input" "reply"',
    examples=(
        'setautoreply contain "hello" "hi there"',
        'setautoreply match "ping" "pong"',
    ),
)
async def cmd_setautoreply(ctx: Context) -> None:
    """Auto-reply in the current chat only when a word or phrase matches a rule."""
    mode = ctx.args[0].lower()
    if mode not in _AUTO_REPLY_MODES:
        raise ValidationError("Mode must be `contain` or `match`.")

    trigger = (ctx.args[1] or "").strip()
    reply_text = " ".join(ctx.args[2:]).strip()
    if not trigger:
        raise UsageError('Usage: `setautoreply <contain|match> "input" "reply"`')
    if not reply_text:
        raise ValidationError("Reply text cannot be empty.")
    if len(trigger) > 255:
        raise ValidationError("Input is too long (max 255 characters).")
    if len(reply_text) > 4000:
        raise ValidationError("Reply is too long (max 4000 characters).")

    await ctx.db.set_auto_reply(ctx.chat_id, mode, trigger, reply_text)
    ctx.bot.invalidate_auto_reply_cache(ctx.chat_id)
    await ctx.reply(
        "✅ Auto-reply saved for **this chat only**.\n"
        f"Mode: `{mode}`\n"
        f"Input: `{trigger}`\n"
        f"Reply: `{truncate(reply_text, 120)}`"
    )


@command(
    "remautoreply",
    category=CATEGORY,
    sudo_only=True,
    min_args=2,
    usage='remautoreply <contain|match> "input"',
    examples=('remautoreply contain "hello"',),
)
async def cmd_remautoreply(ctx: Context) -> None:
    """Remove an auto-reply rule from the current chat."""
    mode = ctx.args[0].lower()
    if mode not in _AUTO_REPLY_MODES:
        raise ValidationError("Mode must be `contain` or `match`.")

    trigger = (ctx.args[1] or "").strip()
    if not trigger:
        raise UsageError('Usage: `remautoreply <contain|match> "input"`')

    if await ctx.db.delete_auto_reply(ctx.chat_id, mode, trigger):
        ctx.bot.invalidate_auto_reply_cache(ctx.chat_id)
        await ctx.reply(f"✅ Removed `{mode}` auto-reply for `{trigger}` in this chat.")
    else:
        await ctx.reply(f"ℹ️ No `{mode}` auto-reply for `{trigger}` in this chat.")


@command(
    "autoreplylist",
    category=CATEGORY,
    sudo_only=True,
    usage="autoreplylist",
)
async def cmd_autoreplylist(ctx: Context) -> None:
    """List auto-reply rules configured for the current chat."""
    rules = await ctx.db.list_auto_replies(ctx.chat_id)
    if not rules:
        await ctx.reply("ℹ️ No auto-replies configured for this chat.")
        return

    lines = [f"💬 **Auto-replies for this chat** ({len(rules)})\n"]
    lines += [
        f"• `{rule.mode}` — `{truncate(rule.trigger, 50)}` → {truncate(rule.reply_text, 60)}"
        for rule in rules
    ]
    await ctx.reply("\n".join(lines))


@command(
    "setreact",
    category=CATEGORY,
    sudo_only=True,
    min_args=2,
    usage="setreact <@channel> <emoji>",
    examples=("setreact @durov 🔥",),
)
async def cmd_setreact(ctx: Context) -> None:
    """React automatically to every new post in a channel."""
    channel = ctx.args[0].lstrip("@").lower()
    emoji = ctx.args[1]

    if not channel:
        raise ValidationError("Provide a channel username.")
    if len(emoji) > 8:
        raise ValidationError("That does not look like a single emoji.")

    await ctx.db.set_reaction(channel, emoji)
    ctx.bot.invalidate_reaction_cache()
    await ctx.reply(f"✅ Reacting with {emoji} to new posts in @{channel}.")


@command(
    "remreact",
    category=CATEGORY,
    sudo_only=True,
    min_args=1,
    usage="remreact <@channel>",
)
async def cmd_remreact(ctx: Context) -> None:
    """Stop auto-reacting in a channel."""
    channel = ctx.args[0].lstrip("@").lower()
    if await ctx.db.delete_reaction(channel):
        ctx.bot.invalidate_reaction_cache()
        await ctx.reply(f"✅ Stopped reacting in @{channel}.")
    else:
        await ctx.reply(f"ℹ️ No auto-reaction configured for @{channel}.")


@command("reactlist", category=CATEGORY, sudo_only=True, usage="reactlist")
async def cmd_reactlist(ctx: Context) -> None:
    """List configured auto-reactions."""
    reactions = await ctx.db.list_reactions()
    if not reactions:
        await ctx.reply("ℹ️ No auto-reactions configured.")
        return
    lines = [f"⚡ **Auto-reactions** ({len(reactions)})\n"]
    lines += [f"• @{channel} → {emoji}" for channel, emoji in sorted(reactions.items())]
    await ctx.reply("\n".join(lines))
