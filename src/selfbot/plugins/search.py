"""Search — account-wide by default, with paged, scannable results.

Quick reference
---------------
* ``search <text>``              search every chat (default)
* ``search <text> -here``        only the current chat
* ``search <text> -from @alice`` filter by sender
* ``search <text> -since 7d``    since a relative/absolute date
* ``search -type photo``         media search (no text needed)
* ``search -chat work``          scope to a chat by name

Pagination: ``more`` / ``back`` / ``page <n>`` / ``open <n>`` /
``recent`` / ``stop``.

All flags use a single dash. Long words (``-from``) are written with one
dash; there is no short-form bundling (``-fv`` is not supported), which
keeps parsing unambiguous.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ..errors import UsageError, ValidationError
from ..registry import Context, command
from ..services.search import (
    MAX_PAGES,
    MEDIA_ATTRS,
    PAGE_SIZE,
    Result,
    SearchQuery,
    SearchRun,
    collect_results,
    parse_date,
    render_empty,
    render_error,
    render_page,
)
from ..utils.text import truncate

logger = logging.getLogger(__name__)

CATEGORY = "Messaging"

# Single-dash flags. Value flags consume the next token; boolean flags stand alone.
_VALUE_FLAGS = {
    "-from", "-since", "-until", "-type", "-chat", "-limit", "-order",
}
_BOOL_FLAGS = {"-here", "-global", "-ungrouped", "-regex", "-exact"}

# Accept the old double-dash forms transparently so muscle memory keeps
# working: --from -> -from, --here -> -here, etc.
# No aliases: all flags use a single dash (e.g. -from, -here).
_ALIASES: dict[str, str] = {}

# Per-user active run / page / progress / task state.
_active: dict[int, SearchRun] = {}
_pages: dict[int, int] = {}
_progress: dict[int, Any] = {}
_tasks: dict[int, asyncio.Task] = {}


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def _parse_args(args: list[str]) -> tuple[list[str], dict[str, str], set[str]]:
    tokens: list[str] = []
    values: dict[str, str] = {}
    booleans: set[str] = set()
    index = 0
    while index < len(args):
        token = _ALIASES.get(args[index], args[index])
        if token in _VALUE_FLAGS:
            if index + 1 >= len(args):
                nice = token.lstrip("-")
                raise UsageError(f"`{token}` needs a value, e.g. `-{nice} alice`.")
            values[token[1:]] = args[index + 1]
            index += 2
        elif token in _BOOL_FLAGS:
            booleans.add(token[1:])
            index += 1
        else:
            tokens.append(token)
            index += 1
    return tokens, values, booleans


def _build_query(args: list[str], *, chat_id: int) -> SearchQuery:
    tokens, values, booleans = _parse_args(args)
    text = " ".join(tokens).strip()

    try:
        page_size = min(int(values.get("limit", str(PAGE_SIZE))), 25)
    except ValueError as exc:
        raise ValidationError("`-limit` must be a number.") from exc
    if page_size < 1:
        raise ValidationError("`-limit` must be at least 1.")

    media = values.get("type")
    if media and media not in MEDIA_ATTRS:
        supported = ", ".join(sorted(set(MEDIA_ATTRS)))
        raise ValidationError(f"Unknown media type `{media}`. Try: {supported}")
    media_attr = MEDIA_ATTRS.get(media or "", media)

    sender: str | None = None
    if "from" in values:
        raw = values["from"].lstrip("@")
        sender = "me" if raw.lower() == "me" else raw

    since = parse_date(values["since"]) if "since" in values else None
    until = parse_date(values["until"], end=True) if "until" in values else None
    order = values.get("order", "newest").lower()
    if order not in {"newest", "oldest", "relevant"}:
        raise ValidationError("`-order` must be newest, oldest or relevant.")

    # Global by default; -here restricts to the current chat.
    global_search = "here" not in booleans

    return SearchQuery(
        text=text,
        sender=sender,
        since=since,
        until=until,
        media=media_attr,
        global_search=global_search,
        chat=values.get("chat"),
        chat_id=chat_id,
        order=order,
        page_size=page_size,
    )


# --------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------


@command(
    "search",
    category=CATEGORY,
    usage=(
        "search <text> [-here] [-from X] [-since D] [-type media] "
        "[-chat name] [-limit N]\n"
        "search more|back|page <n>|open <n>|recent|stop"
    ),
    examples=(
        "search invoice",
        "search invoice -here",
        "search -from @durov -since monday",
        "search contract -chat work -type file",
    ),
)
async def cmd_search(ctx: Context) -> None:
    """Search messages across your account (or this chat with -here)."""
    sub = ctx.args[0].lower() if ctx.args else ""

    if sub == "more":
        return await _show_page(ctx, _pages.get(ctx.sender_id, 1) + 1)
    if sub == "back":
        return await _show_page(ctx, max(1, _pages.get(ctx.sender_id, 1) - 1))
    if sub == "page":
        if len(ctx.args) < 2 or not ctx.args[1].isdigit():
            raise UsageError("Usage: `search page <n>`")
        return await _show_page(ctx, int(ctx.args[1]))
    if sub == "open":
        return await _open_result(ctx)
    if sub == "stop":
        return await _stop_search(ctx)
    if sub == "recent":
        return await _recent_searches(ctx)
    if sub in {"help", "-h"}:
        return await _help(ctx)

    # Fresh search.
    try:
        query = _build_query(ctx.args, chat_id=ctx.chat_id)
    except (UsageError, ValidationError):
        # If parsing fails because there were no recognizable flags, show the
        # friendly help instead of a raw error.
        if not ctx.args or all(a.startswith("-") for a in ctx.args):
            return await _help(ctx)
        raise

    if not (query.text or query.sender or query.since or query.until or query.media):
        return await _help(ctx)

    run = SearchRun(query=query)
    _active[ctx.sender_id] = run
    _pages[ctx.sender_id] = 1

    scope = "this chat" if not query.global_search else (
        query.chat or "all chats"
    )
    status = await ctx.reply(f"🔎 Searching {scope}…")
    _progress[ctx.sender_id] = status

    if query.global_search and not query.chat:
        task = asyncio.create_task(_run_with_progress(ctx, run, status))
        _tasks[ctx.sender_id] = task
        try:
            await task
        except asyncio.CancelledError:
            run.cancel()
        finally:
            _tasks.pop(ctx.sender_id, None)
    else:
        try:
            run.results = await collect_results(ctx.client, query)
        except Exception as exc:
            await _safe_delete(status)
            await ctx.reply(render_error(f"{type(exc).__name__}: {exc}"))
            return

    _order_results(run)
    await _persist(ctx, query, run.results)
    await _safe_delete(status)
    _progress.pop(ctx.sender_id, None)

    if not run.results:
        await ctx.reply(render_empty(query))
        return
    await _reply_chunked(ctx, render_page(run, 1))


async def _help(ctx: Context) -> None:
    prefix = ctx.config.command_prefix
    await ctx.reply(
        "🔎 **Search** — finds messages across every chat by default.\n\n"
        f"• `{prefix}search <text>` — search all chats\n"
        f"• `{prefix}search <text> -here` — only this chat\n"
        f"• `{prefix}search <text> -from @name` — filter sender\n"
        f"• `{prefix}search <text> -since 7d` — since today/yesterday/"
        "monday/1d/1w or YYYY-MM-DD\n"
        f"• `{prefix}search -type photo|video|file|link|...` — media\n"
        f"• `{prefix}search <text> -chat work` — one other chat\n"
        f"• `{prefix}search <text> -limit 15` — results per page\n\n"
        "**Pages:** `more` · `back` · `page <n>` · `open <n>` · "
        "`recent` · `stop`\n\n"
        "Examples:\n"
        f"`{prefix}search invoice`\n"
        f"`{prefix}search contract -chat work -since monday`"
    )


# --------------------------------------------------------------------------
# Pagination / actions
# --------------------------------------------------------------------------


async def _show_page(ctx: Context, number: int) -> None:
    run = _active.get(ctx.sender_id)
    if run is None:
        await ctx.reply("ℹ️ No previous search — run `search <text>` first.")
        return
    number = max(1, min(number, run.page_count))
    _pages[ctx.sender_id] = number
    await _reply_chunked(ctx, render_page(run, number))


async def _open_result(ctx: Context) -> None:
    if len(ctx.args) < 2 or not ctx.args[1].isdigit():
        raise UsageError("Usage: `search open <n>`")
    run = _active.get(ctx.sender_id)
    if run is None:
        raise UsageError("No previous search.")
    page = _pages.get(ctx.sender_id, 1)
    page_index = int(ctx.args[1]) - 1
    real_index = (page - 1) * run.query.page_size + page_index
    if not 0 <= real_index < len(run.results):
        raise ValidationError(f"Result {page_index + 1} isn't on page {page}.")
    result = run.results[real_index]
    link = _message_link(result.chat_id, result.message_id)
    await ctx.reply(
        f"🔗 **Result {real_index + 1}** · {result.chat_title} · "
        f"{result.sender_name}\n{link}\n\n{result.snippet}"
    )


async def _stop_search(ctx: Context) -> None:
    task = _tasks.get(ctx.sender_id)
    run = _active.get(ctx.sender_id)
    if task is not None and not task.done():
        task.cancel()
    if run is not None:
        run.cancel()
    status = _progress.get(ctx.sender_id)
    if status is not None:
        try:
            await ctx.bot.edit(status, "⏹ Stopping…")
        except Exception:
            pass
    await ctx.reply("⏹ Search stopped.")


async def _recent_searches(ctx: Context) -> None:
    rows = await ctx.db.list_searches(ctx.sender_id, limit=15)
    if not rows:
        await ctx.reply("ℹ️ No recent searches yet.")
        return
    lines = ["🕘 **Recent searches**\n"]
    for row in rows:
        marker = "⭐ " if row["saved"] else ""
        lines.append(f"`{row['id']:>3}` {marker}{truncate(row['label'], 70)}")
    lines.append("\nType a new `search`, or `search saved` for bookmarks.")
    await ctx.reply("\n".join(lines))


# --------------------------------------------------------------------------
# Execution helpers
# --------------------------------------------------------------------------


async def _run_with_progress(ctx: Context, run: SearchRun, status: Any) -> None:
    completed = asyncio.Event()

    async def heartbeat() -> None:
        while not completed.is_set():
            await asyncio.sleep(1.5)
            try:
                found = len(run.results)
                scope = run.query.chat or "all chats"
                await ctx.bot.edit(
                    status,
                    f"🔎 Searching {scope}… {found} match"
                    f"{'es' if found != 1 else ''}",
                )
            except Exception:
                pass

    beat = asyncio.create_task(heartbeat())
    try:
        run.results = await collect_results(ctx.client, run.query)
    finally:
        completed.set()
        beat.cancel()
        try:
            await beat
        except Exception:
            pass


def _order_results(run: SearchRun) -> None:
    if run.query.order == "oldest":
        run.results.sort(key=lambda r: r.date)
    elif run.query.order == "relevant" and run.query.text:
        terms = [t.lower() for t in run.query.text.split() if len(t) >= 2]
        run.results.sort(
            key=lambda r: -sum(r.snippet.lower().count(t) for t in terms)
        )
    else:
        run.results.sort(key=lambda r: r.date, reverse=True)


async def _persist(ctx: Context, query: SearchQuery, results: list[Result]) -> None:
    if not results:
        return
    payload = json.dumps(
        {
            "text": query.text,
            "sender": query.sender,
            "since": query.since.isoformat() if query.since else None,
            "until": query.until.isoformat() if query.until else None,
            "media": query.media,
            "global": query.global_search,
            "chat": query.chat,
            "order": query.order,
            "ids": [[r.chat_id, r.message_id] for r in results[: MAX_PAGES * PAGE_SIZE]],
        }
    )
    try:
        await ctx.db.add_search(ctx.sender_id, query.label, payload)
        await ctx.db.prune_searches(ctx.sender_id, keep=50)
    except Exception:
        logger.debug("Could not persist search history", exc_info=True)


async def _reply_chunked(ctx: Context, text: str) -> None:
    from ..utils.text import chunk_text

    for chunk in chunk_text(text, limit=3500):
        await ctx.reply(chunk)


async def _safe_delete(message: Any) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except Exception:
        logger.debug("could not delete search status", exc_info=True)


def _message_link(chat_id: int, message_id: int) -> str:
    # Local import keeps the service module free of Telegram specifics.
    from ..services.search import message_link

    return message_link(chat_id, message_id)
