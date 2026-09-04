"""Auto-delete filters and reply-scoped history deletion.

Reply to a message and send ``filter`` to save its text as a delete filter for
THIS chat only: from then on, every new message from anyone whose text matches
the saved pattern is deleted automatically. ``filter -after`` / ``filter
-before`` are one-shot wipes of the replied message plus everything after or
before it in the same chat.

Filters are always scoped to the chat they were created in (``-here`` is
accepted as an explicit reminder of that and does nothing). Matching is
case-insensitive and defaults to "contains"; ``-exact`` stores a filter that
only fires on whole-message matches.
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

USAGE = "`filter [-after | -before | -list | -clear | -off <text>] [-exact]`"


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


def matches(filter_rule: object, text: str) -> bool:
    """Case-insensitive contains/exact match against a saved pattern."""
    pattern = filter_rule.pattern.strip().casefold()  # type: ignore[attr-defined]
    candidate = text.strip().casefold()
    if not pattern:
        return False
    if filter_rule.exact:  # type: ignore[attr-defined]
        return candidate == pattern
    return pattern in candidate


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
    usage="filter [-after | -before | -list | -clear | -off <text>] [-exact]",
    examples=(
        "filter",
        "filter -exact",
        "filter -after",
        "filter -before",
        "filter -list",
        "filter -off <text>",
        "filter -clear",
    ),
)
async def cmd_filter(ctx: Context) -> None:
    """Save a per-chat auto-delete filter from the replied message.

    Reply to a message and send `filter`: every NEW message in this chat from
    anyone whose text matches it is deleted automatically. Filters only ever
    apply to the chat where they were created. `-after`/`-before` instead
    delete the replied message plus everything after/before it, once.
    """
    ctx.require_single_dash_flags(*FLAGS)
    flags, off_words, stray = _parse_args(ctx.args)

    actions = flags & {"-after", "-before", "-list", "-clear", "-off"}
    if len(actions) > 1:
        raise UsageError(f"Pick one action.\nUsage: {USAGE}")
    if actions and stray:
        raise UsageError(f"Unexpected argument `{stray[0]}`.\nUsage: {USAGE}")
    if flags & {"-after", "-before", "-list", "-clear"} and "-exact" in flags:
        raise UsageError("`-exact` only applies to saving a filter.\nUsage: `filter [-exact]`")

    if "-list" in flags:
        await _list_filters(ctx)
    elif "-clear" in flags:
        await _clear_filters(ctx)
    elif "-after" in flags:
        await _delete_history(ctx, after=True)
    elif "-before" in flags:
        await _delete_history(ctx, after=False)
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
    replied = await ctx.get_reply_message()
    pattern = (
        getattr(replied, "raw_text", None) or getattr(replied, "text", "") or ""
    ).strip() if replied else ""
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
            replied = await ctx.get_reply_message()
            pattern = (
                getattr(replied, "raw_text", None) or getattr(replied, "text", "") or ""
            ).strip() if replied else ""
        if not pattern:
            raise UsageError("Usage: `filter -off <text>` (or reply to a filtered message)")
    removed = await ctx.db.remove_delete_filter(ctx.chat_id, pattern)
    if not removed:
        await ctx.reply(f"ℹ️ No saved filter `{truncate(pattern, 80)}` in this chat.")
        return
    ctx.bot.invalidate_filter_cache(ctx.chat_id)
    await ctx.reply(f"✅ Removed filter `{truncate(pattern, 80)}`.")


# -- one-shot history deletion (-after / -before) -----------------------------


async def _delete_history(ctx: Context, *, after: bool) -> None:
    if not ctx.event.is_reply:
        direction = "-after" if after else "-before"
        raise UsageError(
            f"Reply to the message that anchors the deletion.\nUsage: `filter {direction}`"
        )
    replied = await ctx.get_reply_message()
    target_id = getattr(replied, "id", None)
    if not isinstance(target_id, int):
        raise ValidationError("Could not determine the replied message.")

    direction = "after" if after else "before"
    if not await ctx.bot.confirm(
        ctx.event,
        (
            f"⚠️ Delete the replied message and every message **{direction}** it "
            "in this chat? This cannot be undone."
        ),
    ):
        await ctx.reply("👍 Cancelled.")
        return

    deleted = await _delete_history_range(ctx, target_id, after=after)
    if deleted == 0:
        await ctx.reply("ℹ️ Nothing was deleted (no permission, or nothing to delete).")
        return
    span = "from it onward" if after else "up to and including it"
    await ctx.respond(f"🗑 Deleted **{deleted}** message(s) {span}.")


async def _delete_history_range(ctx: Context, target_id: int, *, after: bool) -> int:
    """Delete an open-ended ID range in batches; returns how many were removed."""
    deleted = 0
    batch: list[int] = []
    kwargs: dict[str, int | None] = (
        {"min_id": target_id - 1, "max_id": None}
        if after
        else {"min_id": 0, "max_id": target_id + 1}
    )
    async for message in ctx.client.iter_messages(ctx.chat_id, limit=None, **kwargs):
        message_id = getattr(message, "id", None)
        if not isinstance(message_id, int):
            continue
        # Explicit boundary guards protect against permissive test doubles and
        # messages arriving while the deletion sweep is running.
        if after and message_id < target_id:
            continue
        if not after and message_id > target_id:
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
