"""Owner-only project updater command."""

from __future__ import annotations

from ..errors import UsageError, ValidationError
from ..registry import Context, command
from ..services.updater import (
    ProjectUpdateError,
    ProjectUpdater,
    parse_github_branch_url,
)
from ..utils.text import format_bytes

CATEGORY = "System"


@command(
    "getcode",
    category=CATEGORY,
    sudo_only=True,
    min_args=1,
    usage="getcode <GitHub branch URL>",
    examples=(
        "getcode https://github.com/ImTheAlireza/SelfBot/tree/arena%2F01a0337b-selfbot",
    ),
)
async def cmd_getcode(ctx: Context) -> None:
    """Replace the deployed project with a validated GitHub branch snapshot."""
    raw_url = ctx.raw_args.strip()
    if not raw_url:
        raise UsageError("Usage: `getcode <GitHub branch URL>`")

    try:
        source = parse_github_branch_url(raw_url)
    except ProjectUpdateError as exc:
        raise ValidationError(str(exc)) from None

    target = ctx.config.project_update_dir
    confirmed = await ctx.bot.confirm(
        ctx.event,
        "⚠️ **Replace deployed project code?**\n"
        f"Source: `{source.label}`\n"
        f"Target: `{target}`\n\n"
        "All existing code files will be overwritten and stale code files removed. "
        "Runtime `.env`, data, virtualenv, session and log files are preserved. "
        "Only continue if you trust this branch; its code will run with full account access.",
        timeout=45,
    )
    if not confirmed:
        await ctx.reply("👍 Code update cancelled.")
        return

    status = await ctx.reply(
        f"📥 Downloading and validating `{source.label}`…"
    )
    updater = ProjectUpdater(target)
    try:
        result = await updater.update(raw_url)
    except ProjectUpdateError as exc:
        await ctx.bot.edit(status, f"❌ Code update failed safely:\n`{exc}`")
        return
    except OSError as exc:
        await ctx.bot.edit(
            status,
            f"❌ Code update failed safely: `{type(exc).__name__}: {exc}`",
        )
        return

    await ctx.bot.edit(
        status,
        "✅ **Project code updated**\n\n"
        f"Source: `{result.source.label}`\n"
        f"Commit: `{result.commit}`\n"
        f"Target: `{result.target}`\n"
        f"Snapshot: `{result.files}` files · `{format_bytes(result.bytes)}`\n\n"
        "Existing code was overwritten and stale code was removed. Runtime state was preserved.\n"
        f"Run `{ctx.config.command_prefix}self restart` to load the new code.",
    )
