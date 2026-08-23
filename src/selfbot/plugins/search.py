"""Search the current chat — or the whole account — with paged results.

Commands
--------
* ``search <text> [filters]`` run a search and show page 1
* ``search more`` / ``search next``  — next page of the last search
* ``search back`` / ``search prev``  — previous page
* ``search page <n>``                — jump to a page
* ``search open <n>``                — show the deep link for result n
* ``search recent``                  — list recent searches
* ``search stop``                    — cancel an in-progress global scan
* ``search save <n>`` / ``search saved`` — name/bookmark a recent search

Filters: ``--from <id|@username|me>``, ``--since/--until <date|1d>``,
``--type <media>``, ``--chat <name>``, ``--global``, ``--limit N``,
``--order newest|oldest|relevant``.
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
    host_to_name,
    message_link,
    parse_date,
    render_empty,
    render_error,
    render_page,
)
from ..utils.text import truncate

logger = logging.getLogger(__name__)

CATEGORY = "Messaging"

#: Per-user active runs (for pagination/cancellation).
_active: dict[int, SearchRun] = {}
#: Per-user current page number.
_pages: dict[int, int] = {}
#: Per-user progress status message.
_progress: dict[int, Any] = {}
#: The currently-running global search task, per user.
_tasks: dict[int, asyncio.Task] = {}


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------

_VALUE_FLAGS = {"--from", "--since", "--until", "--type", "--chat", "--limit", "--order"}
_BOOL_FLAGS = {"--global", "--ungrouped", "--regex", "--exact"}


def _parse_args(args: list[str]) -> tuple[list[str], dict[str, str], set[str]]:
    tokens: list[str] = []
    values: dict[str, str] = {}
    booleans: set[str] = set()
    index = 0
    while index < len(args):
        token = args[index]
        if token in _VALUE_FLAGS:
            if index + 1 >= len(args):
                raise UsageError(f"`{token}` needs a value.")
            values[token[2:]] = args[index + 1]
            index += 2
        elif token in _BOOL_FLAGS:
            booleans.add(token[2:])
            index += 1
        else:
            tokens.append(token)
            index += 1
    return tokens, values, booleans


def _build_query(args: list[str]) -> SearchQuery:
    tokens, values, booleans = _parse_args(args)
    text = " ".join(tokens).strip()

    try:
        page_size = min(int(values.get("limit", str(PAGE_SIZE))), 25)
    except ValueError as exc:
        raise ValidationError("`--limit` must be a number.") from exc
    if page_size < 1:
        raise ValidationError("`--limit` must be at least 1.")

    media = values.get("type")
    if media and media not in MEDIA_ATTRS:
        supported = ", ".join(sorted(set(MEDIA_ATTRS)))
        raise ValidationError(f"Unknown media type `{media}`. Supported: {supported}")
    media_attr = MEDIA_ATTRS.get(media or "", media)

    sender: str | None = None
    if "from" in values:
        raw = values["from"].lstrip("@")
        sender = "me" if raw.lower() == "me" else raw

    since = parse_date(values["since"]) if "since" in values else None
    until = parse_date(values["until"], end=True) if "until" in values else None
    order = values.get("order", "newest").lower()
    if order not in {"newest", "oldest", "relevant"}:
        raise ValidationError("`--order` must be newest, oldest or relevant.")

    return SearchQuery(
        text=text,
        sender=sender,
        since=since,
        until=until,
        media=media_attr,
        global_search="global" in booleans,
        chat=values.get("chat"),
        chat_id=0,  # filled in by the command (it knows the current chat)
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
        "search <text> [--from X] [--since D] [--until D] [--type media] "
        "[--chat name] [--global] [--limit N] [--order newest|oldest|relevant]\n"
        "search more|back|page <n>|open <n>|recent|stop|saved"
    ),
    examples=(
        "search invoice",
        "search --from @durov --since 2026-01-01",
        "search contract --global --chat work --limit 15",
        "search more",
        "search open 3",
    ),
)
async def cmd_search(ctx: Context) -> None:
    """Search messages, with paged results and account-wide ``--global``."""
    sub = ctx.args[0].lower() if ctx.args else ""

    # Pagination / control subcommands take no extra parsing.
    if sub in {"more", "next"}:
        return await _show_page(ctx, (_pages.get(ctx.sender_id, 1)) + 1)
    if sub in {"back", "prev", "previous"}:
        return await _show_page(ctx, max(1, (_pages.get(ctx.sender_id, 1)) - 1))
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
    if sub == "saved":
        return await _saved_searches(ctx)
    if sub == "save":
        return await _save_search(ctx)

    # Otherwise it's a fresh search.
    query = _build_query(ctx.args)
    if query.global_search and not (query.text or query.media or query.sender):
        raise UsageError(
            "`--global` needs a search term, `--type media`, or `--from <who>`."
        )
    if not (query.text or query.sender or query.since or query.until or query.media):
        raise UsageError(
            "Tell me what to look for.\n"
            "Usage: `search <text> [--from X] [--since D] [--until D] "
            "[--type media] [--global] [--limit N]`"
        )

    query.chat_id = ctx.chat_id
    run = SearchRun(query=query)
    _active[ctx.sender_id] = run
    _pages[ctx.sender_id] = 1

    status = await ctx.reply(
        "🌍 Searching across all chats…" if query.global_search else "🔍 Searching…"
    )
    _progress[ctx.sender_id] = status

    if query.global_search:
        # Run the (potentially slow) global scan as a cancellable task with a
        # progress heartbeat so it never looks frozen.
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
    index = int(ctx.args[1]) - 1
    page = _pages.get(ctx.sender_id, 1)
    offset = (page - 1) * run.query.page_size
    real_index = offset + index if index < run.query.page_size else index
    if real_index < 0 or real_index >= len(run.results):
        raise ValidationError(f"Result {index + 1} isn't on this page.")
    result = run.results[real_index]
    link = message_link(result.chat_id, result.message_id)
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
        await ctx.reply("ℹ️ No recent searches.")
        return
    lines = ["🕘 **Recent searches**\n"]
    for row in rows:
        marker = "⭐ " if row["saved"] else ""
        lines.append(f"`{row['id']:>3}` {marker}{truncate(row['label'], 70)}")
    lines.append("\nRe-run with `search recent <id>` (coming soon) or type a new `search`.")
    await ctx.reply("\n".join(lines))


async def _saved_searches(ctx: Context) -> None:
    rows = await ctx.db.list_searches(ctx.sender_id, saved_only=True, limit=30)
    if not rows:
        await ctx.reply("ℹ️ No saved searches. Use `search save <n>` after a search.")
        return
    lines = ["⭐ **Saved searches**\n"]
    for row in rows:
        lines.append(f"`{row['id']:>3}` {truncate(row['label'], 70)}")
    await ctx.reply("\n".join(lines))


async def _save_search(ctx: Context) -> None:
    if len(ctx.args) < 2 or not ctx.args[1].isdigit():
        raise UsageError("Usage: `search save <recent-id>`")
    search_id = int(ctx.args[1])
    row = await ctx.db.get_search(search_id)
    if row is None or row["user_id"] != ctx.sender_id:
        raise ValidationError(f"No saved/recent search #{search_id}.")
    await ctx.db.set_search_saved(search_id, True)
    await ctx.reply(f"⭐ Saved search #{search_id}.")


# --------------------------------------------------------------------------
# Execution helpers
# --------------------------------------------------------------------------


async def _run_with_progress(ctx: Context, run: SearchRun, status: Any) -> None:
    """Heartbeat that edits the status message while a global scan runs."""
    completed = asyncio.Event()

    async def heartbeat() -> None:
        while not completed.is_set():
            await asyncio.sleep(1.5)
            try:
                found = len(run.results)
                if run.chats_scanned:
                    text = (
                        f"🌍 Scanning… {run.chats_scanned} chats, "
                        f"{found} match{'es' if found != 1 else ''}"
                    )
                else:
                    text = f"🌍 Searching… {found} match{'es' if found != 1 else ''}"
                await ctx.bot.edit(status, text)
            except Exception:
                pass

    beat = asyncio.create_task(heartbeat())
    try:
        run.results = await collect_results(
            ctx.client,
            run.query,
            progress=lambda n: None,
        )
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
            key=lambda r: -sum(
                r.snippet.lower().count(t) for t in terms
            )
        )
    else:
        run.results.sort(key=lambda r: r.date, reverse=True)


async def _persist(ctx: Context, query: SearchQuery, results: list[Result]) -> None:
    if not results:
        return
    # Store enough to re-run; the result list itself isn't cached in the DB
    # (it can be re-fetched), but the label and flags are.
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
    """Split a long page on result boundaries and send each chunk."""
    from ..utils.text import chunk_text

    chunks = chunk_text(text, limit=3500)
    for chunk in chunks:
        await ctx.reply(chunk)


async def _safe_delete(message: Any) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except Exception:
        logger.debug("could not delete search status", exc_info=True)


# Backwards-compatible aliases (the old module exposed these names).
MEDIA_TYPES = MEDIA_ATTRS
_parse_date = parse_date


# Re-exported for tests/backward compatibility.
__all__ = [
    "CATEGORY",
    "MEDIA_ATTRS",
    "MEDIA_TYPES",
    "cmd_search",
    "host_to_name",
    "message_link",
    "parse_date",
]
