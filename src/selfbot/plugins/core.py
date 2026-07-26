"""Core commands: help, status, ping, bot control and admin management."""

from __future__ import annotations

import asyncio
import logging
import platform
import time
from datetime import datetime, timezone

from ..errors import UsageError, ValidationError
from ..registry import Context, command
from ..utils.text import format_duration, truncate

logger = logging.getLogger(__name__)

CATEGORY = "Core"


@command(
    "help",
    category=CATEGORY,
    usage="help [command]",
    examples=("help", "help settimer"),
)
async def cmd_help(ctx: Context) -> None:
    """Show the command list, or details for one command."""
    prefix = ctx.config.command_prefix

    if ctx.args:
        name = ctx.args[0].lstrip(prefix or "")
        cmd = ctx.bot.registry.get(name)
        if cmd is None:
            raise ValidationError(f"No such command: `{name}`")
        await ctx.reply(cmd.format_help(prefix))
        return

    grouped = ctx.bot.registry.by_category()
    lines = [f"🤖 **SelfBot** — {len(ctx.bot.registry)} commands\n"]

    for category in sorted(grouped):
        lines.append(f"**{category}**")
        for cmd in grouped[category]:
            marker = " 👑" if cmd.sudo_only else ""
            lines.append(f"  `{prefix}{cmd.name}`{marker} — {truncate(cmd.help, 58)}")
        lines.append("")

    lines.append(f"Run `{prefix}help <command>` for usage and examples.")
    await ctx.reply("\n".join(lines))


@command("ping", category=CATEGORY, usage="ping")
async def cmd_ping(ctx: Context) -> None:
    """Measure round-trip latency to Telegram."""
    start = time.perf_counter()
    message = await ctx.reply("🏓 Pinging…")
    elapsed = (time.perf_counter() - start) * 1000
    await ctx.bot.edit(message, f"🏓 **Pong!** `{elapsed:.0f} ms`")


@command("whoami", category=CATEGORY, usage="whoami")
async def cmd_whoami(ctx: Context) -> None:
    """Show your own user ID — useful for setting SUDO_USER_ID."""
    me = ctx.bot.me
    role = await ctx.db.get_role(ctx.sender_id) or "none"
    await ctx.reply(
        f"👤 **You**\n"
        f"ID: `{ctx.sender_id}`\n"
        f"Name: {getattr(me, 'first_name', '?')}\n"
        f"Username: @{getattr(me, 'username', None) or '—'}\n"
        f"Role: `{role}`\n"
        f"Chat ID: `{ctx.chat_id}`"
    )


@command("status", category=CATEGORY, usage="status", aliases=("stats",))
async def cmd_status(ctx: Context) -> None:
    """Show uptime, configuration and runtime counters."""
    bot = ctx.bot
    active_timers = len(bot.timer_tasks)
    queued = sum(len(v) for v in bot.zip_queue.values())

    await ctx.reply(
        f"📊 **SelfBot Status**\n\n"
        f"State: {'🟢 active' if bot.active else '🔴 paused'}\n"
        f"Uptime: `{format_duration(bot.uptime)}`\n"
        f"Commands: `{len(bot.registry)}`\n"
        f"Database: `{bot.db.backend}`\n"
        f"AI: `{bot.ai.name}`  •  Images: `{bot.image_ai.name}`\n"
        f"Active timers: `{active_timers}`\n"
        f"Queued files: `{queued}`\n"
        f"Python: `{platform.python_version()}` on `{platform.system()}`\n"
        f"Time: `{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC`"
    )


@command(
    "self",
    category=CATEGORY,
    sudo_only=True,
    usage="self <on|off|restart|status|logs> [n]",
    examples=("self off", "self logs 50"),
)
async def cmd_self(ctx: Context) -> None:
    """Control the bot process: on, off, restart, status, logs."""
    if not ctx.args:
        raise UsageError(
            "Usage: `self <on|off|restart|status|logs>`\n"
            "• `on` / `off` — enable or pause command handling\n"
            "• `restart` — restart via supervisor\n"
            "• `status` — supervisor process state\n"
            "• `logs [n]` — tail the error log"
        )

    action = ctx.args[0].lower()

    if action == "on":
        ctx.bot.active = True
        await ctx.reply("✅ Bot is now **active**.")
    elif action == "off":
        ctx.bot.active = False
        await ctx.reply("⏸ Bot is now **paused**. Send `self on` to resume.")
    elif action == "restart":
        await _supervisor_restart(ctx)
    elif action == "status":
        await _supervisor_status(ctx)
    elif action == "logs":
        await _supervisor_logs(ctx)
    else:
        raise ValidationError(f"Unknown action `{action}`. Use on/off/restart/status/logs.")


async def _run(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run a subprocess without blocking the event loop."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        raise
    return (
        process.returncode or 0,
        stdout.decode(errors="replace").strip(),
        stderr.decode(errors="replace").strip(),
    )


def _require_supervisor(ctx: Context) -> None:
    if not ctx.config.supervisor.enabled:
        raise UsageError(
            "Supervisor is not configured. Set `SUPERVISOR_CONFIG` and "
            "`SUPERVISOR_PROCESS` in your .env to use this."
        )


async def _supervisor_restart(ctx: Context) -> None:
    _require_supervisor(ctx)
    if not await ctx.bot.confirm(ctx.event, "⚠️ Restart the bot?"):
        await ctx.reply("👍 Cancelled.")
        return

    await ctx.reply("🔄 Restarting…")
    supervisor = ctx.config.supervisor
    try:
        code, _out, err = await _run(
            "supervisorctl", "-c", supervisor.config_path,
            "restart", supervisor.process_name,
        )
        if code != 0:
            await ctx.reply(f"❌ Restart failed:\n`{truncate(err, 500)}`")
    except asyncio.TimeoutError:
        await ctx.reply("❌ Restart timed out. Check `self status`.")
    except FileNotFoundError:
        await ctx.reply("❌ `supervisorctl` not found on PATH.")


async def _supervisor_status(ctx: Context) -> None:
    _require_supervisor(ctx)
    supervisor = ctx.config.supervisor
    try:
        code, out, err = await _run(
            "supervisorctl", "-c", supervisor.config_path,
            "status", supervisor.process_name,
        )
    except FileNotFoundError:
        raise UsageError("`supervisorctl` not found on PATH.") from None
    except asyncio.TimeoutError:
        raise UsageError("supervisorctl timed out.") from None

    if code != 0 and not out:
        await ctx.reply(f"❌ Could not read status:\n`{truncate(err, 500)}`")
        return

    emoji = next(
        (e for token, e in (
            ("RUNNING", "✅"), ("STARTING", "🔄"),
            ("STOPPED", "⏹"), ("FATAL", "❌"),
        ) if token in out),
        "❓",
    )
    await ctx.reply(f"{emoji} **Process status**\n```\n{truncate(out, 1000)}\n```")


async def _supervisor_logs(ctx: Context) -> None:
    log_file = ctx.config.supervisor.log_file
    if not log_file:
        raise UsageError("Set `SUPERVISOR_LOG_FILE` in your .env to read logs.")

    lines = 20
    if len(ctx.args) > 1 and ctx.args[1].isdigit():
        lines = min(int(ctx.args[1]), 200)

    try:
        code, out, err = await _run("tail", "-n", str(lines), log_file)
    except FileNotFoundError:
        raise UsageError("`tail` not found on PATH.") from None

    if code != 0:
        await ctx.reply(f"❌ Could not read logs:\n`{truncate(err, 400)}`")
        return
    await ctx.reply(f"📋 **Last {lines} lines**\n```\n{out or '(empty)'}\n```")


# ---------------------------------------------------------------------------
# Admin management
# ---------------------------------------------------------------------------


@command(
    "setadmin",
    category="Admin",
    sudo_only=True,
    min_args=1,
    usage="setadmin <user_id|@username>",
    examples=("setadmin 123456789", "setadmin @someone"),
)
async def cmd_setadmin(ctx: Context) -> None:
    """Allow another user to run bot commands."""
    target = ctx.args[0]
    try:
        entity = await ctx.client.get_entity(int(target) if target.isdigit() else target)
    except Exception as exc:
        raise ValidationError(f"Could not find user `{target}`: {exc}") from exc

    user_id = entity.id
    if user_id == ctx.config.sudo_user_id:
        raise ValidationError("That user is already the owner.")

    name = getattr(entity, "first_name", None) or getattr(entity, "username", None) or "Unknown"
    await ctx.db.add_admin(user_id, name)
    await ctx.reply(f"✅ **{name}** (`{user_id}`) can now use the bot.")


@command(
    "remadmin",
    category="Admin",
    sudo_only=True,
    min_args=1,
    usage="remadmin <user_id>",
)
async def cmd_remadmin(ctx: Context) -> None:
    """Revoke another user's access."""
    target = ctx.args[0]
    if not target.lstrip("-").isdigit():
        raise ValidationError("Provide a numeric user ID.")

    removed = await ctx.db.remove_admin(int(target))
    if removed:
        await ctx.reply(f"✅ Removed `{target}`.")
    else:
        await ctx.reply(f"ℹ️ `{target}` is not an admin (or is the owner).")


@command("adminlist", category="Admin", sudo_only=True, usage="adminlist")
async def cmd_adminlist(ctx: Context) -> None:
    """List everyone who can use the bot."""
    users = await ctx.db.list_users()
    if not users:
        await ctx.reply("ℹ️ No users registered.")
        return

    emoji = {"sudo": "👑", "admin": "👤"}
    lines = [f"👥 **Authorised users** ({len(users)})\n"]
    for user in users:
        icon = emoji.get(user["role"], "•")
        name = user.get("username") or "—"
        lines.append(f"{icon} `{user['id']}` · **{user['role']}** · {name}")
    await ctx.reply("\n".join(lines))
