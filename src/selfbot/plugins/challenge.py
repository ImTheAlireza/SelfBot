"""Automated challenge participant tagging with real-time collision prevention."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from telethon import types
from telethon.errors import FloodWaitError

from ..errors import UsageError, ValidationError
from ..registry import Context, command
from ..utils.text import format_duration

logger = logging.getLogger(__name__)

CATEGORY = "Automation"


@dataclass(slots=True)
class ChallengeState:
    """In-memory state for an active challenge tagging session."""

    chat_id: int
    challenge_msg_id: int
    task: asyncio.Task[Any] | None = None
    candidates: list[Any] = field(default_factory=list)
    tagged_user_ids: set[int] = field(default_factory=set)
    tagged_usernames: set[str] = field(default_factory=set)
    tagged_by_us_count: int = 0
    tagged_by_others_count: int = 0
    total_candidates: int = 0
    started_at: float = field(default_factory=time.monotonic)
    min_delay: float = 25.0
    max_delay: float = 55.0
    batch_size: int = 1
    is_cancelled: bool = False

    def cancel(self) -> None:
        self.is_cancelled = True
        if self.task and not self.task.done():
            self.task.cancel()


def extract_mentions(message: Any) -> tuple[set[int], set[str]]:
    """Extract mentioned user IDs and usernames from a message."""
    user_ids: set[int] = set()
    usernames: set[str] = set()
    text = getattr(message, "message", "") or getattr(message, "text", "") or ""
    entities = getattr(message, "entities", None) or []

    for entity in entities:
        if isinstance(entity, types.MessageEntityMentionName):
            user_ids.add(entity.user_id)
        elif isinstance(entity, types.MessageEntityMention):
            mention = text[entity.offset : entity.offset + entity.length].lstrip("@").lower()
            if mention:
                usernames.add(mention)
        elif isinstance(entity, types.MessageEntityTextUrl):
            url = getattr(entity, "url", "") or ""
            if url.startswith("tg://user?id="):
                try:
                    uid = int(url.split("=")[-1])
                    user_ids.add(uid)
                except ValueError:
                    pass

    return user_ids, usernames


def is_active_user(user: Any, now_dt: datetime) -> bool:
    """Return True if user is not a bot/deleted and was recently active."""
    if getattr(user, "bot", False) or getattr(user, "deleted", False):
        return False

    status = getattr(user, "status", None)
    if status is None:
        return False

    if isinstance(status, (types.UserStatusOnline, types.UserStatusRecently)):
        return True

    if isinstance(status, types.UserStatusOffline):
        was_online = getattr(status, "was_online", None)
        if was_online is not None:
            was_online_utc = (
                was_online if was_online.tzinfo else was_online.replace(tzinfo=timezone.utc)
            )
            # Active within the last 3 days
            if (now_dt - was_online_utc).total_seconds() <= 86400 * 3:
                return True

    return False


def format_user_mention(user: Any) -> str:
    """Format a clickable mention or username for a user."""
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    user_id = getattr(user, "id", None)
    first_name = (getattr(user, "first_name", "") or "").strip()
    name = first_name or "User"
    return f"[{name}](tg://user?id={user_id})" if user_id else f"@{username or name}"


async def _scan_existing_mentions(
    client: Any, chat_id: int, challenge_msg_id: int, limit: int = 1000
) -> tuple[set[int], set[str]]:
    """Scan recent messages replying to the challenge message for already tagged users."""
    tagged_uids: set[int] = set()
    tagged_unames: set[str] = set()

    try:
        async for msg in client.iter_messages(chat_id, min_id=max(0, challenge_msg_id - 1), limit=limit):
            reply_to = getattr(msg, "reply_to", None)
            reply_to_msg_id = getattr(reply_to, "reply_to_msg_id", None)
            if reply_to_msg_id == challenge_msg_id or msg.id == challenge_msg_id:
                uids, unames = extract_mentions(msg)
                tagged_uids.update(uids)
                tagged_unames.update(unames)
    except Exception:
        logger.debug("Could not scan existing replies in chat %s", chat_id, exc_info=True)

    return tagged_uids, tagged_unames


async def _challenge_worker(ctx: Context, state: ChallengeState) -> None:
    """Background worker that posts mentions in batches with natural random delays."""
    bot = ctx.bot
    chat_id = state.chat_id
    client = ctx.client

    try:
        # Randomize the candidates order
        random.shuffle(state.candidates)

        while state.candidates and not state.is_cancelled:
            # Pick a batch of non-tagged users
            batch: list[Any] = []
            while state.candidates and len(batch) < state.batch_size:
                user = state.candidates.pop()
                uid = getattr(user, "id", None)
                uname = (getattr(user, "username", "") or "").lower()

                if uid in state.tagged_user_ids or (uname and uname in state.tagged_usernames):
                    continue
                batch.append(user)

            if not batch:
                break

            # Format mention text
            text = " ".join(format_user_mention(u) for u in batch)

            # Send reply to challenge message
            sent_ok = False
            for _attempt in range(3):
                if state.is_cancelled:
                    return
                try:
                    await client.send_message(
                        chat_id,
                        text,
                        reply_to=state.challenge_msg_id,
                    )
                    sent_ok = True
                    break
                except FloodWaitError as exc:
                    wait = min(exc.seconds + 2, 300)
                    logger.warning("FloodWait during challenge tagging: sleeping %ss", wait)
                    await asyncio.sleep(wait)
                except Exception as exc:
                    logger.warning("Failed to send challenge tag batch: %s", exc)
                    await asyncio.sleep(5.0)

            if not sent_ok:
                logger.error("Aborting challenge tagging due to repeated send failures.")
                break

            # Mark as tagged
            for u in batch:
                uid = getattr(u, "id", None)
                uname = (getattr(u, "username", "") or "").lower()
                if uid:
                    state.tagged_user_ids.add(uid)
                if uname:
                    state.tagged_usernames.add(uname)
                state.tagged_by_us_count += 1

            # Sleep natural random delay between batches
            if state.candidates and not state.is_cancelled:
                delay = random.uniform(state.min_delay, state.max_delay)
                await asyncio.sleep(delay)

        if not state.is_cancelled:
            elapsed = format_duration(time.monotonic() - state.started_at)
            await ctx.respond(
                f"🎉 **Challenge tagging finished!**\n"
                f"• Tagged by bot: **{state.tagged_by_us_count}** members\n"
                f"• Already tagged by others: **{state.tagged_by_others_count}**\n"
                f"• Total duration: `{elapsed}`"
            )

    except asyncio.CancelledError:
        logger.info("Challenge worker in chat %s cancelled", chat_id)
        raise
    except Exception as exc:
        logger.exception("Challenge worker in chat %s crashed", chat_id)
        await ctx.respond(f"❌ Challenge tagging error: `{exc}`")
    finally:
        bot.challenge_tasks.pop(chat_id, None)


def _parse_delay_range(arg: str) -> tuple[float, float]:
    """Parse a delay range like '30-60' or single delay like '30'."""
    if "-" in arg:
        try:
            p1, p2 = arg.split("-", 1)
            min_d, max_d = float(p1), float(p2)
            if min_d <= 0 or max_d < min_d:
                raise ValueError
            return min_d, max_d
        except ValueError:
            raise UsageError("Invalid delay range. Example: `startchallenge 2 30-60`") from None
    if arg.replace(".", "", 1).isdigit():
        val = float(arg)
        if val <= 0:
            raise UsageError("Delay must be greater than 0.")
        return max(5.0, val * 0.8), val * 1.2
    raise UsageError("Invalid delay range. Example: `startchallenge 2 30-60`")


@command(
    "startchallenge",
    category=CATEGORY,
    sudo_only=True,
    requires_reply=True,
    usage="startchallenge [count] [min_delay-max_delay]",
    examples=(
        "startchallenge",
        "startchallenge 2",
        "startchallenge 2 30-60",
        "startchallenge 1 20-40",
    ),
)
async def cmd_startchallenge(ctx: Context) -> None:
    """Start automated member tagging on the replied challenge message.

    By default, tags 1 member per message. The first argument specifies the
    number of users to tag per message, and the optional second argument specifies
    the random delay range (in seconds).

    Filters out bots, deleted accounts, admins and inactive users, and avoids
    tagging anyone already tagged by other admins.
    """
    bot = ctx.bot
    if not hasattr(bot, "challenge_tasks"):
        bot.challenge_tasks = {}

    if ctx.chat_id in bot.challenge_tasks:
        raise ValidationError(
            "A challenge tagging session is already active in this chat.\n"
            "Use `stopchallenge` to cancel it first."
        )

    replied = await ctx.get_reply_message()
    if not replied:
        raise UsageError("Reply to the challenge message to start tagging.")

    challenge_msg_id = replied.id

    # Defaults: 1 user per message, 25-55s random delay
    batch_size = 1
    min_delay, max_delay = 25.0, 55.0

    if ctx.args:
        first_arg = ctx.args[0]
        if first_arg.isdigit():
            count = int(first_arg)
            if count < 1:
                raise UsageError("Number of tags per message must be at least 1.")
            batch_size = min(count, 10)  # Safe upper cap
            if len(ctx.args) > 1:
                min_delay, max_delay = _parse_delay_range(ctx.args[1])
        elif "-" in first_arg:
            # Fallback if user passed delay range as first argument
            min_delay, max_delay = _parse_delay_range(first_arg)
            if len(ctx.args) > 1 and ctx.args[1].isdigit():
                batch_size = min(max(1, int(ctx.args[1])), 10)
        else:
            raise UsageError("Usage: `startchallenge [count] [min_delay-max_delay]`")

    status = await ctx.reply("🔍 Scanning chat members and previous mentions…")

    # 1. Fetch admins to exclude
    admin_ids: set[int] = {ctx.config.sudo_user_id}
    if getattr(bot, "me", None) and getattr(bot.me, "id", None):
        admin_ids.add(bot.me.id)

    try:
        admins = await ctx.client.get_participants(
            ctx.chat_id, filter=types.ChannelParticipantsAdmins
        )
        for a in admins:
            if getattr(a, "id", None):
                admin_ids.add(a.id)
    except Exception:
        logger.debug("Could not fetch admin list for chat %s", ctx.chat_id, exc_info=True)

    # 2. Scan existing mentions on the challenge message
    tagged_uids, tagged_unames = await _scan_existing_mentions(
        ctx.client, ctx.chat_id, challenge_msg_id
    )

    # 3. Collect active candidates
    now_dt = datetime.now(timezone.utc)
    candidates: list[Any] = []
    seen_ids: set[int] = set()

    try:
        async for user in ctx.client.iter_participants(ctx.chat_id):
            uid = getattr(user, "id", None)
            if not uid or uid in seen_ids or uid in admin_ids:
                continue
            seen_ids.add(uid)

            uname = (getattr(user, "username", "") or "").lower()
            if uid in tagged_uids or (uname and uname in tagged_unames):
                continue

            if is_active_user(user, now_dt):
                candidates.append(user)
    except Exception as exc:
        await ctx.bot.edit(status, f"❌ Failed to fetch chat participants: `{exc}`")
        return

    if not candidates:
        await ctx.bot.edit(
            status,
            "ℹ️ No eligible active members found to tag (all active members may have already been tagged).",
        )
        return

    # 4. Create and start state
    state = ChallengeState(
        chat_id=ctx.chat_id,
        challenge_msg_id=challenge_msg_id,
        candidates=candidates,
        tagged_user_ids=tagged_uids,
        tagged_usernames=tagged_unames,
        tagged_by_others_count=len(tagged_uids | tagged_unames),
        total_candidates=len(candidates),
        min_delay=min_delay,
        max_delay=max_delay,
        batch_size=batch_size,
    )

    task = asyncio.ensure_future(_challenge_worker(ctx, state))
    task.set_name(f"challenge-{ctx.chat_id}")
    state.task = task
    bot.challenge_tasks[ctx.chat_id] = state

    await ctx.bot.edit(
        status,
        f"🚀 **Challenge tagging started!**\n\n"
        f"• Target Message: `{challenge_msg_id}`\n"
        f"• Active Candidates: **{len(candidates)}** members\n"
        f"• Already Tagged by Others: **{state.tagged_by_others_count}**\n"
        f"• Speed: `{min_delay:.0f}-{max_delay:.0f}s` per batch ({batch_size} users/msg)\n\n"
        f"Send `stopchallenge` anytime to halt.",
    )


@command(
    "stopchallenge",
    category=CATEGORY,
    sudo_only=True,
    usage="stopchallenge",
    aliases=("cancelchallenge", "endchallenge"),
)
async def cmd_stopchallenge(ctx: Context) -> None:
    """Stop the active challenge tagging session and clear all data from memory."""
    challenge_tasks = getattr(ctx.bot, "challenge_tasks", {})
    state = challenge_tasks.get(ctx.chat_id)
    if not state or state.is_cancelled:
        await ctx.reply("ℹ️ No challenge tagging session is active in this chat.")
        return

    state.cancel()
    challenge_tasks.pop(ctx.chat_id, None)

    elapsed = format_duration(time.monotonic() - state.started_at)
    await ctx.reply(
        f"🛑 **Challenge tagging stopped.**\n\n"
        f"• Tagged by bot: **{state.tagged_by_us_count}**\n"
        f"• Already tagged by others: **{state.tagged_by_others_count}**\n"
        f"• Time elapsed: `{elapsed}`\n\n"
        f"🧹 All session data cleared from memory.",
    )


@command(
    "challengestatus",
    category=CATEGORY,
    sudo_only=True,
    usage="challengestatus",
    aliases=("cstatus",),
)
async def cmd_challengestatus(ctx: Context) -> None:
    """Show the status of the current challenge tagging session."""
    challenge_tasks = getattr(ctx.bot, "challenge_tasks", {})
    state = challenge_tasks.get(ctx.chat_id)
    if not state or state.is_cancelled:
        await ctx.reply("ℹ️ No challenge tagging session is active in this chat.")
        return

    remaining = len(state.candidates)
    elapsed = format_duration(time.monotonic() - state.started_at)

    await ctx.reply(
        f"📊 **Challenge Tagging Status**\n\n"
        f"• Target Message: `{state.challenge_msg_id}`\n"
        f"• Remaining: **{remaining}** / {state.total_candidates}\n"
        f"• Tagged by bot: **{state.tagged_by_us_count}**\n"
        f"• Tagged by others: **{state.tagged_by_others_count}**\n"
        f"• Delay: `{state.min_delay:.0f}-{state.max_delay:.0f}s` ({state.batch_size} users/msg)\n"
        f"• Elapsed: `{elapsed}`",
    )
