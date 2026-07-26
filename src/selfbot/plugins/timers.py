"""Countdown timers with live-updating messages.

Timers survive restarts: state lives in the database and every active timer is
restored on startup. All arithmetic is in UTC, which fixes the original bug
where naive ``datetime.now()`` was compared against MySQL ``NOW()``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import timedelta

from ..db import Timer, utcnow
from ..errors import UsageError, ValidationError
from ..registry import Context, command
from ..utils.text import (
    format_duration_long,
    progress_bar,
    styled_clock,
    truncate,
)

logger = logging.getLogger(__name__)

CATEGORY = "Timers"

MAX_DURATION = 365 * 86400  # one year


def _new_hash() -> str:
    return secrets.token_hex(4)  # 8 hex chars, collision-safe


def parse_duration(text: str) -> int | None:
    """Parse ``SS``, ``MM:SS``, ``HH:MM:SS``, ``DD:HH:MM:SS`` or ``1h30m``.

    Returns total seconds, or ``None`` when the format is unrecognised.
    """
    text = text.strip().lower()
    if not text:
        return None

    if text.isdigit():
        return int(text)

    if ":" in text:
        parts = text.split(":")
        if len(parts) > 4 or not all(p.isdigit() for p in parts if p != ""):
            return None
        values = [int(p or 0) for p in parts]
        multipliers = [1, 60, 3600, 86400]
        total = 0
        for index, value in enumerate(reversed(values)):
            total += value * multipliers[index]
        return total

    # Compact form: 1d2h30m15s
    import re

    matches = re.findall(r"(\d+)\s*([dhms])", text)
    if not matches or re.sub(r"\d+\s*[dhms]\s*", "", text):
        return None
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(int(value) * units[unit] for value, unit in matches)


def render_timer(timer: Timer, remaining: int) -> str:
    fraction = remaining / timer.duration_seconds if timer.duration_seconds else 0
    return (
        f"⏰ **{timer.title.upper()}**\n\n"
        f"{styled_clock(remaining)}\n\n"
        f"`{progress_bar(fraction)}`\n\n"
        f"❌ `dismiss {timer.hash}`   🔄 `resend {timer.hash}`"
    )


def render_finished(timer: Timer) -> str:
    return (
        f"🔔 **{timer.title.upper()}**\n\n"
        f"⏰ Time's up!\n"
        f"⏱ Duration: {format_duration_long(timer.duration_seconds)}\n"
        f"✅ {utcnow():%H:%M:%S} UTC"
    )


def _interval_for(remaining: int) -> int:
    """Update frequently near the end, lazily when far out.

    Keeps edits well under Telegram's rate limit for long timers.
    """
    if remaining <= 30:
        return 2
    if remaining <= 300:
        return 5
    if remaining <= 3600:
        return 30
    return 300


async def run_timer(bot: object, timer_hash: str) -> None:
    """Background loop that keeps one timer's message current."""
    db = bot.db  # type: ignore[attr-defined]
    try:
        while True:
            timer = await db.get_timer(timer_hash)
            if timer is None or not timer.is_active:
                return

            remaining = timer.remaining_seconds
            if remaining <= 0:
                await finish_timer(bot, timer)
                return

            if timer.message_id:
                await bot.edit(  # type: ignore[attr-defined]
                    _MessageRef(bot, timer.chat_id, timer.message_id),
                    render_timer(timer, remaining),
                )

            await asyncio.sleep(min(_interval_for(remaining), max(1, remaining)))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Timer %s crashed", timer_hash)
    finally:
        bot.timer_tasks.pop(timer_hash, None)  # type: ignore[attr-defined]


class _MessageRef:
    """Adapter so ``bot.edit`` can target a message we only know by ID."""

    def __init__(self, bot: object, chat_id: int, message_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = message_id

    async def edit(self, text: str, **kwargs: object) -> object:
        return await self._bot.client.edit_message(  # type: ignore[attr-defined]
            self._chat_id, self._message_id, text, **kwargs
        )


async def finish_timer(bot: object, timer: Timer) -> None:
    await bot.db.deactivate_timer(timer.hash)  # type: ignore[attr-defined]
    try:
        await bot.client.send_message(  # type: ignore[attr-defined]
            timer.chat_id, render_finished(timer)
        )
    except Exception:
        logger.warning("Could not deliver completion for timer %s", timer.hash)
    logger.info("Timer %s (%s) finished", timer.hash, timer.title)


async def start_timer_task(bot: object, timer_hash: str) -> None:
    existing = bot.timer_tasks.get(timer_hash)  # type: ignore[attr-defined]
    if existing and not existing.done():
        existing.cancel()
    task = asyncio.ensure_future(run_timer(bot, timer_hash))
    task.set_name(f"timer-{timer_hash}")
    bot.timer_tasks[timer_hash] = task  # type: ignore[attr-defined]


async def restore_timers(bot: object) -> None:
    """Re-arm every active timer after a restart."""
    timers = await bot.db.list_active_timers()  # type: ignore[attr-defined]
    restored = expired = 0
    for timer in timers:
        if timer.remaining_seconds > 0:
            await start_timer_task(bot, timer.hash)
            restored += 1
        else:
            await finish_timer(bot, timer)
            expired += 1
    if restored or expired:
        logger.info("Timers restored: %d active, %d expired", restored, expired)


@command(
    "settimer",
    category=CATEGORY,
    min_args=2,
    usage="settimer <duration> <title>",
    examples=("settimer 10:00 tea", "settimer 1h30m gym", "settimer 90 pasta"),
)
async def cmd_settimer(ctx: Context) -> None:
    """Start a countdown that updates live in the chat."""
    seconds = parse_duration(ctx.args[0])
    if seconds is None:
        raise UsageError(
            "Could not read that duration.\n"
            "Try `90`, `10:00`, `1:30:00`, `2:12:00:00` or `1h30m`."
        )
    if seconds <= 0:
        raise ValidationError("Duration must be greater than zero.")
    if seconds > MAX_DURATION:
        raise ValidationError("Maximum timer length is one year.")

    title = " ".join(ctx.args[1:]).strip()
    if len(title) > 200:
        raise ValidationError("Title is too long (max 200 characters).")

    timer = Timer(
        hash=_new_hash(),
        user_id=ctx.sender_id,
        chat_id=ctx.chat_id,
        title=title,
        duration_seconds=seconds,
        end_time=utcnow() + timedelta(seconds=seconds),
    )

    message = await ctx.reply(render_timer(timer, seconds))
    timer.message_id = message.id

    await ctx.db.create_timer(timer)
    await start_timer_task(ctx.bot, timer.hash)
    logger.info("Timer %s set for %ss by %s", timer.hash, seconds, ctx.sender_id)


@command("activetimers", category=CATEGORY, usage="activetimers", aliases=("timers",))
async def cmd_activetimers(ctx: Context) -> None:
    """List running timers."""
    timers = await ctx.db.list_active_timers(None if ctx.is_sudo else ctx.sender_id)
    timers = [t for t in timers if t.remaining_seconds > 0]

    if not timers:
        await ctx.reply("ℹ️ No active timers.")
        return

    lines = [f"⏰ **Active timers** ({len(timers)})\n"]
    for timer in timers:
        lines.append(
            f"**{truncate(timer.title, 40)}** — {styled_clock(timer.remaining_seconds)}\n"
            f"  🔄 `resend {timer.hash}`   ❌ `dismiss {timer.hash}`"
        )
    await ctx.reply("\n".join(lines))


async def _load_owned(ctx: Context, timer_hash: str) -> Timer:
    timer = await ctx.db.get_timer(timer_hash)
    if timer is None or not timer.is_active:
        raise ValidationError(f"No active timer `{timer_hash}`.")
    if timer.user_id != ctx.sender_id and not ctx.is_sudo:
        raise ValidationError("That timer belongs to someone else.")
    return timer


@command(
    "dismiss",
    category=CATEGORY,
    min_args=1,
    usage="dismiss <hash>",
    examples=("dismiss a1b2c3d4",),
)
async def cmd_dismiss(ctx: Context) -> None:
    """Cancel a running timer."""
    timer = await _load_owned(ctx, ctx.args[0])

    task = ctx.bot.timer_tasks.pop(timer.hash, None)
    if task and not task.done():
        task.cancel()

    await ctx.db.deactivate_timer(timer.hash)

    if timer.message_id:
        try:
            await ctx.client.delete_messages(timer.chat_id, timer.message_id)
        except Exception:
            logger.debug("Could not delete timer message", exc_info=True)

    await ctx.reply(f"🗑 Dismissed **{truncate(timer.title, 40)}**.")


@command("resend", category=CATEGORY, min_args=1, usage="resend <hash>")
async def cmd_resend(ctx: Context) -> None:
    """Repost a timer's message in the current chat."""
    timer = await _load_owned(ctx, ctx.args[0])
    remaining = timer.remaining_seconds
    if remaining <= 0:
        raise ValidationError("That timer has already finished.")

    task = ctx.bot.timer_tasks.pop(timer.hash, None)
    if task and not task.done():
        task.cancel()

    if timer.message_id and timer.chat_id == ctx.chat_id:
        try:
            await ctx.client.delete_messages(timer.chat_id, timer.message_id)
        except Exception:
            pass

    message = await ctx.reply(render_timer(timer, remaining))
    await ctx.db.update_timer_message(timer.hash, message.id, ctx.chat_id)
    await start_timer_task(ctx.bot, timer.hash)
