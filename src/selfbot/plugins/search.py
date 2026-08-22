"""Search the current chat — or the whole account with --global."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..errors import UsageError, ValidationError
from ..registry import Context, command
from ..utils.text import truncate

logger = logging.getLogger(__name__)

CATEGORY = "Messaging"

#: Maps the user-facing type name to the Telethon message attribute.
MEDIA_TYPES: dict[str, str] = {
    "photo": "photo",
    "photos": "photo",
    "video": "video",
    "videos": "video",
    "voice": "voice",
    "voices": "voice",
    "videomsg": "video_note",
    "videomsgs": "video_note",
    "music": "audio",
    "musics": "audio",
    "audio": "audio",
    "file": "document",
    "files": "document",
    "sticker": "sticker",
    "stickers": "sticker",
    "gif": "gif",
    "gifs": "gif",
    "link": "web_preview",
    "links": "web_preview",
}

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y")


def _parse_date(value: str, *, end: bool = False) -> datetime:
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
            if end:
                dt = dt + timedelta(days=1) - timedelta(microseconds=1)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValidationError(
        f"Could not read date `{value}`. Use YYYY-MM-DD (e.g. 2026-08-22)."
    )


@command(
    "search",
    category=CATEGORY,
    usage=(
        "search <text> [--from <id|@username|me>] [--since YYYY-MM-DD] "
        "[--until YYYY-MM-DD] [--type <media>] [--limit N] [--global]"
    ),
    examples=(
        "search invoice",
        "search --from @durov --since 2026-01-01",
        "search contract --global --limit 30",
        "search --type photos --global",
    ),
)
async def cmd_search(ctx: Context) -> None:
    """Search chat history by text, sender, date or media type.

    Without ``--global`` only the current chat is searched. ``--global``
    searches every private chat, group and channel in the account.
    """
    tokens, flags = _parse_args(ctx.args)

    text = " ".join(tokens).strip()
    sender = flags.get("from")
    since = flags.get("since")
    until = flags.get("until")
    media = flags.get("type")
    global_search = "--global" in ctx.args
    try:
        limit = min(int(flags.get("limit", "20")), 100)
    except ValueError as exc:
        raise ValidationError("`--limit` must be a number.") from exc
    if limit < 1:
        raise ValidationError("`--limit` must be at least 1.")

    if global_search and not (text or media or sender):
        raise UsageError(
            "`--global` needs a search term, `--type media`, or `--from <who>`."
        )

    if not (text or sender or since or until or media):
        raise UsageError(
            "Tell me what to look for.\n"
            "Usage: `search <text> [--from X] [--since D] [--until D] "
            "[--type media] [--limit N] [--global]`"
        )

    if media and media not in MEDIA_TYPES:
        supported = ", ".join(sorted(set(MEDIA_TYPES)))
        raise ValidationError(f"Unknown media type `{media}`. Supported: {supported}")

    from_user: Any = None
    if sender:
        sender = sender.lstrip("@")
        if sender.lower() == "me":
            from_user = "me"
        elif sender.lstrip("-").isdigit():
            from_user = int(sender)
        else:
            from_user = sender

    since_dt = _parse_date(since) if since else None
    until_dt = _parse_date(until, end=True) if until else None

    status = await ctx.reply(
        "🌍 Searching across all chats…" if global_search else "🔍 Searching…"
    )

    attribute = MEDIA_TYPES.get(media) if media else None

    found: Any
    try:
        if global_search:
            found = await _search_global(
                ctx,
                text=text,
                from_user=from_user,
                since_dt=since_dt,
                until_dt=until_dt,
                attribute=attribute,
                limit=limit,
            )
        else:
            found = await _search_local(
                ctx,
                text=text,
                from_user=from_user,
                since_dt=since_dt,
                until_dt=until_dt,
                attribute=attribute,
                limit=limit,
            )
    except Exception as exc:
        await _delete_status(status)
        await ctx.reply(f"❌ Search failed: `{type(exc).__name__}: {exc}`")
        return

    if not found:
        await _delete_status(status)
        await ctx.reply("ℹ️ No messages matched.")
        return

    if global_search:
        lines = [f"🌍 **{len(found)} global result(s)**\n"]
        for message, chat_title, chat_id in found[:limit]:
            snippet = _snippet(message)
            sender_name = await _sender_name(message)
            when = message.date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
            link = _message_link(chat_id, message.id)
            lines.append(
                f"• **{chat_title}** · {sender_name} · `{when}` · [go]({link})\n  {snippet}"
            )
    else:
        lines = [f"🔍 **{len(found)} result(s)**\n"]
        for message in found[:limit]:
            snippet = _snippet(message)
            sender_name = await _sender_name(message)
            when = message.date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
            link = _message_link(ctx.chat_id, message.id)
            lines.append(
                f"• **{sender_name}** · `{when}` · [go]({link})\n  {snippet}"
            )
    await status.delete()
    await ctx.reply("\n".join(lines))


async def _delete_status(message: Any) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete the search status message", exc_info=True)


async def _search_local(
    ctx: Context,
    *,
    text: str,
    from_user: Any,
    since_dt: datetime | None,
    until_dt: datetime | None,
    attribute: str | None,
    limit: int,
) -> list[Any]:
    found: list[Any] = []
    async for message in ctx.client.iter_messages(
        ctx.chat_id,
        search=text or None,
        from_user=from_user,
        offset_date=until_dt,
        limit=1000,
    ):
        if since_dt and message.date < since_dt:
            continue
        if attribute and not getattr(message, attribute, None):
            continue
        found.append(message)
        if len(found) >= limit:
            break
    return found


async def _search_global(
    ctx: Context,
    *,
    text: str,
    from_user: Any,
    since_dt: datetime | None,
    until_dt: datetime | None,
    attribute: str | None,
    limit: int,
) -> list[tuple[Any, str, int]]:
    """Search every dialog in the account.

    Telegram's global message search (entity=None) accepts a search string and
    a from_user filter but not date/media filters, so those are applied
    client-side. To support --type without text we iterate each dialog.
    """
    found: list[tuple[Any, str, int]] = []
    title_cache: dict[int, str] = {}

    def _matches(message: Any) -> bool:
        if since_dt and message.date < since_dt:
            return False
        if until_dt and message.date > until_dt:
            return False
        return not (attribute and not getattr(message, attribute, None))

    if text:
        async for message in ctx.client.iter_messages(
            None,
            search=text,
            from_user=from_user,
            limit=max(limit * 5, 100),
        ):
            if not _matches(message):
                continue
            chat_id = _chat_id_of(message)
            title = await _chat_title(ctx, chat_id, title_cache)
            found.append((message, title, chat_id))
            if len(found) >= limit:
                break
    else:
        scanned = 0
        async for dialog in ctx.client.iter_dialogs(limit=200):
            async for message in ctx.client.iter_messages(
                dialog.id,
                from_user=from_user,
                offset_date=until_dt,
                limit=500,
            ):
                if not _matches(message):
                    continue
                chat_id = _chat_id_of(message)
                title = dialog.name or str(chat_id)
                title_cache[chat_id] = title
                found.append((message, title, chat_id))
                if len(found) >= limit:
                    break
                scanned += 1
                if scanned > 2000:
                    break
            if len(found) >= limit or scanned > 2000:
                break

    return found


def _chat_id_of(message: Any) -> int:
    """Resolve the originating chat id across peer types."""
    peer_id = message.peer_id
    if hasattr(peer_id, "channel_id"):
        return int(f"-100{peer_id.channel_id}")
    if hasattr(peer_id, "chat_id"):
        return int(f"-{peer_id.chat_id}")
    if hasattr(peer_id, "user_id"):
        return int(peer_id.user_id)
    return 0


async def _chat_title(
    ctx: Context, chat_id: int, cache: dict[int, str]
) -> str:
    if chat_id in cache:
        return cache[chat_id]
    title = str(chat_id)
    try:
        entity = await ctx.client.get_entity(chat_id)
        title = (
            getattr(entity, "title", None)
            or " ".join(
                filter(
                    None,
                    [
                        getattr(entity, "first_name", "") or "",
                        getattr(entity, "last_name", "") or "",
                    ],
                )
            ).strip()
            or getattr(entity, "username", None)
            or str(chat_id)
        )
    except Exception:
        logger.debug("Could not resolve chat title for %s", chat_id)
    cache[chat_id] = title
    return title


def _parse_args(args: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split positional tokens from ``--flag value`` pairs.

    Boolean flags (``--global``) are stripped so they never become part of the
    search text.
    """
    tokens: list[str] = []
    flags: dict[str, str] = {}
    known = {"--from", "--since", "--until", "--type", "--limit"}
    booleans = {"--global"}
    index = 0
    while index < len(args):
        token = args[index]
        if token in known:
            if index + 1 >= len(args):
                raise UsageError(f"`{token}` needs a value.")
            flags[token[2:]] = args[index + 1]
            index += 2
        elif token in booleans:
            index += 1
        else:
            tokens.append(token)
            index += 1
    return tokens, flags


def _snippet(message: Any) -> str:
    if message.media and not (getattr(message, "raw_text", "") or "").strip():
        media = _describe_media(message)
        caption = (getattr(message, "raw_text", "") or "").strip()
        return truncate(f"🖼 {media}" + (f" — {caption}" if caption else ""), 120)
    text = (getattr(message, "raw_text", "") or "").strip()
    if text:
        return truncate(text, 120)
    return "_(no text)_"


def _describe_media(message: Any) -> str:
    for label, attr in (
        ("photo", "photo"),
        ("video", "video"),
        ("voice", "voice"),
        ("video note", "video_note"),
        ("audio", "audio"),
        ("sticker", "sticker"),
        ("GIF", "gif"),
        ("file", "document"),
    ):
        if getattr(message, attr, None):
            return label
    return "media"


async def _sender_name(message: Any) -> str:
    try:
        sender = await message.get_sender()
    except Exception:
        sender = None
    if sender is None:
        return str(getattr(message, "sender_id", "?"))
    name = " ".join(
        filter(
            None,
            [getattr(sender, "first_name", "") or "", getattr(sender, "last_name", "") or ""],
        )
    ).strip()
    username = getattr(sender, "username", None)
    if name:
        return name
    if username:
        return f"@{username}"
    return str(getattr(sender, "id", "?"))


def _message_link(chat_id: int, message_id: int) -> str:
    """Best-effort deep link to a message.

    For channels/supergroups Telegram uses the ``t.me/c/<id>/<msg>`` form with
    the internal id stripped of the ``-100`` prefix. For private chats we fall
    back to a link the official clients resolve against the open chat.
    """
    text_id = str(chat_id)
    if text_id.startswith("-100"):
        internal = text_id[4:]
        if internal.isdigit():
            return f"https://t.me/c/{internal}/{message_id}"
    return f"tg://openmessage?chat_id={chat_id}&message_id={message_id}"
