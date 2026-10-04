"""Per-chat auto-forwarding (mirroring) rules.

Type ``autoforward @channel -music`` inside a chat and every NEW message that
@channel posts is mirrored into that chat. Any source works: a channel, a
group, a bot or a person — give it as ``@username``, a ``t.me`` link, a
numeric chat id, or ``me`` for your own Saved Messages.

Rules are per destination: each chat keeps its own list, so one source can be
mirrored into several chats with different filters.

Without flags every message is mirrored. With flags the rule is limited to
exactly those kinds — ``autoforward @channel -music -video`` mirrors only
audio and video messages. Media kind flags are the same vocabulary as `del`:
``-text``, ``-photo``, ``-video``, ``-voice``, ``-videomsg``, ``-music``,
``-file``, ``-sticker``, ``-gif``, ``-link``.

By default a real forward is used, so the original source stays visible. Add
``-hide`` to send a copy instead: the message arrives without the "Forwarded
from" header (it also works for chats that forbid forwarding).

Manage the rules in the destination chat:

* ``autoforward list [-all]`` — show this chat's rules (or every chat's)
* ``autoforward off [source]`` — pause one rule, or all rules of this chat
* ``autoforward on [source]`` — resume it
* ``autoforward remove <source>`` — delete it (``remove -all`` for every rule)

``autoforward`` with no arguments prints the rules of the current chat and the
usage. Rules survive restarts; nothing is mirrored retroactively — only
messages that arrive after the rule exists.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from telethon import utils
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError

from ..errors import UsageError, ValidationError
from ..registry import Context, command
from ..utils.text import truncate

logger = logging.getLogger(__name__)

CATEGORY = "Automation"

# ---------------------------------------------------------------------------
# Media kinds
# ---------------------------------------------------------------------------

#: How each kind is spelled when talking to the user.
TYPE_LABELS: dict[str, str] = {
    "text": "text",
    "photo": "photo",
    "video": "video",
    "voice": "voice",
    "video_note": "video message",
    "audio": "music",
    "file": "file",
    "sticker": "sticker",
    "gif": "gif",
    "link": "link preview",
}

#: Accepted flag spellings (without the dash) → canonical kind. The plural
#: forms mirror what `del` and `search -type` already accept.
TYPE_FLAGS: dict[str, str] = {
    "text": "text",
    "texts": "text",
    "photo": "photo",
    "photos": "photo",
    "video": "video",
    "videos": "video",
    "voice": "voice",
    "voices": "voice",
    "videomsg": "video_note",
    "videomsgs": "video_note",
    "videonote": "video_note",
    "videonotes": "video_note",
    "music": "audio",
    "musics": "audio",
    "audio": "audio",
    "audios": "audio",
    "file": "file",
    "files": "file",
    "document": "file",
    "documents": "file",
    "sticker": "sticker",
    "stickers": "sticker",
    "gif": "gif",
    "gifs": "gif",
    "link": "link",
    "links": "link",
}

#: Canonical spellings, in the order help and error messages list them.
_FLAG_ORDER: tuple[str, ...] = (
    "-text",
    "-photo",
    "-video",
    "-voice",
    "-videomsg",
    "-music",
    "-file",
    "-sticker",
    "-gif",
    "-link",
    "-hide",
)

#: Every accepted spelling — the plural forms are aliases of the canonical ones.
_KNOWN_FLAGS: tuple[str, ...] = (*(f"-{name}" for name in TYPE_FLAGS), "-hide")

_HIDE_FLAG = "-hide"
_ALL_FLAG = "-all"

SUBCOMMANDS: tuple[str, ...] = ("list", "on", "off", "remove")
_SUBCOMMAND_ALIASES: dict[str, str] = {
    "rm": "remove",
    "delete": "remove",
    "del": "remove",
    "unset": "remove",
    "enable": "on",
    "disable": "off",
    "ls": "list",
}

#: Deeper chains of rules are treated as a loop and dropped.
MAX_HOPS = 5

#: How long a message id we produced is remembered for the loop guard.
_HOP_TTL = 600.0

#: Sweep the loop-guard map once it grows past this many entries.
_HOP_SWEEP_SIZE = 512

#: Pause before retrying once after a flood wait.
_MAX_FLOOD_SLEEP = 60.0

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_NUMBER_RE = re.compile(r"-?\d+")

USAGE = (
    "`autoforward <source> [-text] [-photo] [-video] [-voice] [-videomsg] "
    "[-music] [-file] [-sticker] [-gif] [-link] [-hide]`\n"
    "`autoforward list [-all]` · `autoforward on|off [source]` · "
    "`autoforward remove <source>`"
)


# ---------------------------------------------------------------------------
# Message inspection
# ---------------------------------------------------------------------------


def classify_message(message: Any) -> str:
    """The canonical kind of one message.

    Checked most-specific first: a music file is both an ``audio`` and a
    ``document`` in Telegram's model, and a voice note is both a ``voice`` and
    a ``document``; the narrower kind wins so ``-music`` and ``-file`` do not
    overlap.
    """
    if getattr(message, "sticker", None) is not None:
        return "sticker"
    if getattr(message, "gif", None) is not None:
        return "gif"
    if getattr(message, "video_note", None) is not None:
        return "video_note"
    if getattr(message, "voice", None) is not None:
        return "voice"
    if getattr(message, "audio", None) is not None:
        return "audio"
    if getattr(message, "video", None) is not None:
        return "video"
    if getattr(message, "photo", None) is not None:
        return "photo"
    if getattr(message, "document", None) is not None:
        return "file"
    if getattr(message, "web_preview", None) is not None:
        return "link"
    return "text"


def matches_types(rule: Any, message: Any) -> bool:
    """True when a rule allows this message's kind (empty = everything)."""
    allowed = tuple(getattr(rule, "media_types", ()) or ())
    if not allowed:
        return True
    return classify_message(message) in allowed


def types_label(types: tuple[str, ...] | list[str]) -> str:
    """Human-readable list of allowed kinds."""
    if not types:
        return "every message"
    labels = [TYPE_LABELS.get(kind, kind) for kind in types]
    return ", ".join(labels)


# ---------------------------------------------------------------------------
# New-message watcher (wired into SelfBot._handle_message)
# ---------------------------------------------------------------------------


async def mirror_rules(bot: Any) -> list[Any]:
    """Enabled rules, cached on the bot until a command changes them."""
    cache = getattr(bot, "_auto_forward_cache", None)
    if cache is not None:
        return cache
    rules = await bot.db.list_enabled_auto_forwards()
    try:
        bot._auto_forward_cache = rules
    except Exception:  # pragma: no cover - cache is best-effort
        logger.debug("Could not cache mirror rules", exc_info=True)
    return rules


def _hops_state(bot: Any) -> dict[tuple[int, int], tuple[int, int, float]] | None:
    state = getattr(bot, "_auto_forward_hops", None)
    if state is None:
        state = {}
        try:
            bot._auto_forward_hops = state
        except Exception:  # pragma: no cover - best-effort
            return None
    return state


def _prune_hops(state: dict[tuple[int, int], tuple[int, int, float]]) -> None:
    now = time.monotonic()
    for key in [k for k, entry in state.items() if now - entry[2] > _HOP_TTL]:
        state.pop(key, None)


@dataclass(slots=True)
class HopInfo:
    """Bookkeeping for a message this bot mirrored."""

    hops: int
    origin_chat_id: int


def _pop_hop(bot: Any, chat_id: int, message_id: int) -> HopInfo | None:
    """Forget and return what we know about a message we might have sent."""
    state = _hops_state(bot)
    if not state:
        return None
    _prune_hops(state)
    entry = state.pop((chat_id, message_id), None)
    return HopInfo(hops=int(entry[0]), origin_chat_id=int(entry[1])) if entry else None


def _remember_hop(bot: Any, chat_id: int, message_id: int, info: HopInfo) -> None:
    state = _hops_state(bot)
    if state is None:  # pragma: no cover - best-effort
        return
    if len(state) >= _HOP_SWEEP_SIZE:
        _prune_hops(state)
    state[(chat_id, message_id)] = (info.hops, info.origin_chat_id, time.monotonic())


def _matches_source(rule: Any, chat_id: int, username: str | None) -> bool:
    if rule.source_id is not None and int(rule.source_id) == chat_id:
        return True
    if rule.source_username and username:
        return rule.source_username.casefold() == username.casefold()
    return False


def _sent_message_ids(dest_chat_id: int, sent: Any) -> list[tuple[int, int]]:
    """Normalise what Telethon returned into ``(chat, message)`` pairs."""
    messages = sent if isinstance(sent, (list, tuple)) else [sent]
    pairs: list[tuple[int, int]] = []
    for message in messages:
        message_id = getattr(message, "id", None)
        if isinstance(message_id, int):
            pairs.append((dest_chat_id, message_id))
    return pairs


async def maybe_auto_forward(bot: Any, event: Any) -> bool:
    """Mirror one incoming message to every chat that asked for its source.

    Returns True when at least one message was delivered. Never raises: the
    caller (the message handler) should not lose a message because a mirror
    failed.
    """
    chat_id = getattr(event, "chat_id", None)
    message = getattr(event, "message", None)
    message_id = getattr(message, "id", None)
    if chat_id is None or not isinstance(message_id, int):
        return False

    try:
        rules = await mirror_rules(bot)
    except Exception:
        logger.debug("Could not load auto-forward rules", exc_info=True)
        return False
    if not rules:
        return False

    username = getattr(getattr(event, "chat", None), "username", None)
    matching = [
        rule for rule in rules if rule.enabled and _matches_source(rule, chat_id, username)
    ]
    if not matching:
        return False

    # Loop guard: `_auto_forward_hops` remembers every message this bot sent,
    # so a mirrored message coming back to the chat it started in (A→B plus
    # B→A, or a longer A→B→C→A cycle) is dropped instead of bouncing forever.
    previous = _pop_hop(bot, chat_id, message_id)
    hops = previous.hops if previous else 0
    origin = previous.origin_chat_id if previous else chat_id
    if previous and origin == chat_id:
        logger.warning("Auto-forward loop guard: %s is already the origin of this message", chat_id)
        return False
    if previous:
        # A mirrored message never travels back to where it started: that is
        # what a A→B plus B→A pair of rules would otherwise do forever.
        matching = [rule for rule in matching if rule.dest_chat_id != origin]
    if hops >= MAX_HOPS:
        logger.warning(
            "Auto-forward loop guard: dropping a message in chat %s after %d hop(s)",
            chat_id,
            hops,
        )
        return False

    delivered = False
    for rule in matching:
        if not matches_types(rule, message):
            continue
        pairs = await _deliver(bot, rule, message)
        if not pairs:
            continue
        delivered = True
        info = HopInfo(hops=hops + 1, origin_chat_id=origin)
        for dest_chat_id, new_message_id in pairs:
            _remember_hop(bot, dest_chat_id, new_message_id, info)
    return delivered


async def _send_once(bot: Any, rule: Any, message: Any) -> Any:
    """One delivery attempt: a real forward, or a hidden copy when asked."""
    if rule.hide_sender:
        return await _copy_message(bot, rule.dest_chat_id, message)
    return await _forward_message(bot, rule.dest_chat_id, message)


async def _deliver(bot: Any, rule: Any, message: Any) -> list[tuple[int, int]]:
    """Forward (or copy) one message for one rule. Empty list on failure."""
    dest = rule.dest_chat_id
    try:
        sent = await _send_once(bot, rule, message)
    except ChatForwardsRestrictedError:
        logger.info(
            "Chat %s forbids forwarding; sending a copy to %s instead",
            getattr(message, "chat_id", "?"),
            dest,
        )
        try:
            sent = await _copy_message(bot, dest, message)
        except Exception as exc:
            logger.warning("Auto-forward copy to %s failed: %s", dest, exc)
            return []
    except FloodWaitError as exc:
        wait = min(float(exc.seconds or 0), _MAX_FLOOD_SLEEP)
        logger.warning("Auto-forward flood wait: pausing %.0fs", wait)
        await asyncio.sleep(wait)
        try:
            sent = await _send_once(bot, rule, message)
        except Exception as retry_exc:
            logger.warning("Auto-forward to %s failed after flood wait: %s", dest, retry_exc)
            return []
    except Exception as exc:
        logger.warning("Auto-forward to chat %s failed: %s", dest, exc)
        return []

    pairs = _sent_message_ids(dest, sent)
    if pairs:
        bot.metrics.incr("auto_forwards")  # type: ignore[attr-defined]
        logger.debug("Auto-forwarded message %s to chat %s", getattr(message, "id", "?"), dest)
    return pairs


async def _forward_message(bot: Any, dest_chat_id: int, message: Any) -> Any:
    return await bot.client.forward_messages(dest_chat_id, message)


def _copy_payload(message: Any) -> tuple[str, Any, bool]:
    """Text, entities and whether a real file has to be re-sent."""
    text = getattr(message, "message", None) or getattr(message, "raw_text", None) or ""
    entities = getattr(message, "entities", None) or None
    media = getattr(message, "media", None)
    kind = classify_message(message)
    # A link-preview "media" is not a file: resending the text is enough, and
    # Telegram rebuilds the preview on its own.
    sendable = media is not None and kind != "link"
    return text, entities, sendable


async def _copy_message(bot: Any, dest_chat_id: int, message: Any) -> Any:
    """Send a copy that carries no "Forwarded from" header.

    Media is re-sent by its file reference, so Telegram does not re-upload
    anything: the message merely looks like one of ours.
    """
    text, entities, sendable = _copy_payload(message)
    kwargs: dict[str, Any] = {}
    if entities:
        kwargs["formatting_entities"] = entities
    if sendable:
        return await bot.client.send_file(dest_chat_id, message, caption=text, **kwargs)
    if not text:
        return None
    return await bot.client.send_message(
        dest_chat_id,
        text,
        link_preview=bool(getattr(message, "web_preview", None)),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SourceRef:
    """A resolved `autoforward` source."""

    key: str
    chat_id: int | None
    title: str
    username: str | None = None


def _source_target(raw: str) -> tuple[Any, str]:
    """Split user input into (what to resolve, stable key for the database)."""
    value = raw.strip()
    if not value:
        raise UsageError(USAGE)

    lowered = value.casefold()
    if lowered in {"me", "self", "saved", "saved messages"}:
        return "me", "me"

    # Links: https://t.me/name, t.me/name, telegram.me/name, with query/extra paths.
    link = re.match(
        r"^(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(.+)$", value, re.IGNORECASE
    )
    if link:
        path = link.group(1).split("?", 1)[0].strip("/")
        if not path or path.startswith("+") or path.startswith("joinchat"):
            raise ValidationError(
                "Invite links are not supported — use the chat's @username or its "
                "numeric id instead."
            )
        value = path.split("/", 1)[0]
        lowered = value.casefold()

    if lowered.startswith("@"):
        value = value[1:]
        lowered = value.casefold()

    if re.fullmatch(r"-?\d+", value):
        number = int(value)
        return number, f"id:{number}"

    if not _USERNAME_RE.match(value):
        raise ValidationError(
            f"`{truncate(raw, 60)}` is not a username, a numeric id or a t.me link."
        )
    return f"@{value}", f"@{lowered}"


def peer_id_of(entity: Any) -> int | None:
    """Telethon-style marked chat id (``-100…`` for channels)."""
    try:
        return int(utils.get_peer_id(entity))
    except Exception:
        pass
    raw = getattr(entity, "id", None)
    if raw is None:
        return None
    raw = int(raw)
    if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
        return int(f"-100{raw}")
    if getattr(entity, "title", None) is not None and not hasattr(entity, "first_name"):
        return -raw  # basic group
    return raw


def _entity_title(entity: Any) -> str:
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    name = " ".join(
        part
        for part in (
            getattr(entity, "first_name", None),
            getattr(entity, "last_name", None),
        )
        if part
    ).strip()
    return name or getattr(entity, "username", None) or "?"


async def resolve_source(bot: Any, raw: str) -> SourceRef:
    """Turn user input into a chat to watch, or explain why we cannot."""
    target, key = _source_target(raw)
    try:
        entity = await bot.client.get_entity(target)
    except Exception as exc:
        raise ValidationError(
            f"Could not resolve `{truncate(raw, 60)}`. Check the spelling, or use the "
            "numeric chat id (forward something from it to Saved Messages to see it)."
        ) from exc

    if getattr(entity, "bot", False) and getattr(entity, "id", None) is None:
        raise ValidationError("That target is not a chat.")

    username = getattr(entity, "username", None)
    return SourceRef(
        key=key,
        chat_id=peer_id_of(entity),
        title=_entity_title(entity),
        username=str(username) if username else None,
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@command(
    "autoforward",
    category=CATEGORY,
    sudo_only=True,
    usage="autoforward <source> [-type ...] [-hide]",
    examples=(
        "autoforward @music_channel -music",
        "autoforward @channel -music -video -hide",
        "autoforward https://t.me/group_name",
        "autoforward 123456789 -photo",
        "autoforward list",
        "autoforward off @music_channel",
        "autoforward remove @music_channel",
    ),
)
async def cmd_autoforward(ctx: Context) -> None:
    """Mirror every new message from a channel, group or user into this chat.

    Flags narrow the mirror to those kinds only; without flags every new
    message is forwarded. `-hide` sends a copy without the "Forwarded from"
    header. Use `list`, `on`, `off` and `remove` to manage the rules of the
    chat the command is typed in.
    """
    ctx.require_single_dash_flags(*_FLAG_ORDER, _ALL_FLAG)

    if not ctx.args:
        await _show_overview(ctx)
        return

    action = _SUBCOMMAND_ALIASES.get(ctx.args[0].casefold(), ctx.args[0].casefold())
    if action in SUBCOMMANDS:
        await _run_action(ctx, action, ctx.args[1:])
        return

    await _save_rule(ctx, ctx.args)


def _split_flags(args: list[str]) -> tuple[list[str], list[str]]:
    """Split arguments into (dashed flags, positional words)."""
    flags: list[str] = []
    positional: list[str] = []
    for arg in args:
        # A negative number is a chat id, not a flag.
        is_flag = arg.startswith("-") and len(arg) > 1 and not _NUMBER_RE.fullmatch(arg)
        if is_flag:
            flags.append(arg.casefold())
        else:
            positional.append(arg)
    return flags, positional


def _require_media_flag(flag: str) -> str:
    """Validate one media flag and return its canonical kind."""
    if flag == _ALL_FLAG:
        raise UsageError(
            f"`{_ALL_FLAG}` only applies to `autoforward list` and `autoforward remove`.\n{USAGE}"
        )
    if flag not in _KNOWN_FLAGS:
        valid = ", ".join(f"`{name}`" for name in _FLAG_ORDER)
        raise UsageError(f"Unknown flag `{flag}`.\nFlags: {valid}")
    return TYPE_FLAGS[flag.lstrip("-")]


async def _save_rule(ctx: Context, args: list[str]) -> None:
    flags, positional = _split_flags(args)
    if not positional:
        raise UsageError(USAGE)
    if len(positional) > 1:
        raise UsageError(
            f"Give exactly one source. Got `{'`, `'.join(positional)}`.\n{USAGE}"
        )

    hide_sender = False
    media_types: list[str] = []
    for flag in flags:
        if flag == _HIDE_FLAG:
            hide_sender = True
            continue
        kind = _require_media_flag(flag)
        if kind not in media_types:
            media_types.append(kind)

    ref = await resolve_source(ctx.bot, positional[0])
    if ref.chat_id is not None and ref.chat_id == ctx.chat_id:
        raise ValidationError(
            "The source is this chat itself — mirroring would only loop. "
            "Type the command in the chat that should RECEIVE the messages."
        )

    await ctx.db.set_auto_forward(
        ctx.chat_id,
        ref.key,
        source_id=ref.chat_id,
        source_title=ref.title,
        source_username=ref.username,
        media_types=tuple(media_types),
        hide_sender=hide_sender,
        created_by=ctx.sender_id,
    )
    ctx.bot.invalidate_auto_forward_cache()

    mode = "hidden copy (no forward header)" if hide_sender else "real forward"
    await ctx.reply(
        "📡 **Auto-forward saved for this chat.**\n"
        f"Source: **{ref.title}** (`{ref.key}`)\n"
        f"Types: {types_label(media_types)}\n"
        f"Mode: {mode}\n\n"
        f"`autoforward list` · `autoforward off {ref.key}` · "
        f"`autoforward remove {ref.key}`"
    )


async def _show_overview(ctx: Context) -> None:
    rules = await ctx.db.list_auto_forwards(ctx.chat_id)
    header = "📡 **Auto-forward**\n" + USAGE
    if not rules:
        await ctx.reply(
            header
            + "\n\nℹ️ No rules in this chat yet. Example: "
            "`autoforward @music_channel -music`."
        )
        return
    await ctx.reply(header + "\n\n" + _render_rules(rules))


async def _run_action(ctx: Context, action: str, args: list[str]) -> None:
    flags, positional = _split_flags(args)
    all_flag = _ALL_FLAG in flags
    for flag in flags:
        if flag != _ALL_FLAG:
            _require_media_flag(flag)

    if action == "list":
        if positional:
            raise UsageError(f"Usage: `autoforward list [-all]`\n{USAGE}")
        await _list_rules(ctx, all_chats=all_flag)
        return

    if len(positional) > 1:
        raise UsageError(f"Give at most one source.\n{USAGE}")

    lookup = positional[0] if positional else None
    if lookup is None and action == "remove" and not all_flag:
        raise UsageError(
            f"Which source? Add it, or use `autoforward remove -all`.\n{USAGE}"
        )
    if lookup is not None and all_flag:
        raise UsageError(f"Pick either a source or `-all`, not both.\n{USAGE}")

    rules = await ctx.db.list_auto_forwards(ctx.chat_id)
    if not rules:
        await ctx.reply("ℹ️ No auto-forward rules in this chat.")
        return

    if lookup is not None:
        rule = _find_rule(rules, lookup)
        if rule is None:
            await ctx.reply(f"ℹ️ No rule for `{truncate(lookup, 60)}` in this chat.")
            return
        targets = [rule]
    else:
        targets = rules

    if action in {"on", "off"}:
        enabled = action == "on"
        if lookup is None:
            changed = await ctx.db.set_all_auto_forwards_enabled(ctx.chat_id, enabled)
        else:
            changed = await ctx.db.set_auto_forward_enabled(
                ctx.chat_id, targets[0].source_key, enabled
            )
        ctx.bot.invalidate_auto_forward_cache()
        if enabled:
            await ctx.reply(f"▶️ Resumed {changed} auto-forward rule(s).")
        else:
            await ctx.reply(
                f"⏸ Paused {changed} auto-forward rule(s). "
                "New messages are skipped while paused; `autoforward on` resumes them."
            )
        return

    # remove
    if all_flag:
        if not await ctx.bot.confirm(
            ctx.event,
            f"⚠️ Delete all {len(rules)} auto-forward rule(s) in this chat?",
        ):
            await ctx.reply("👍 Cancelled.")
            return
        removed = await ctx.db.delete_all_auto_forwards(ctx.chat_id)
        ctx.bot.invalidate_auto_forward_cache()
        await ctx.reply(f"✅ Removed {removed} auto-forward rule(s) from this chat.")
        return

    removed = await ctx.db.delete_auto_forward(ctx.chat_id, targets[0].source_key)
    ctx.bot.invalidate_auto_forward_cache()
    if removed:
        await ctx.reply(f"✅ Removed the rule for `{targets[0].source_key}`.")
    else:
        await ctx.reply(f"ℹ️ No rule for `{targets[0].source_key}` in this chat.")


def _find_rule(rules: list[Any], lookup: str) -> Any | None:
    """Match a rule by source key, @username or numeric id."""
    wanted = lookup.strip()
    if not wanted:
        return None
    folded = wanted.casefold()
    for rule in rules:
        key = str(rule.source_key)
        if key.casefold() in {folded, f"@{folded}"}:
            return rule
        if (
            key.startswith("id:")
            and _NUMBER_RE.fullmatch(wanted)
            and key[3:] == str(int(wanted))
        ):
            return rule
        if rule.source_username and str(rule.source_username).casefold() == folded.lstrip("@"):
            return rule
    return None


def _render_rules(rules: list[Any]) -> str:
    lines = []
    for rule in rules:
        state = "on" if rule.enabled else "off"
        mode = "hidden" if rule.hide_sender else "forward"
        title = f" — {rule.source_title}" if rule.source_title else ""
        lines.append(
            f"• `{rule.source_key}`{title} · {types_label(rule.media_types)}"
            f" · {mode} · [{state}]"
        )
    return "\n".join(lines)


async def _list_rules(ctx: Context, *, all_chats: bool) -> None:
    if not all_chats:
        rules = await ctx.db.list_auto_forwards(ctx.chat_id)
        if not rules:
            await ctx.reply("ℹ️ No auto-forward rules in this chat.")
            return
        await ctx.reply(
            f"📡 **Auto-forward rules in this chat ({len(rules)})**\n\n" + _render_rules(rules)
        )
        return

    rules = await ctx.db.list_all_auto_forwards()
    if not rules:
        await ctx.reply("ℹ️ No auto-forward rules anywhere.")
        return
    by_dest: dict[int, list[Any]] = {}
    for rule in rules:
        by_dest.setdefault(rule.dest_chat_id, []).append(rule)
    blocks = [
        f"**Chat `{dest}`**\n" + _render_rules(by_dest[dest]) for dest in sorted(by_dest)
    ]
    await ctx.reply(
        f"📡 **Auto-forward rules — all chats ({len(rules)})**\n\n" + "\n\n".join(blocks)
    )
