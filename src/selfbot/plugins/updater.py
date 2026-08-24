"""Owner-only project updater command."""

from __future__ import annotations

from ..errors import UsageError, ValidationError
from ..registry import Context, command
from ..services.updater import (
    ProjectUpdateError,
    ProjectUpdater,
    parse_getcode_request,
    parse_github_branch_url,
    resolve_update_target,
)
from ..utils.text import format_bytes

CATEGORY = "System"


@command(
    "getcode",
    category=CATEGORY,
    sudo_only=True,
    min_args=2,
    usage="getcode <GitHub branch URL> <destination folder>",
    examples=(
        "getcode https://github.com/ImTheAlireza/SelfBot/tree/arena%2F01a0337b-selfbot Selfbot",
    ),
)
async def cmd_getcode(ctx: Context) -> None:
    """Replace the deployed project with a validated GitHub branch snapshot."""
    raw_request = ctx.raw_args.strip()
    if not raw_request:
        raise UsageError("Usage: `getcode <GitHub branch URL> <destination folder>`")

    try:
        branch_url, destination = parse_getcode_request(raw_request)
        source = parse_github_branch_url(branch_url)
        target = resolve_update_target(ctx.config.project_update_root, destination)
    except ProjectUpdateError as exc:
        raise ValidationError(str(exc)) from None
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

    status = await ctx.reply(f"📥 Downloading and validating `{source.label}`…")
    updater = ProjectUpdater(target)
    try:
        result = await updater.update(branch_url)
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
        f"Restart `{destination}`'s process/service to load the new code. If this is the "
        f"current SelfBot, run `{ctx.config.command_prefix}self restart`.",
    )
