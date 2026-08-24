"""Core commands: help, status, ping, bot control and admin management."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ..errors import UsageError, ValidationError
from ..registry import Context, command
from ..services.supervisor import (
    STATE_EMOJI,
    SupervisorNotFound,
    SupervisorRunner,
    audit_program,
    describe_discovery,
    parse_state,
    resolve_supervisorctl,
)
from ..utils.text import format_duration, truncate

logger = logging.getLogger(__name__)

CATEGORY = "Core"


def _restart_process() -> None:
    """Replace this process with a fresh SelfBot instance.

    This works even when the bot was started directly from a shell and no
    Docker, systemd, or supervisord process manager is available. ``execv``
    keeps the same PID and environment while loading the latest code from
    disk. Application CLI flags, such as ``--env-file``, are preserved.
    """
    argv = [sys.executable, "-m", "selfbot", *sys.argv[1:]]
    logger.info("Re-executing SelfBot with %s", sys.executable)
    try:
        os.execv(sys.executable, argv)
    except OSError:
        # If exec fails, leave the existing bot alive and make the reason
        # visible in its logs instead of silently terminating it.
        logger.exception("Could not re-execute SelfBot")


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
    lines.append("All command flags use one dash, for example `-here` or `-force`.")
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

    ai_line = ""
    try:
        from .ai import get_manager

        manager = get_manager(ctx)
        statuses = await manager.status()
        if statuses:
            available = sum(1 for s in statuses if s.available)
            cooling = sum(1 for s in statuses if s.cooldown_remaining > 0)
            default = next(
                (s.provider.name for s in statuses if s.provider.is_default), None
            )
            parts = [f"{available}/{len(statuses)} up"]
            if default:
                parts.append(f"default `{default}`")
            if cooling:
                parts.append(f"{cooling} cooling")
            ai_line = f"AI providers: `{ ' · '.join(parts) }`\n"
    except Exception:
        logger.debug("Could not build AI status line", exc_info=True)

    await ctx.reply(
        f"📊 **SelfBot Status**\n\n"
        f"State: {'🟢 active' if bot.active else '🔴 paused'}\n"
        f"Uptime: `{format_duration(bot.uptime)}`\n"
        f"Commands: `{len(bot.registry)}`\n"
        f"Database: `{bot.db.backend}`\n"
        f"{ai_line}"
        f"Active timers: `{active_timers}`\n"
        f"Queued files: `{queued}`\n"
        f"Python: `{platform.python_version()}` on `{platform.system()}`\n"
        f"Time: `{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC`"
    )


@command(
    "self",
    category=CATEGORY,
    sudo_only=True,
    usage="self <on|off|restart|status|logs|diag> [n]",
    examples=("self off", "self status", "self logs 50", "self diag"),
)
async def cmd_self(ctx: Context) -> None:
    """Control the bot process: on, off, restart, status, logs, diag."""
    if not ctx.args:
        raise UsageError(
            "Usage: `self <on|off|restart|status|logs|diag>`\n"
            "• `on` / `off` — enable or pause command handling\n"
            "• `restart` — restart this bot process\n"
            "• `status` — supervisor process state\n"
            "• `logs [n]` — tail the error log\n"
            "• `diag` — troubleshoot supervisor setup"
        )

    action = ctx.args[0].lower()

    if action == "on":
        ctx.bot.active = True
        await ctx.reply("✅ Bot is now **active**.")
    elif action == "off":
        ctx.bot.active = False
        await ctx.reply("⏸ Bot is now **paused**. Send `self on` to resume.")
    elif action == "restart":
        await _restart_bot(ctx)
    elif action == "status":
        await _supervisor_status(ctx)
    elif action == "logs":
        await _supervisor_logs(ctx)
    elif action in {"diag", "diagnose", "doctor"}:
        await _supervisor_diag(ctx)
    else:
        raise ValidationError(
            f"Unknown action `{action}`. "
            "Use on/off/restart/status/logs/diag."
        )


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


def _runner(ctx: Context) -> SupervisorRunner:
    """Build a supervisor runner from config, or explain why we cannot."""
    supervisor = ctx.config.supervisor
    if not supervisor.enabled:
        raise UsageError(
            "Set `SUPERVISOR_PROCESS` in your .env to the program name from "
            "your supervisord config, then restart the bot."
        )
    return SupervisorRunner(
        process_name=supervisor.process_name,
        config_path=supervisor.config_path,
        executable=supervisor.executable,
    )


def _not_found_help(ctx: Context, exc: SupervisorNotFound) -> str:
    return (
        f"❌ {exc}\n\n"
        f"Run `{ctx.config.command_prefix}self diag` to see everywhere I looked."
    )


async def _restart_bot(ctx: Context) -> None:
    """Restart by replacing the current interpreter.

    Never call ``supervisorctl restart`` from inside the supervised process: it
    waits for this process to exit while our event loop waits for
    ``supervisorctl`` to finish, creating a deadlock until supervisord kills the
    bot at ``stopwaitsecs``. Re-exec keeps the same PID, so supervisord continues
    monitoring it without participating in the restart.
    """
    await ctx.reply("🔄 Restarting this process… I'll be back in a few seconds.")
    logger.info("Restart requested; scheduling in-process re-exec")
    # Give Telegram time to send the acknowledgement before replacing Python.
    asyncio.get_event_loop().call_later(1.0, _restart_process)


async def _supervisor_status(ctx: Context) -> None:
    runner = _runner(ctx)

    try:
        result = await runner.status()
    except SupervisorNotFound as exc:
        await ctx.reply(_not_found_help(ctx, exc))
        return
    except asyncio.TimeoutError:
        await ctx.reply("❌ supervisorctl timed out. Is supervisord running?")
        return
    except OSError as exc:
        await ctx.reply(f"❌ Could not run supervisorctl: `{exc}`")
        return

    output = result.output or "(no output)"

    # A non-zero exit with usable output still tells the user something.
    if not result.ok and "no such process" in output.lower():
        await ctx.reply(
            f"❓ supervisord has no program named `{runner.process_name}`.\n\n"
            f"Check `SUPERVISOR_PROCESS` matches the `[program:...]` section "
            f"in your supervisord config."
        )
        return

    state = parse_state(output)
    emoji = STATE_EMOJI.get(state, "❓")

    await ctx.reply(
        f"{emoji} **{runner.process_name}** — {state}\n\n"
        f"```\n{truncate(output, 800)}\n```\n"
        f"Bot uptime: `{format_duration(ctx.bot.uptime)}`"
    )


async def _supervisor_logs(ctx: Context) -> None:
    log_file = ctx.config.supervisor.log_file
    if not log_file:
        raise UsageError(
            "Set `SUPERVISOR_LOG_FILE` in your .env to the path of your "
            "stderr log to read it from here."
        )

    lines = 20
    if len(ctx.args) > 1 and ctx.args[1].isdigit():
        lines = min(int(ctx.args[1]), 200)

    path = Path(log_file).expanduser()  # noqa: ASYNC240 - pure string work
    if not await asyncio.to_thread(path.is_file):
        raise UsageError(f"Log file not found: `{path}`")

    try:
        code, out, err = await _run("tail", "-n", str(lines), str(path))
    except FileNotFoundError:
        # No `tail` (unusual, but possible in slim containers): read directly.
        try:
            content = await asyncio.to_thread(
                path.read_text, encoding="utf-8", errors="replace"
            )
            out, code, err = "\n".join(content.splitlines()[-lines:]), 0, ""
        except OSError as exc:
            raise UsageError(f"Could not read the log: `{exc}`") from None

    if code != 0:
        await ctx.reply(f"❌ Could not read logs:\n`{truncate(err, 400)}`")
        return
    await ctx.reply(f"📋 **Last {lines} lines**\n```\n{out or '(empty)'}\n```")


async def _supervisor_diag(ctx: Context) -> None:
    """Report exactly how supervisorctl is being located."""
    supervisor = ctx.config.supervisor

    lines = [
        "🔧 **Supervisor diagnostics**\n",
        "**Configuration**",
        f"  `SUPERVISOR_PROCESS` = `{supervisor.process_name or '(unset)'}`",
        f"  `SUPERVISOR_CONFIG`  = `{supervisor.config_path or '(unset — auto)'}`",
        f"  `SUPERVISOR_CTL`     = `{supervisor.executable or '(unset — auto)'}`",
        f"  `SUPERVISOR_LOG_FILE`= `{supervisor.log_file or '(unset)'}`",
        "",
        "**Discovery**",
        describe_discovery(supervisor.executable),
        "",
        f"**Interpreter**\n  `{sys.executable}`",
    ]

    resolved = resolve_supervisorctl(supervisor.executable)
    if resolved:
        runner = _runner(ctx) if supervisor.enabled else None
        lines.append("\n**Command I will run**")
        if runner is not None:
            lines.append(f"  `{' '.join(runner.build_command('status', runner.process_name))}`")
        else:
            lines.append(f"  `{' '.join(resolved)}` (set SUPERVISOR_PROCESS to enable)")
    else:
        lines.append(
            "\n⚠️ **supervisorctl was not found.**\n"
            "Fix with either:\n"
            "  • `pip install supervisor` in the bot's virtualenv, or\n"
            "  • set `SUPERVISOR_CTL=/full/path/to/supervisorctl` in .env"
        )

    # Verify the program section will actually restart *this* version.
    if supervisor.process_name:
        audit = await asyncio.to_thread(
            audit_program, supervisor.process_name, supervisor.config_path
        )
        lines.append(f"\n**`[program:{supervisor.process_name}]`**")

        if not audit.found:
            lines.append(
                "  ❓ Not found in the config I could read.\n"
                "  If `self status` works, supervisord is reading a different\n"
                "  file — set `SUPERVISOR_CONFIG` to it."
            )
        else:
            lines.append(f"  Config: `{audit.config_file}`")
            lines.append(f"  Command: `{truncate(audit.command or '(unset)', 160)}`")
            if audit.directory:
                lines.append(f"  Directory: `{audit.directory}`")

            for problem in audit.problems:
                lines.append(f"  ❌ {problem}")
            for note in audit.notes:
                lines.append(f"  ⚠️ {note}")
            if not audit.problems and not audit.notes:
                lines.append("  ✅ Looks correct for v2.")

    await ctx.reply("\n".join(lines))


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
