"""Auto-delete filters and match-scoped history deletion.

Reply to a message and send ``filter`` (or ``filter -after``) to save its text
as a delete filter for THIS chat only: from then on, every NEW message from
anyone whose text contains the saved pattern is deleted automatically.
``filter -before`` is retroactive: it scans this chat and deletes every
EXISTING message whose text matches the replied message — contains by
default, identical only with ``-exact``. Non-matching messages are never
touched.

Filters are always scoped to the chat they were created in (``-here`` is
accepted as an explicit reminder of that and does nothing). Matching is
case-insensitive; ``-exact`` switches to whole-message equality.
"""

from __future__ import annotations

import asyncio
import logging

from telethon.errors import FloodWaitError

from ..errors import UsageError, ValidationError
from ..registry import Context, command
from ..utils.text import truncate

logger = logging.getLogger(__name__)

CATEGORY = "Messaging"

#: Maximum length of one saved pattern (keeps the primary key sane).
MAX_PATTERN = 256

#: Telegram caps batched deletions at 100 messages per call.
DELETE_BATCH = 100

#: Flags understood by `filter`, in the order they appear in help text.
FLAGS = ("-after", "-before", "-clear", "-exact", "-here", "-list", "-off")

USAGE = "`filter [-after | -before] [-exact]` · `filter -list` · `filter -clear` · `filter -off <text>`"


# ---------------------------------------------------------------------------
# New-message watcher (wired into Bot._handle_message)
# ---------------------------------------------------------------------------


async def chat_filters(bot: object, chat_id: int) -> list[object]:
    """Cached per-chat delete filters, mirroring the auto-reply cache."""
    cache = getattr(bot, "_filter_cache", None)
    if cache is None:
        return await bot.db.list_delete_filters(chat_id)  # type: ignore[attr-defined]
    cached = cache.get(chat_id)
    if cached is not None:
        return cached
    rules = await bot.db.list_delete_filters(chat_id)  # type: ignore[attr-defined]
    cache[chat_id] = rules
    return rules


def text_matches(pattern: str, exact: bool, text: str) -> bool:
    """Case-insensitive contains/exact match of ``text`` against ``pattern``."""
    needle = pattern.strip().casefold()
    if not needle:
        return False
    candidate = text.strip().casefold()
    if exact:
        return candidate == needle
    return needle in candidate


def matches(filter_rule: object, text: str) -> bool:
    """Case-insensitive contains/exact match against a saved pattern."""
    return text_matches(
        filter_rule.pattern,  # type: ignore[attr-defined]
        bool(filter_rule.exact),  # type: ignore[attr-defined]
        text,
    )


async def delete_if_filtered(bot: object, event: object, text: str) -> bool:
    """Delete ``event``'s message when it matches a saved filter. True if deleted."""
    chat_id = getattr(event, "chat_id", None)
    if chat_id is None:
        return False

    # Never eat the owner's own commands.
    is_own = bool(getattr(event, "out", False))
    prefix = bot.config.command_prefix  # type: ignore[attr-defined]
    if is_own and prefix and text.startswith(prefix):
        return False

    try:
        rules = await chat_filters(bot, chat_id)
    except Exception:
        logger.debug("Could not load delete filters for chat %s", chat_id, exc_info=True)
        return False
    if not rules:
        return False

    matched = next((rule for rule in rules if matches(rule, text)), None)
    if matched is None:
        return False

    message_id = getattr(getattr(event, "message", None), "id", None)
    if message_id is None:
        message_id = getattr(event, "id", None)
    if not isinstance(message_id, int):
        return False

    try:
        await bot.client.delete_messages(chat_id, [message_id])  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning(
            "Delete filter %r could not remove message %s in chat %s: %s",
            matched.pattern,
            message_id,
            chat_id,
            exc,
        )
        return False

    bot.metrics.incr("filter_deletes")  # type: ignore[attr-defined]
    logger.info(
        "Delete filter %r removed message %s in chat %s", matched.pattern, message_id, chat_id
    )
    return True


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@command(
    "filter",
    category=CATEGORY,
    sudo_only=True,
    aliases=("filters",),
    usage="filter [-after | -before] [-exact] | filter -list | -clear | -off <text>",
    examples=(
        "filter",
        "filter -after",
        "filter -exact",
        "filter -before",
        "filter -exact -before",
        "filter -list",
        "filter -off <text>",
        "filter -clear",
    ),
)
async def cmd_filter(ctx: Context) -> None:
    """Save a per-chat auto-delete filter from the replied message.

    Reply to a message and send `filter` (or `filter -after`): every NEW
    message in this chat from anyone whose text contains it is deleted
    automatically, going forward. `filter -before` instead scans this chat's
    existing history and deletes every message whose text matches the replied
    message; add `-exact` to only delete identical messages. Non-matching
    messages are never touched, and filters only apply to the chat where they
    were created.
    """
    ctx.require_single_dash_flags(*FLAGS)
    flags, off_words, stray = _parse_args(ctx.args)

    actions = flags & {"-after", "-before", "-list", "-clear", "-off"}
    if len(actions) > 1:
        raise UsageError(f"Pick one action.\nUsage: {USAGE}")
    if actions and stray:
        raise UsageError(f"Unexpected argument `{stray[0]}`.\nUsage: {USAGE}")
    if "-exact" in flags and flags & {"-list", "-clear", "-off"}:
        raise UsageError(
            "`-exact` only applies to saving a filter or history deletion.\n"
            "Usage: `filter [-after | -before] [-exact]`"
        )

    if "-list" in flags:
        await _list_filters(ctx)
    elif "-clear" in flags:
        await _clear_filters(ctx)
    elif "-after" in flags:
        # "After" = the future: save a persistent filter for new messages.
        await _save_filter(ctx, exact="-exact" in flags)
    elif "-before" in flags:
        # "Before" = the history: one-shot sweep of matching existing messages.
        await _delete_matching_history(ctx, exact="-exact" in flags)
    elif "-off" in flags:
        await _remove_filter(ctx, off_words)
    else:
        if stray:
            raise UsageError(
                "Reply to the message whose text should become the filter.\n"
                f"Usage: {USAGE}"
            )
        await _save_filter(ctx, exact="-exact" in flags)


def _parse_args(args: list[str]) -> tuple[set[str], list[str], list[str]]:
    """Split args into (flags, words after -off, stray words)."""
    flags: set[str] = set()
    off_words: list[str] = []
    stray: list[str] = []
    collecting_off = False
    for arg in args:
        lowered = arg.lower()
        if lowered in FLAGS:
            flags.add(lowered)
            collecting_off = lowered == "-off"
            continue
        if arg.startswith("-") and len(arg) > 1:
            raise UsageError(
                f"Unknown flag `{arg}`.\nFlags: {', '.join('`' + f + '`' for f in FLAGS)}"
            )
        if collecting_off:
            off_words.append(arg)
        else:
            stray.append(arg)
    return flags, off_words, stray


# -- save --------------------------------------------------------------------


async def _save_filter(ctx: Context, *, exact: bool) -> None:
    if not ctx.event.is_reply:
        raise UsageError(
            "Reply to the message whose text should become the filter.\n"
            f"Usage: {USAGE}"
        )
    pattern = await _replied_text(ctx)
    if not pattern:
        raise ValidationError("The replied message has no text to filter.")
    if len(pattern) > MAX_PATTERN:
        raise ValidationError(f"Keep filters under {MAX_PATTERN} characters.")

    await ctx.db.add_delete_filter(
        ctx.chat_id, pattern, exact=exact, created_by=ctx.sender_id
    )
    ctx.bot.invalidate_filter_cache(ctx.chat_id)

    mode = "**exactly matches**" if exact else "**contains**"
    await ctx.reply(
        "🗑 **Delete filter saved for this chat.**\n"
        f"New messages from anyone whose text {mode} "
        f"`{truncate(pattern, 100)}` will be deleted automatically.\n"
        "`filter -list` · `filter -off <text>` · `filter -clear`"
    )


# -- list / clear / remove ---------------------------------------------------


async def _list_filters(ctx: Context) -> None:
    rules = await ctx.db.list_delete_filters(ctx.chat_id)
    if not rules:
        await ctx.reply(
            "ℹ️ No delete filters in this chat. "
            "Reply to a message and send `filter` to create one."
        )
        return
    lines = [
        f"• {'🎯 exact' if rule.exact else '🔤 contains'} — `{truncate(rule.pattern, 80)}`"
        for rule in rules
    ]
    await ctx.reply(f"🗑 **Delete filters in this chat ({len(rules)})**\n" + "\n".join(lines))


async def _clear_filters(ctx: Context) -> None:
    existing = await ctx.db.list_delete_filters(ctx.chat_id)
    if not existing:
        await ctx.reply("ℹ️ No delete filters in this chat.")
        return
    if not await ctx.bot.confirm(
        ctx.event,
        f"⚠️ Remove all {len(existing)} delete filter(s) in this chat?",
    ):
        await ctx.reply("👍 Cancelled.")
        return
    removed = await ctx.db.clear_delete_filters(ctx.chat_id)
    ctx.bot.invalidate_filter_cache(ctx.chat_id)
    await ctx.reply(f"✅ Removed {removed} delete filter(s) in this chat.")


async def _remove_filter(ctx: Context, off_words: list[str]) -> None:
    pattern = " ".join(off_words).strip()
    if not pattern:
        if ctx.event.is_reply:
            pattern = await _replied_text(ctx)
        if not pattern:
            raise UsageError("Usage: `filter -off <text>` (or reply to a filtered message)")
    removed = await ctx.db.remove_delete_filter(ctx.chat_id, pattern)
    if not removed:
        await ctx.reply(f"ℹ️ No saved filter `{truncate(pattern, 80)}` in this chat.")
        return
    ctx.bot.invalidate_filter_cache(ctx.chat_id)
    await ctx.reply(f"✅ Removed filter `{truncate(pattern, 80)}`.")


# -- one-shot history deletion (-before) --------------------------------------


async def _delete_matching_history(ctx: Context, *, exact: bool) -> None:
    """Delete every EXISTING message in this chat matching the replied text."""
    if not ctx.event.is_reply:
        raise UsageError(
            "Reply to the message whose text should be matched.\n"
            "Usage: `filter -before [-exact]`"
        )
    pattern = await _replied_text(ctx)
    if not pattern:
        raise ValidationError("The replied message has no text to match.")

    word = "is identical to" if exact else "contains"
    if not await ctx.bot.confirm(
        ctx.event,
        (
            f"⚠️ Scan this chat and delete every message whose text {word} "
            f"`{truncate(pattern, 100)}`? Other messages are not touched. "
            "This cannot be undone."
        ),
    ):
        await ctx.reply("👍 Cancelled.")
        return

    deleted = await _delete_matching_range(ctx, pattern=pattern, exact=exact)
    if deleted == 0:
        await ctx.reply("ℹ️ Nothing matched in this chat.")
        return
    scope = "identical to" if exact else "containing"
    await ctx.respond(f"🗑 Deleted **{deleted}** message(s) {scope} the filter in this chat.")


async def _replied_text(ctx: Context) -> str:
    """The replied message's text, or an empty string when it has none."""
    replied = await ctx.get_reply_message()
    if not replied:
        return ""
    return (
        getattr(replied, "raw_text", None) or getattr(replied, "text", "") or ""
    ).strip()


async def _delete_matching_range(ctx: Context, *, pattern: str, exact: bool) -> int:
    """Sweep this chat, deleting only messages whose text matches.

    Bounded at the command message so messages arriving mid-sweep are left
    alone; everything older is scanned.
    """
    deleted = 0
    batch: list[int] = []
    command_id = getattr(ctx.event, "id", None)
    # Telethon's forward iterator applies `max_id` as an offset (a None value
    # would crash it), so only pass it when we have an integer command id.
    kwargs: dict[str, int] = (
        {"max_id": command_id + 1} if isinstance(command_id, int) else {}
    )
    async for message in ctx.client.iter_messages(ctx.chat_id, limit=None, **kwargs):
        message_id = getattr(message, "id", None)
        if not isinstance(message_id, int):
            continue
        # Guard against messages arriving while the sweep is running.
        if isinstance(command_id, int) and message_id > command_id:
            continue
        text = (
            getattr(message, "raw_text", None) or getattr(message, "text", "") or ""
        )
        if not text_matches(pattern, exact, text):
            continue
        batch.append(message_id)
        if len(batch) >= DELETE_BATCH:
            deleted += await _delete_batch(ctx, batch)
            batch.clear()
    if batch:
        deleted += await _delete_batch(ctx, batch)
    return deleted


async def _delete_batch(ctx: Context, batch: list[int]) -> int:
    """Delete one batch, waiting out a flood wait once before giving up."""
    try:
        await ctx.client.delete_messages(ctx.chat_id, batch)
        return len(batch)
    except FloodWaitError as exc:
        wait = min(exc.seconds + 1, 300)
        logger.warning("Flood wait during filter history deletion: sleeping %ss", wait)
        await asyncio.sleep(wait)
    except Exception as exc:
        logger.warning("Could not delete batch in chat %s: %s", ctx.chat_id, exc)
        return 0
    try:
        await ctx.client.delete_messages(ctx.chat_id, batch)
        return len(batch)
    except Exception as exc:
        logger.warning("Could not delete batch in chat %s after flood wait: %s", ctx.chat_id, exc)
        return 0
