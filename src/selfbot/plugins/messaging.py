"""Messaging tools: spam, message deletion, user info and quick replies."""

from __future__ import annotations

import asyncio
import logging
import time
from io import BytesIO

from telethon.errors import ChatWriteForbiddenError, FloodWaitError
from telethon.tl.functions.users import GetFullUserRequest

from ..errors import UsageError, ValidationError
from ..registry import Context, command
from ..utils.text import truncate

logger = logging.getLogger(__name__)

CATEGORY = "Messaging"

#: Message predicates for `del`. Keys are what the user types.
DELETE_TYPES: dict[str, str] = {
    "photos": "photo",
    "videos": "video",
    "voices": "voice",
    "videomsgs": "video_note",
    "musics": "audio",
    "files": "document",
    "stickers": "sticker",
    "gifs": "gif",
    "links": "web_preview",
    "all": "all",
}


@command(
    "spam",
    category=CATEGORY,
    min_args=2,
    usage="spam <message> <count>",
    examples=("spam hello 5",),
)
async def cmd_spam(ctx: Context) -> None:
    """Send a message repeatedly, respecting rate limits."""
    bot = ctx.bot
    limits = ctx.config.spam

    try:
        count = int(ctx.args[-1])
    except ValueError:
        raise ValidationError("The last argument must be a number.") from None

    message = " ".join(ctx.args[:-1]).strip()
    if not message:
        raise ValidationError("Nothing to send.")
    if count < 1:
        raise ValidationError("Count must be at least 1.")
    if count > limits.limit:
        raise ValidationError(f"Limit is {limits.limit} messages per run.")

    user_id = ctx.sender_id
    if user_id in bot.spam_tasks and not bot.spam_tasks[user_id].done():
        raise ValidationError("A spam run is already active. Use `cancel` first.")

    last = bot.spam_cooldowns.get(user_id, 0.0)
    remaining = limits.cooldown - (time.monotonic() - last)
    if remaining > 0:
        raise ValidationError(f"Cooling down — try again in {remaining:.1f}s.")

    bot.spam_cooldowns[user_id] = time.monotonic()
    task = asyncio.ensure_future(_spam_worker(ctx, message, count))
    bot.spam_tasks[user_id] = task

    try:
        sent = await task
    except asyncio.CancelledError:
        await ctx.respond("🛑 Spam cancelled.")
        return
    finally:
        bot.spam_tasks.pop(user_id, None)

    await ctx.respond(f"✅ Sent {sent}/{count} messages.")


async def _spam_worker(ctx: Context, message: str, count: int) -> int:
    """Send messages with backoff. Returns how many actually went out."""
    delay = ctx.config.spam.delay
    sent = 0
    for index in range(count):
        try:
            await ctx.event.respond(message)
            sent += 1
        except FloodWaitError as exc:
            wait = min(exc.seconds + 1, 300)
            logger.warning("Flood wait during spam: sleeping %ss", wait)
            await asyncio.sleep(wait)
            continue
        except ChatWriteForbiddenError:
            logger.warning("Cannot write in chat %s; aborting spam", ctx.chat_id)
            break
        if index < count - 1 and delay > 0:
            await asyncio.sleep(delay)
    return sent


@command("cancel", category=CATEGORY, usage="cancel")
async def cmd_cancel(ctx: Context) -> None:
    """Stop your running spam task."""
    task = ctx.bot.spam_tasks.get(ctx.sender_id)
    if task is None or task.done():
        await ctx.reply("ℹ️ Nothing to cancel.")
        return
    task.cancel()
    await ctx.reply("🛑 Cancelling…")


@command(
    "del",
    category=CATEGORY,
    sudo_only=True,
    min_args=1,
    max_args=2,
    usage="del <count|type> [-me]",
    examples=("del 10", "del 400 -me", "del photos -me", "del all"),
)
async def cmd_del(ctx: Context) -> None:
    """Delete messages in this chat; append `-me` to target only yours.

    The chat is always taken from the command event. No argument can select or
    affect another chat.
    """
    lowered = [arg.lower() for arg in ctx.args]
    if "-me" in lowered[:-1] or lowered.count("-me") > 1:
        raise UsageError("`-me` must be the final argument: `del <count|type> -me`")

    own_only = bool(lowered and lowered[-1] == "-me")
    args = ctx.args[:-1] if own_only else ctx.args
    if len(args) != 1:
        raise UsageError("Usage: `del <count|type> [-me]`")

    target = args[0].lower()
    limit: int | None
    kind: str

    if target.isdigit():
        count = int(target)
        if count < 1:
            raise ValidationError("Count must be at least 1.")
        if count > 1000:
            raise ValidationError("Refusing to delete more than 1000 messages at once.")

        who = "of your messages" if own_only else "messages from everyone"
        if (not own_only or count > 10) and not await ctx.bot.confirm(
            ctx.event,
            f"⚠️ Delete up to {count} {who} in this chat? This cannot be undone.",
        ):
            await ctx.reply("👍 Cancelled.")
            return
        limit = count
        kind = "all"

    elif target in DELETE_TYPES:
        kind = DELETE_TYPES[target]
        scope = "all messages" if kind == "all" else f"all `{target}`"
        who = "of yours" if own_only else "from everyone"
        if not await ctx.bot.confirm(
            ctx.event,
            f"⚠️ Delete {scope} {who} in this chat? This cannot be undone.",
        ):
            await ctx.reply("👍 Cancelled.")
            return
        # A type or `all` means the complete history of this chat, not an
        # arbitrary global scan and not a fixed number of recent messages.
        limit = None

    else:
        supported = ", ".join(f"`{key}`" for key in DELETE_TYPES)
        raise ValidationError(f"Unknown type `{target}`.\nSupported: {supported}")

    matched, deleted = await _delete_matching_messages(
        ctx,
        limit=limit,
        kind=kind,
        own_only=own_only,
    )
    if not matched:
        await ctx.reply("ℹ️ Nothing matched in this chat.")
        return

    await ctx.respond(f"🗑 Deleted **{deleted}** message(s) in this chat.")


@command(
    "delto",
    category=CATEGORY,
    sudo_only=True,
    requires_reply=True,
    max_args=1,
    usage="delto [-me]",
    examples=("delto", "delto -me"),
)
async def cmd_delto(ctx: Context) -> None:
    """Delete from this command back through the replied message, inclusive.

    No confirmation is requested. With ``-me``, only messages sent by this
    account are deleted; the replied message still defines the lower boundary.
    """
    if ctx.args and (len(ctx.args) != 1 or ctx.args[0].lower() != "-me"):
        raise UsageError("Usage: `delto [-me]`")
    own_only = bool(ctx.args)

    replied = await ctx.get_reply_message()
    target_id = getattr(replied, "id", None)
    command_id = getattr(ctx.event, "id", None)
    if not isinstance(target_id, int) or not isinstance(command_id, int):
        raise ValidationError("Could not determine the message range.")
    if target_id > command_id:
        raise ValidationError("The replied message must be older than this command.")

    matched, deleted = await _delete_message_range(
        ctx,
        target_id=target_id,
        command_id=command_id,
        own_only=own_only,
    )
    if deleted < matched:
        logger.warning(
            "delto deleted %d/%d messages in chat %s",
            deleted,
            matched,
            ctx.chat_id,
        )


async def _delete_message_range(
    ctx: Context,
    *,
    target_id: int,
    command_id: int,
    own_only: bool,
) -> tuple[int, int]:
    """Delete an inclusive ID range in batches, newest to oldest."""
    matched = 0
    deleted = 0
    batch: list[int] = []
    from_user = "me" if own_only else None

    async for message in ctx.client.iter_messages(
        ctx.chat_id,
        limit=None,
        from_user=from_user,
        min_id=max(0, target_id - 1),
        max_id=command_id + 1,
    ):
        message_id = getattr(message, "id", None)
        if not isinstance(message_id, int):
            continue
        # Keep explicit guards even though Telegram's min/max filters are
        # exclusive; they protect against permissive test doubles and API edge
        # cases while messages are arriving during deletion.
        if message_id > command_id:
            continue
        if message_id < target_id:
            break
        matched += 1
        batch.append(message_id)
        if len(batch) == 100:
            deleted += await _delete_batch(ctx, batch)
            batch = []

    if batch:
        deleted += await _delete_batch(ctx, batch)
    return matched, deleted


async def _delete_matching_messages(
    ctx: Context,
    *,
    limit: int | None,
    kind: str,
    own_only: bool,
) -> tuple[int, int]:
    """Scan only the current chat and delete matching messages in batches."""
    matched = 0
    deleted = 0
    batch: list[int] = []
    from_user = "me" if own_only else None

    async for message in ctx.client.iter_messages(
        ctx.chat_id,
        limit=limit,
        from_user=from_user,
    ):
        if kind != "all" and not getattr(message, kind, None):
            continue
        matched += 1
        batch.append(message.id)
        if len(batch) == 100:  # Telegram caps one delete request at 100 IDs.
            deleted += await _delete_batch(ctx, batch)
            batch = []

    if batch:
        deleted += await _delete_batch(ctx, batch)
    return matched, deleted


async def _delete_batch(ctx: Context, message_ids: list[int]) -> int:
    """Delete one current-chat batch, retrying a single Telegram flood wait."""
    for attempt in range(2):
        try:
            await ctx.client.delete_messages(ctx.chat_id, message_ids)
            return len(message_ids)
        except FloodWaitError as exc:
            if attempt:
                logger.warning(
                    "Delete batch still rate-limited in chat %s; skipping",
                    ctx.chat_id,
                )
                return 0
            wait = min(exc.seconds + 1, 60)
            logger.warning("Delete rate-limited; retrying in %ss", wait)
            await asyncio.sleep(wait)
        except Exception as exc:
            logger.warning("Delete batch failed in chat %s: %s", ctx.chat_id, exc)
            return 0
    return 0


@command(
    "info",
    category="Info",
    usage="info [user_id|@username]",
    examples=("info", "info @durov"),
)
async def cmd_info(ctx: Context) -> None:
    """Show details about a user (reply, mention, or yourself)."""
    status = await ctx.reply("⏳ Fetching…")

    try:
        if ctx.event.is_reply:
            replied = await ctx.get_reply_message()
            target = replied.sender_id
        elif ctx.args:
            raw = ctx.args[0]
            target = int(raw) if raw.lstrip("-").isdigit() else raw
        else:
            target = "me"

        entity = await ctx.client.get_entity(target)
    except Exception as exc:
        await ctx.bot.edit(status, f"❌ Could not find that user: `{exc}`")
        return

    bio = ""
    try:
        full = await ctx.client(GetFullUserRequest(entity.id))
        bio = getattr(full.full_user, "about", "") or ""
    except Exception:
        pass

    name = " ".join(
        filter(None, [getattr(entity, "first_name", ""), getattr(entity, "last_name", "")])
    ) or "—"
    username = getattr(entity, "username", None)
    premium = getattr(entity, "premium", False)

    caption = (
        f"👤 **{name}**\n\n"
        f"ID: `{entity.id}`\n"
        f"Username: {'@' + username if username else '—'}\n"
        f"Premium: {'💎 yes' if premium else 'no'}\n"
        f"Bot: {'yes' if getattr(entity, 'bot', False) else 'no'}\n"
        f"Bio: {truncate(bio, 200) if bio else '—'}"
    )

    photo = None
    try:
        if getattr(entity, "photo", None):
            photo = await ctx.client.download_profile_photo(
                entity, file=bytes, download_big=True
            )
    except Exception:
        logger.debug("Profile photo download failed", exc_info=True)

    await status.delete()
    if photo:
        # Raw bytes have no filename, so Telethon cannot infer an image MIME
        # type and uploads them as an unnamed document. A named in-memory JPEG
        # makes the profile picture render as a Telegram photo instead.
        photo_file = BytesIO(photo)
        photo_file.name = f"profile_{entity.id}.jpg"  # type: ignore[attr-defined]
        try:
            await ctx.client.send_file(
                ctx.chat_id,
                photo_file,
                caption=caption,
                force_document=False,
            )
        finally:
            photo_file.close()
    else:
        await ctx.reply(caption)


# ---------------------------------------------------------------------------
# Quick replies
# ---------------------------------------------------------------------------


@command(
    "qreply",
    category=CATEGORY,
    usage="qreply <set|remove|list|info> [alias] [message]",
    examples=("qreply set email me@example.com", "qreply list", "qreply remove email"),
)
async def cmd_qreply(ctx: Context) -> None:
    """Manage text shortcuts you trigger with `-alias`."""
    prefix = ctx.config.quick_reply_prefix

    if not ctx.args:
        await ctx.reply(
            "📝 **Quick replies**\n\n"
            "`qreply set <alias> <message>` — create or update\n"
            "`qreply set <alias>` — use the replied-to message\n"
            "`qreply remove <alias>` — delete\n"
            "`qreply list` — show all\n"
            "`qreply info <alias>` — full text\n\n"
            f"Type `{prefix}alias` in any chat to expand it."
        )
        return

    action = ctx.args[0].lower()

    if action == "set":
        await _qreply_set(ctx)
    elif action in {"remove", "rm", "del", "delete"}:
        await _qreply_remove(ctx)
    elif action in {"list", "ls"}:
        await _qreply_list(ctx)
    elif action == "info":
        await _qreply_info(ctx)
    else:
        raise ValidationError(f"Unknown action `{action}`. Try `qreply` for help.")


def _validate_alias(alias: str) -> str:
    alias = alias.lower()
    if not alias.isalnum():
        raise ValidationError("Alias must be letters and numbers only.")
    if len(alias) > 50:
        raise ValidationError("Alias must be 50 characters or fewer.")
    return alias


async def _qreply_set(ctx: Context) -> None:
    if len(ctx.args) < 2:
        raise UsageError("Usage: `qreply set <alias> <message>`")

    alias = _validate_alias(ctx.args[1])

    if len(ctx.args) > 2:
        # Slice the raw text so quoting and spacing survive intact.
        marker = ctx.args[1]
        _, _, remainder = ctx.raw_args.partition(marker)
        message = remainder.strip() or " ".join(ctx.args[2:])
    elif ctx.event.is_reply:
        replied = await ctx.get_reply_message()
        from telethon.extensions import html

        if replied and getattr(replied, "entities", None):
            try:
                message = html.unparse(replied.message, replied.entities).strip()
            except Exception:
                message = (getattr(replied, "raw_text", "") or "").strip()
        else:
            message = (getattr(replied, "raw_text", "") or "").strip() if replied else ""
        if not message:
            raise ValidationError("The replied message has no text.")
    else:
        raise UsageError("Provide a message, or reply to one.")

    if len(message) > 4000:
        raise ValidationError("Message is too long (max 4000 characters).")

    await ctx.db.set_quick_reply(ctx.sender_id, alias, message)
    prefix = ctx.config.quick_reply_prefix
    await ctx.reply(
        f"✅ Saved **{prefix}{alias}**\n`{truncate(message, 120)}`\n\n"
        f"Type `{prefix}{alias}` to use it."
    )


async def _qreply_remove(ctx: Context) -> None:
    if len(ctx.args) < 2:
        raise UsageError("Usage: `qreply remove <alias>`")
    alias = ctx.args[1].lower()
    if await ctx.db.delete_quick_reply(ctx.sender_id, alias):
        await ctx.reply(f"✅ Removed `{ctx.config.quick_reply_prefix}{alias}`.")
    else:
        await ctx.reply(f"ℹ️ No quick reply named `{alias}`.")


async def _qreply_list(ctx: Context) -> None:
    replies = await ctx.db.list_quick_replies(ctx.sender_id)
    if not replies:
        await ctx.reply("ℹ️ No quick replies yet. Use `qreply set <alias> <message>`.")
        return
    prefix = ctx.config.quick_reply_prefix
    lines = [f"📝 **Quick replies** ({len(replies)})\n"]
    lines += [f"• `{prefix}{r.alias}` → {truncate(r.message, 60)}" for r in replies]
    await ctx.reply("\n".join(lines))


async def _qreply_info(ctx: Context) -> None:
    if len(ctx.args) < 2:
        raise UsageError("Usage: `qreply info <alias>`")
    alias = ctx.args[1].lower()
    message = await ctx.db.get_quick_reply(ctx.sender_id, alias)
    if message is None:
        raise ValidationError(f"No quick reply named `{alias}`.")
    prefix = ctx.config.quick_reply_prefix
    await ctx.reply(
        f"📝 **{prefix}{alias}**\nLength: {len(message)} characters\n\n{message}"
    )
