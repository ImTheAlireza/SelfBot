"""Automatic emoji reactions for channels."""

from __future__ import annotations

import logging

from ..errors import ValidationError
from ..registry import Context, command

logger = logging.getLogger(__name__)

CATEGORY = "Automation"


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
