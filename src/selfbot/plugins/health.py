"""Health command and optional localhost /healthz endpoint."""

from __future__ import annotations

import logging
from typing import Any

from ..registry import Context, command
from ..utils.text import format_duration

logger = logging.getLogger(__name__)

CATEGORY = "Core"


@command(
    "health",
    category=CATEGORY,
    sudo_only=True,
    usage="health [metrics]",
    aliases=("diag-health",),
)
async def cmd_health(ctx: Context) -> None:
    """Show runtime health: tasks, memory, DB, AI and recent API failures."""
    bot = ctx.bot
    metrics = bot.metrics
    snapshot = await metrics.snapshot(bot)

    if ctx.args and ctx.args[0].lower() == "metrics":
        lines = ["📈 **Metrics**\n"]
        for key, value in sorted(snapshot["counters"].items()):
            lines.append(f"  {key}: `{value}`")
        lines.append(
            f"\nevent-loop lag: `{snapshot['event_loop_lag_ms']} ms` · "
            f"rss: `{snapshot['rss_mb']} MB`"
        )
        await ctx.reply("\n".join(lines))
        return

    rss = f"{snapshot['rss_mb']} MB" if snapshot["rss_mb"] is not None else "n/a"
    db_icon = "🟢" if snapshot["db_connected"] else "🔴"
    state_icon = "🟢" if snapshot["state"] == "active" else "🔴"

    try:
        from .ai import get_manager

        manager = get_manager(ctx)
        statuses = await manager.status()
        available = sum(1 for s in statuses if s.available)
        cooling = sum(1 for s in statuses if s.cooldown_remaining > 0)
        ai_line = (
            f"AI: `{available}/{len(statuses)} up`"
            + (f" · `{cooling} cooling`" if cooling else "")
        )
    except Exception as exc:
        ai_line = f"AI: `unavailable ({type(exc).__name__})`"

    lines = [
        "🩺 **SelfBot health**\n",
        f"{state_icon} State: `{snapshot['state']}` · up `{format_duration(snapshot['uptime_seconds'])}`",
        f"🧠 Tasks: `{snapshot['tasks']}` (timers, spam, background)",
        f"💾 RSS: `{rss}` · event-loop lag: `{snapshot['event_loop_lag_ms']} ms`",
        f"{db_icon} DB: `{snapshot['db_backend']}` · "
        f"`{snapshot['db_tables']}` tables · connected: `{snapshot['db_connected']}`",
        ai_line,
    ]

    counters = snapshot["counters"]
    lines.append(
        f"\n📨 messages: `{counters.get('messages_seen', 0)}` · "
        f"commands: `{counters.get('commands_run', 0)}` ok / "
        f"`{counters.get('commands_failed', 0)}` failed · "
        f"AI: `{counters.get('ai_requests', 0)}`"
    )

    failures = snapshot.get("recent_failures") or []
    if failures:
        lines.append(f"\n⚠️ **Recent API failures ({len(failures)})**")
        for event in failures[:5]:
            status = f" HTTP {event['status']}" if event.get("status") else ""
            lines.append(
                f"  • `{event['source']}`{status} — {event['message']} "
                f"(~{int(event['age_seconds'])}s ago)"
            )
    else:
        lines.append("\n✅ No recent API failures.")

    await ctx.reply("\n".join(lines))


# --------------------------------------------------------------------------
# Optional HTTP health endpoint
# --------------------------------------------------------------------------


async def start_health_server(bot: Any) -> Any:
    """Start an aiohttp /healthz server when HEALTH_PORT is configured.

    Bound to 127.0.0.1 by default so it is safe for Docker HEALTHCHECKs without
    exposing data on a public interface. Never fatal — returns None if the
    server cannot start.
    """
    health_cfg = bot.config.health
    if not health_cfg.enabled:
        return None

    try:
        from aiohttp import web

        async def healthz(_request: Any) -> web.Response:
            snap = await bot.metrics.snapshot(bot)
            ok = bool(snap["db_connected"])
            return web.json_response(
                {
                    "status": "ok" if ok else "degraded",
                    "uptime": snap["uptime_seconds"],
                    "state": snap["state"],
                    "db": snap["db_connected"],
                    "tasks": snap["tasks"],
                    "rss_mb": snap["rss_mb"],
                },
                status=200 if ok else 503,
            )

        async def readyz(_request: Any) -> web.Response:
            return web.json_response({"ready": True})

        app = web.Application()
        app.router.add_get("/healthz", healthz)
        app.router.add_get("/readyz", readyz)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host=health_cfg.bind, port=health_cfg.port)
        await site.start()
        logger.info(
            "Health endpoint listening on http://%s:%s/healthz",
            health_cfg.bind,
            health_cfg.port,
        )
        return runner
    except Exception:
        logger.exception("Could not start health endpoint")
        return None


async def stop_health_server(runner: Any) -> None:
    if runner is None:
        return
    try:
        await runner.cleanup()
    except Exception:
        logger.debug("Error stopping health endpoint", exc_info=True)
