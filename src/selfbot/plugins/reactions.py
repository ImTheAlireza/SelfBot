"""Automation tools: reactions and per-chat auto-replies."""

from __future__ import annotations

import logging

from ..errors import UsageError, ValidationError
from ..registry import Context, command
from ..utils.text import truncate

logger = logging.getLogger(__name__)

CATEGORY = "Automation"

_AUTO_REPLY_MODES = {"contain", "match"}
_REPLY_CONDITION_FLAGS = {"-nr", "-sr"}
_REPLY_CONDITION_LABELS = {
    "any": "any message",
    "nr": "non-reply only",
    "sr": "reply-to-me only",
}


@command(
    "setautoreply",
    category=CATEGORY,
    sudo_only=True,
    min_args=3,
    usage='setautoreply <contain|match> "input" "reply" [-nr|-sr]',
    examples=(
        'setautoreply contain "hello" "hi there"',
        'setautoreply contain "hello" "hi there" -nr',
        'setautoreply match "ping" "pong" -sr',
    ),
)
async def cmd_setautoreply(ctx: Context) -> None:
    """Auto-reply in the current chat only when a word or phrase matches a rule.

    Flags:
      • (no flag) — reply on any message
      • `-nr` — only reply if the triggering message is **not** a reply
      • `-sr` — only reply if the triggering message is a **reply to me**
    """
    ctx.require_single_dash_flags(*_REPLY_CONDITION_FLAGS)
    # Separate flags from positional args.
    raw_args = list(ctx.args)
    flags = [a for a in raw_args if a.lower() in _REPLY_CONDITION_FLAGS]
    positional = [a for a in raw_args if a.lower() not in _REPLY_CONDITION_FLAGS]

    if len(positional) < 3:
        raise UsageError(
            'Usage: `setautoreply <contain|match> "input" "reply" [-nr|-sr]`\n\n'
            "Flags:\n"
            "• (none) — reply on any message\n"
            "• `-nr` — only if the message is **not** a reply\n"
            "• `-sr` — only if the message is a **reply to me**"
        )

    mode = positional[0].lower()
    if mode not in _AUTO_REPLY_MODES:
        raise ValidationError("Mode must be `contain` or `match`.")

    trigger = (positional[1] or "").strip()
    reply_text = " ".join(positional[2:]).strip()

    if not trigger:
        raise UsageError('Usage: `setautoreply <contain|match> "input" "reply" [-nr|-sr]`')
    if not reply_text:
        raise ValidationError("Reply text cannot be empty.")
    if len(trigger) > 255:
        raise ValidationError("Input is too long (max 255 characters).")
    if len(reply_text) > 4000:
        raise ValidationError("Reply is too long (max 4000 characters).")

    # Determine reply condition from flags.
    if len(flags) > 1:
        raise ValidationError("Only one reply condition flag allowed (`-nr` or `-sr`).")
    reply_condition = flags[0].lower().lstrip("-") if flags else "any"

    await ctx.db.set_auto_reply(ctx.chat_id, mode, trigger, reply_text, reply_condition=reply_condition)
    ctx.bot.invalidate_auto_reply_cache(ctx.chat_id)

    cond_label = _REPLY_CONDITION_LABELS.get(reply_condition, reply_condition)
    await ctx.reply(
        "✅ Auto-reply saved for **this chat only**.\n"
        f"Mode: `{mode}`\n"
        f"Input: `{trigger}`\n"
        f"Reply: `{truncate(reply_text, 120)}`\n"
        f"Condition: `{cond_label}`"
    )


@command(
    "remautoreply",
    category=CATEGORY,
    sudo_only=True,
    min_args=1,
    usage='remautoreply <contain|match> "input" | remautoreply -allchats',
    examples=('remautoreply contain "hello"', 'remautoreply -allchats'),
)
async def cmd_remautoreply(ctx: Context) -> None:
    """Remove an auto-reply rule from the current chat, or all chats.

    Use `-allchats` to wipe every auto-reply across all chats.
    """
    ctx.require_single_dash_flags("-allchats")
    # Check for -allchats flag.
    if ctx.args[0].lower() == "-allchats":
        rules = await ctx.db.list_all_auto_replies()
        if not rules:
            await ctx.reply("ℹ️ No auto-replies exist anywhere.")
            return
        count = await ctx.db.delete_all_auto_replies()
        ctx.bot.invalidate_auto_reply_cache()
        await ctx.reply(f"✅ Removed **{count}** auto-repl{'y' if count == 1 else 'ies'} across all chats.")
        return

    if len(ctx.args) < 2:
        raise UsageError(
            'Usage: `remautoreply <contain|match> "input"`\n'
            "Or: `remautoreply -allchats` to remove everywhere."
        )

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
    for rule in rules:
        cond_label = _REPLY_CONDITION_LABELS.get(rule.reply_condition, rule.reply_condition)
        lines.append(
            f"• `{rule.mode}` — `{truncate(rule.trigger, 50)}` → {truncate(rule.reply_text, 60)}"
            f"  [{cond_label}]"
        )
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
