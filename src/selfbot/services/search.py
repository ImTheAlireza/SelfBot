"""Search execution, rendering and pagination.

Split out from the command module so the formatting and result-shaping logic
can be unit-tested without a Telegram fake. The command layer (plugins/search.py)
handles argument parsing and sends the rendered pages; this module does the
actual searching and produces Markdown strings.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: How many results per page.
PAGE_SIZE = 10
#: Maximum number of pages we remember (enough IDs to back a deep "more").
MAX_PAGES = 5
#: Snippet width (chars) on each side of the match.
KWIC_RADIUS = 70
#: Hard cap on results a single search can collect.
HARD_CAP = 500

MEDIA_ATTRS: dict[str, str] = {
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
_REL_UNITS = re.compile(r"(\d+)\s*([hdwmy])", re.IGNORECASE)


# --------------------------------------------------------------------------
# Query model
# --------------------------------------------------------------------------


@dataclass
class SearchQuery:
    text: str = ""
    sender: str | None = None  # resolved "me" / id / username
    since: datetime | None = None
    until: datetime | None = None
    media: str | None = None  # canonical attribute name
    global_search: bool = False
    chat: str | None = None  # scope a global search to one other chat
    chat_id: int = 0  # current chat, used for non-global searches
    order: str = "newest"  # newest | oldest | relevant
    page_size: int = PAGE_SIZE

    @property
    def label(self) -> str:
        bits: list[str] = []
        scope = "all chats" if self.global_search else "this chat"
        if self.chat:
            scope = self.chat
        bits.append(f"in {scope}")
        if self.text:
            bits.append(f'"{self.text}"')
        if self.sender:
            bits.append(f"from {self.sender}")
        if self.media:
            bits.append(self.media)
        if self.since:
            bits.append(f"since {self.since:%Y-%m-%d}")
        if self.until:
            bits.append(f"until {self.until:%Y-%m-%d}")
        return " · ".join(bits)


@dataclass
class Result:
    chat_id: int
    message_id: int
    chat_title: str
    sender_name: str
    date: datetime
    snippet: str
    media_icon: str = ""
    is_media: bool = False


@dataclass
class SearchRun:
    query: SearchQuery
    results: list[Result] = field(default_factory=list)
    cancelled: bool = False
    scanned: int = 0
    chats_scanned: int = 0

    @property
    def page_count(self) -> int:
        return max(1, (len(self.results) + self.query.page_size - 1) // self.query.page_size)

    def page(self, number: int) -> list[Result]:
        number = max(1, min(number, self.page_count))
        start = (number - 1) * self.query.page_size
        return self.results[start : start + self.query.page_size]

    def cancel(self) -> None:
        self.cancelled = True


# --------------------------------------------------------------------------
# Date parsing
# --------------------------------------------------------------------------


def parse_date(value: str, *, end: bool = False) -> datetime:
    value = value.strip()
    now = datetime.now(timezone.utc)

    # Relative words.
    low = value.lower()
    if low == "today":
        dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif low == "yesterday":
        dt = (now - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif low in {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}:
        target = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"].index(low)
        days_back: int = (now.weekday() - target) % 7 or 7
        dt = (now - timedelta(days=days_back)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    else:
        match = _REL_UNITS.fullmatch(low)
        if match:
            amount = int(match.group(1))
            unit = match.group(2).lower()
            delta = {
                "h": timedelta(hours=amount),
                "d": timedelta(days=amount),
                "w": timedelta(weeks=amount),
                "m": timedelta(days=30 * amount),
                "y": timedelta(days=365 * amount),
            }[unit]
            dt = now - delta
        else:
            for fmt in _DATE_FORMATS:
                try:
                    dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            else:
                from ..errors import ValidationError

                raise ValidationError(
                    f"Could not read date `{value}`. Use YYYY-MM-DD, today, "
                    "yesterday, Monday, or 1d/1w/1m/1y."
                )

    if end:
        dt = dt + timedelta(days=1) - timedelta(microseconds=1)
    return dt


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def relative_time(when: datetime, *, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = now - when
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 7 * 86400:
        return f"{seconds // 86400}d ago"
    if seconds < 365 * 86400:
        return when.strftime("%b %d")
    return when.strftime("%Y-%m-%d")


_MEDIA_ICON = {
    "photo": "🖼",
    "video": "🎥",
    "voice": "🎙",
    "video_note": "🎬",
    "audio": "🎵",
    "document": "📄",
    "sticker": "🧷",
    "gif": "🎞",
    "web_preview": "🔗",
}


def media_icon(message: Any) -> str:
    for attr, icon in _MEDIA_ICON.items():
        if getattr(message, attr, None):
            return icon
    if getattr(message, "media", None):
        return "📎"
    return ""


def media_label(message: Any) -> str:
    for attr, label in (
        ("photo", "photo"),
        ("video", "video"),
        ("voice", "voice"),
        ("video_note", "video note"),
        ("audio", "audio"),
        ("sticker", "sticker"),
        ("gif", "GIF"),
        ("document", "file"),
    ):
        if getattr(message, attr, None):
            extra = ""
            if attr == "document":
                doc = getattr(message, "file", None)
                size = getattr(doc, "size", None)
                name = getattr(doc, "name", None)
                if size:
                    extra = f" · {_format_size(size)}"
                if name:
                    extra = f" · {name}" + extra
            elif attr in {"audio", "video", "voice"}:
                duration = getattr(getattr(message, attr, None), "duration", None)
                if duration:
                    extra = f" · {_format_duration(int(duration))}"
            return f"{label}{extra}"
    return "media"


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _format_duration(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def highlight(text: str, terms: list[str]) -> str:
    """Bold matched terms using Telegram Markdown, escaping the rest."""
    if not text:
        return ""
    escaped = html.escape(text)
    if not terms:
        return escaped
    # Build an alternation of whole-ish terms, longest first to avoid
    # partial overlaps. Case-insensitive.
    terms = sorted({t for t in terms if t}, key=len, reverse=True)
    pattern = "|".join(re.escape(t) for t in terms)
    if not pattern:
        return escaped

    def _wrap(match: re.Match[str]) -> str:
        return f"**{match.group(0)}**"

    return re.sub(pattern, _wrap, escaped, flags=re.IGNORECASE)


def kwic(text: str, terms: list[str], *, radius: int = KWIC_RADIUS) -> str:
    """Return a snippet centered on the first matched term."""
    if not text:
        return "_(no text)_"
    if not terms:
        return text[: radius * 2].strip()

    lowered = text.lower()
    first = -1
    matched_term = ""
    for term in sorted(terms, key=len, reverse=True):
        idx = lowered.find(term.lower())
        if idx != -1 and (first == -1 or idx < first):
            first = idx
            matched_term = term

    if first == -1:
        snippet = text[: radius * 2].rstrip()
    else:
        start = max(0, first - radius)
        end = min(len(text), first + len(matched_term) + radius)
        snippet = text[start:end].rstrip()
        if start > 0:
            snippet = "…" + snippet
        if end < len(text):
            snippet = snippet + "…"
    return highlight(snippet, terms)


def message_link(chat_id: int, message_id: int) -> str:
    text_id = str(chat_id)
    if text_id.startswith("-100"):
        internal = text_id[4:]
        if internal.isdigit():
            return f"https://t.me/c/{internal}/{message_id}"
    return f"tg://openmessage?chat_id={chat_id}&message_id={message_id}"


def host_to_name(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return "provider"
    parts = host.split(".")
    if len(parts) >= 2 and parts[0] in {"api", "www", "openai"}:
        parts = parts[1:]
    return parts[0] if parts else host


# --------------------------------------------------------------------------
# Result collection
# --------------------------------------------------------------------------


async def collect_results(
    client: Any,
    query: SearchQuery,
    *,
    title_cache: dict[int, str] | None = None,
    progress: Any = None,
) -> list[Result]:
    """Run the search and return a list of :class:`Result` objects."""
    title_cache = title_cache if title_cache is not None else {}
    attribute = MEDIA_ATTRS.get(query.media or "", query.media)

    if query.global_search and query.chat:
        target = await _resolve_chat(client, query.chat)
        return await _iter_chat(client, target, query, attribute, progress)
    if query.global_search and query.text:
        return await _global_text_search(
            client, query, attribute, title_cache, progress
        )
    if query.global_search:
        # Media/date/sender-only global search: scan each dialog.
        return await _global_scan(
            client, query, attribute, title_cache, progress
        )
    return await _iter_chat(
        client, query.chat_id, query, attribute, progress
    )


async def _global_text_search(
    client: Any,
    query: SearchQuery,
    attribute: str | None,
    title_cache: dict[int, str],
    progress: Any,
) -> list[Result]:
    out: list[Result] = []
    async for message in client.iter_messages(
        None,
        search=query.text,
        from_user=query.sender,
        limit=min(HARD_CAP, max(query.page_size * MAX_PAGES * 3, 100)),
    ):
        if progress is not None:
            await progress(len(out))
        if not _matches(message, query, attribute):
            continue
        chat_id = _chat_id_of(message)
        title = await _chat_title(client, chat_id, title_cache)
        out.append(await _make_result(message, title, chat_id, query.text))
        if len(out) >= query.page_size * MAX_PAGES:
            break
    return out


async def _global_scan(
    client: Any,
    query: SearchQuery,
    attribute: str | None,
    title_cache: dict[int, str],
    progress: Any,
) -> list[Result]:
    """Media/sender-only global search by iterating each dialog."""
    out: list[Result] = []
    scanned = 0
    async for dialog in client.iter_dialogs(limit=200):
        title = dialog.name or str(dialog.id)
        title_cache.setdefault(dialog.id, title)
        async for message in client.iter_messages(
            dialog.id,
            from_user=query.sender,
            offset_date=query.until,
            limit=500,
        ):
            if not _matches(message, query, attribute):
                continue
            cid = _chat_id_of(message)
            out.append(
                await _make_result(message, title, cid, query.text)
            )
            scanned += 1
            if len(out) >= query.page_size * MAX_PAGES or scanned > 2000:
                break
        if len(out) >= query.page_size * MAX_PAGES or scanned > 2000:
            break
    return out


async def _iter_chat(
    client: Any,
    chat_id: Any,
    query: SearchQuery,
    attribute: str | None,
    progress: Any,
) -> list[Result]:
    out: list[Result] = []
    title = ""
    if isinstance(chat_id, int):
        cache: dict[int, str] = {}
        title = await _chat_title(client, chat_id, cache)

    async for message in client.iter_messages(
        chat_id,
        search=query.text or None,
        from_user=query.sender,
        offset_date=query.until,
        limit=HARD_CAP,
    ):
        if not _matches(message, query, attribute):
            continue
        cid = _chat_id_of(message)
        ctitle = title or (await _chat_title(client, cid, {}))
        out.append(await _make_result(message, ctitle, cid, query.text))
        if len(out) >= query.page_size * MAX_PAGES:
            break
    return out


def _matches(message: Any, query: SearchQuery, attribute: str | None) -> bool:
    if query.since and message.date < query.since:
        return False
    if query.until and message.date > query.until:
        return False
    return not (attribute and not getattr(message, attribute, None))


async def _make_result(
    message: Any, chat_title: str, chat_id: int, query_text: str
) -> Result:
    raw = (getattr(message, "raw_text", "") or "").strip()
    terms = [t for t in query_text.split() if len(t) >= 2] if query_text else []
    icon = media_icon(message)
    is_media = bool(icon)
    if is_media and not raw:
        snippet = f"{icon} {media_label(message)}"
    elif is_media:
        snippet = f"{icon} {media_label(message)} — {kwic(raw, terms)}"
    else:
        snippet = kwic(raw, terms)
    return Result(
        chat_id=chat_id,
        message_id=message.id,
        chat_title=chat_title,
        sender_name=await _sender_name(message),
        date=message.date,
        snippet=snippet,
        media_icon=icon,
        is_media=is_media,
    )


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
    return name or f"@{sender.username}" if getattr(sender, "username", None) else str(
        getattr(sender, "id", "?")
    )


def _chat_id_of(message: Any) -> int:
    peer = message.peer_id
    if hasattr(peer, "channel_id"):
        return int(f"-100{peer.channel_id}")
    if hasattr(peer, "chat_id"):
        return int(f"-{peer.chat_id}")
    if hasattr(peer, "user_id"):
        return int(peer.user_id)
    return 0


async def _chat_title(
    client: Any, chat_id: int, cache: dict[int, str]
) -> str:
    if chat_id in cache:
        return cache[chat_id]
    title = str(chat_id)
    try:
        entity = await client.get_entity(chat_id)
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


async def _resolve_chat(client: Any, name: str) -> Any:
    name = name.lstrip("@")
    try:
        return await client.get_entity(name)
    except Exception as exc:
        from ..errors import ValidationError

        raise ValidationError(f"Could not find chat `{name}`: {exc}") from exc


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_page(run: SearchRun, page: int) -> str:
    """Render one page of results as a clean, scannable Markdown message."""
    page = max(1, min(page, run.page_count))
    results = run.page(page)
    q = run.query

    lines = [_render_header(run, page)]

    if q.global_search and not q.chat:
        groups: dict[str, list[Result]] = {}
        order: list[str] = []
        for result in results:
            groups.setdefault(result.chat_title, []).append(result)
            if result.chat_title not in order:
                order.append(result.chat_title)
        counter = (page - 1) * q.page_size
        for chat_title in order:
            lines.append("")
            lines.append(f"💬 **{chat_title}**")
            for result in groups[chat_title]:
                counter += 1
                lines.append(_render_result(result, counter))
    else:
        lines.append("")
        for index, result in enumerate(results, start=(page - 1) * q.page_size + 1):
            lines.append(_render_result(result, index))

    if run.cancelled:
        lines.extend(["", "_⏹ stopped — partial results_"])

    footer = _page_footer(page, run.page_count)
    if footer:
        lines.extend(["", footer])
    return "\n".join(lines).rstrip()


def _render_header(run: SearchRun, page: int) -> str:
    q = run.query
    count = len(run.results)
    title = f"\"{q.text}\"" if q.text else (q.media or "search")
    scope = q.chat or ("this chat" if not q.global_search else "all chats")
    noun = "result" if count == 1 else "results"
    pager = f" · {page}/{run.page_count}" if run.page_count > 1 else ""
    return f"🔍 **{title}** — {count} {noun} in {scope}{pager}"


def _render_result(result: Result, number: int) -> str:
    when = relative_time(result.date)
    link = message_link(result.chat_id, result.message_id)
    return (
        f"`{number}` **{result.sender_name}** · {when} · [open]({link})\n"
        f"    {result.snippet}"
    )


def _page_footer(page: int, page_count: int) -> str:
    hints = ["`more`" if page < page_count else None, "`back`" if page > 1 else None, "`open <n>`"]
    return "· " + " · ".join(h for h in hints if h)


def render_empty(query: SearchQuery) -> str:
    return f"🔍 No messages matched ({query.label})."


def render_error(message: str) -> str:
    return f"❌ Search failed: `{message}`"
