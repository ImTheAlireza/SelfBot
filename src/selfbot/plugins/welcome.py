"""Per-chat welcome messages for arriving members.

`selfwlc` stores one welcome template per chat and greets every user who
joins (or is added to) that chat while the feature is switched on there.

Templates support placeholder tags that are filled in from the arriving
user's profile:

* ``[name]`` — profile name (first + last)
* ``[nametag]`` — profile name as a clickable mention
* ``[username]`` — ``@username`` (empty when the user has none)
* ``[[username]/[nametag]]`` — ``@username`` when set, otherwise a mention

Persian (and any other Unicode) text works throughout.
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import CommandError, UsageError
from ..registry import Context, command
from ..utils.text import truncate

logger = logging.getLogger(__name__)

CATEGORY = "Automation"

#: The combined tag must be replaced before its two component tags.
COMBINED_TAG = "[[username]/[nametag]]"

_USAGE = (
    "Usage:\n"
    "• `selfwlc set` — reply to a message to save it as this chat's welcome\n"
    "• `selfwlc on` / `selfwlc off` — toggle welcoming in this chat\n"
    "• `selfwlc off -all` — switch welcoming off in every chat\n"
    "• `selfwlc list` — show every chat with a saved welcome\n"
    "• `selfwlc clear` — delete this chat's welcome message\n"
    "• `selfwlc clear -all` — delete welcome messages everywhere\n\n"
    "Tags: `[name]`, `[nametag]`, `[username]`, `[[username]/[nametag]]`"
)


def _display_name(user: Any) -> str:
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    name = f"{first} {last}".strip()
    return name or "User"


def render_welcome(template: str, user: Any) -> str:
    """Fill a welcome template with one user's profile details.

    Supports both Markdown and HTML templates (including <tg-emoji> and <a>).
    """
    name = _display_name(user)
    user_id = getattr(user, "id", None)
    username = getattr(user, "username", None)

    is_html = "<tg-emoji" in template or "<a " in template or "<b>" in template or "<i>" in template
    if is_html:
        nametag = f'<a href="tg://user?id={user_id}">{name}</a>' if user_id else name
    else:
        nametag = f"[{name}](tg://user?id={user_id})" if user_id else name

    username_text = f"@{username}" if username else ""

    text = template
    # Longest tag first, so `[[username]/[nametag]]` is not shredded by the
    # `[username]` / `[nametag]` replacements below.
    text = text.replace(COMBINED_TAG, username_text or nametag)
    text = text.replace("[nametag]", nametag)
    text = text.replace("[username]", username_text)
    text = text.replace("[name]", name)
    return text


async def _chat_label(ctx: Context, chat_id: int) -> str:
    """Best-effort human-readable name for a chat id."""
    try:
        entity = await ctx.client.get_entity(chat_id)
    except Exception:
        return f"`{chat_id}`"
    title = getattr(entity, "title", None) or _display_name(entity)
    return f"{title} (`{chat_id}`)"


@command(
    "selfwlc",
    category=CATEGORY,
    sudo_only=True,
    min_args=1,
    usage="selfwlc <set|on|off|list|clear> [-all]",
    examples=(
        "selfwlc set",
        "selfwlc on",
        "selfwlc off",
        "selfwlc off -all",
        "selfwlc list",
        "selfwlc clear",
        "selfwlc clear -all",
    ),
)
async def cmd_selfwlc(ctx: Context) -> None:
    """Welcome new members with a saved per-chat message.

    Save a template by replying to any message with `selfwlc set`, then turn
    it on with `selfwlc on`. The welcome only fires in chats where it was
    switched on. Tags: `[name]`, `[nametag]`, `[username]`,
    `[[username]/[nametag]]`.
    """
    ctx.require_single_dash_flags("-all")
    action = ctx.args[0].lower()
    flag = (ctx.arg(1) or "").lower()

    if action == "set":
        await _wlc_set(ctx)
    elif action == "on":
        await _wlc_on(ctx)
    elif action == "off":
        await _wlc_off(ctx, everywhere=flag == "-all")
    elif action == "list":
        await _wlc_list(ctx)
    elif action == "clear":
        await _wlc_clear(ctx, everywhere=flag == "-all")
    else:
        raise UsageError(_USAGE)


async def _wlc_set(ctx: Context) -> None:
    if not ctx.event.is_reply:
        raise UsageError(
            "Reply to a message to save it as this chat's welcome.\n"
            "Example message: `hello [name]!`"
        )

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
        raise CommandError("That message has no text to save.")
    if len(message) > 4000:
        raise CommandError("Welcome message is too long (max 4000 characters).")

    existing = await ctx.db.get_welcome(ctx.chat_id)
    await ctx.db.set_welcome_message(ctx.chat_id, message)

    status = "enabled" if existing and existing.enabled else "disabled"
    hint = "" if (existing and existing.enabled) else "\nSend `selfwlc on` to activate it."
    await ctx.reply(
        "✅ Welcome message saved for **this chat**.\n"
        f"Preview: {truncate(message, 200)}\n"
        f"Status: `{status}`{hint}"
    )


async def _wlc_on(ctx: Context) -> None:
    welcome = await ctx.db.get_welcome(ctx.chat_id)
    if welcome is None:
        raise CommandError(
            "No welcome message is saved for this chat.\n"
            "Reply to a message with `selfwlc set` first."
        )
    if welcome.enabled:
        await ctx.reply("ℹ️ Welcome is already **on** in this chat.")
        return
    await ctx.db.set_welcome_enabled(ctx.chat_id, True)
    await ctx.reply("✅ Welcome is now **on** for this chat.")


async def _wlc_off(ctx: Context, *, everywhere: bool) -> None:
    if everywhere:
        count = await ctx.db.disable_all_welcomes()
        if not count:
            await ctx.reply("ℹ️ Welcome was not on in any chat.")
            return
        await ctx.reply(f"✅ Welcome switched **off** in **{count}** chat{'s' if count != 1 else ''}.")
        return

    welcome = await ctx.db.get_welcome(ctx.chat_id)
    if welcome is None or not welcome.enabled:
        await ctx.reply("ℹ️ Welcome is not on in this chat.")
        return
    await ctx.db.set_welcome_enabled(ctx.chat_id, False)
    await ctx.reply("✅ Welcome is now **off** for this chat. The message stays saved.")


async def _wlc_list(ctx: Context) -> None:
    welcomes = await ctx.db.list_welcomes()
    if not welcomes:
        await ctx.reply("ℹ️ No welcome messages saved in any chat.")
        return

    lines = [f"👋 **Welcome messages** ({len(welcomes)})\n"]
    for wlc in welcomes:
        status = "🟢 on" if wlc.enabled else "⚪ off"
        marker = " ← this chat" if wlc.chat_id == ctx.chat_id else ""
        label = await _chat_label(ctx, wlc.chat_id)
        lines.append(f"• {label} — {status}{marker}\n  {truncate(wlc.message, 80)}")
    await ctx.reply("\n".join(lines))


async def _wlc_clear(ctx: Context, *, everywhere: bool) -> None:
    if everywhere:
        count = await ctx.db.delete_all_welcomes()
        if not count:
            await ctx.reply("ℹ️ No welcome messages exist anywhere.")
            return
        await ctx.reply(
            f"✅ Removed welcome message{'s' if count != 1 else ''} from **{count}** "
            f"chat{'s' if count != 1 else ''}."
        )
        return

    if await ctx.db.delete_welcome(ctx.chat_id):
        await ctx.reply("✅ Welcome message removed for this chat.")
    else:
        await ctx.reply("ℹ️ No welcome message saved for this chat.")
