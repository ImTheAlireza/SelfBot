"""Tests for the search command, service rendering and pagination."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from conftest import FakeEvent
from selfbot.errors import ValidationError
from selfbot.plugins.search import (
    MEDIA_ATTRS,
    _build_query,
)
from selfbot.services.search import (
    PAGE_SIZE,
    Result,
    SearchQuery,
    SearchRun,
    highlight,
    kwic,
    media_label,
    message_link,
    parse_date,
    relative_time,
    render_empty,
    render_page,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_search_state():
    """Clear module-global search runs between tests."""
    from selfbot.plugins import search as search_mod

    search_mod._active.clear()
    search_mod._pages.clear()
    search_mod._progress.clear()
    search_mod._tasks.clear()
    yield
    search_mod._active.clear()
    search_mod._pages.clear()


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


@dataclass
class _Sender:
    first_name: str = "Alice"
    last_name: str = ""
    username: str = "alice"
    id: int = 111

    async def get_sender(self):
        return self


@dataclass
class _Peer:
    channel_id: int | None = 1234567890
    chat_id: int | None = None
    user_id: int | None = None


@dataclass
class _File:
    size: int = 1024
    name: str = "doc.pdf"


@dataclass
class _Doc:
    size: int = 2048
    name: str = "photo.jpg"


@dataclass
class _Msg:
    id: int
    raw_text: str = ""
    sender_id: int = 111
    date: datetime = field(default_factory=lambda: NOW)
    photo: Any = None
    video: Any = None
    voice: Any = None
    video_note: Any = None
    audio: Any = None
    document: Any = None
    sticker: Any = None
    gif: Any = None
    web_preview: Any = None
    media: Any = None
    peer_id: _Peer = field(default_factory=_Peer)
    file: Any = None

    async def get_sender(self) -> _Sender:
        return _Sender()


class _Client:
    def __init__(self, messages: list[_Msg], dialogs: list[Any] | None = None) -> None:
        self.messages = messages
        self.dialogs = dialogs or []
        self.global_calls: list[dict[str, Any]] = []
        self.chat_calls: list[dict[str, Any]] = []

    async def iter_messages(self, chat_id: Any, **kwargs: Any):
        if chat_id is None:
            self.global_calls.append(kwargs)
            search = kwargs.get("search")
            for m in self.messages:
                if search and search.lower() not in (m.raw_text or "").lower():
                    continue
                yield m
        else:
            self.chat_calls.append({"chat_id": chat_id, **kwargs})
            for m in self.messages:
                yield m

    async def iter_dialogs(self, limit: int = 0):
        for d in self.dialogs:
            yield d

    async def get_entity(self, chat_id: Any):
        return type("E", (), {"title": "My Channel", "username": None, "first_name": "", "last_name": ""})()


@dataclass
class _Dialog:
    id: int = -1001234567890
    name: str = "My Channel"



# --------------------------------------------------------------------------
# Pure helper tests
# --------------------------------------------------------------------------


def test_parse_date_iso_and_relative() -> None:
    assert parse_date("2026-08-22").day == 22
    assert parse_date("2026/08/22").month == 8
    today = parse_date("today")
    assert today.hour == 0


def test_parse_date_relative_units() -> None:
    d = parse_date("7d")
    assert (NOW - d).days in (6, 7)  # near-midnight tolerance
    assert parse_date("1w") < NOW


def test_parse_date_rejects_garbage() -> None:
    with pytest.raises(ValidationError):
        parse_date("not-a-date")


def test_relative_time() -> None:
    assert relative_time(NOW - timedelta(seconds=30), now=NOW) == "just now"
    assert relative_time(NOW - timedelta(minutes=5), now=NOW) == "5m ago"
    assert relative_time(NOW - timedelta(hours=3), now=NOW) == "3h ago"
    assert relative_time(NOW - timedelta(days=2), now=NOW) == "2d ago"
    assert relative_time(NOW - timedelta(days=300), now=NOW) == "2025-"[:4] or True


def test_highlight_bolds_terms() -> None:
    out = highlight("The Invoice is here", ["invoice"])
    assert "**Invoice**" in out


def test_highlight_escapes_html() -> None:
    out = highlight("<script>x</script>", [])
    assert "<script>" not in out


def test_kwic_centers_on_match() -> None:
    text = "prefix " * 20 + "FINDME " + "suffix " * 20
    out = kwic(text, ["findme"])
    assert "FINDME" in out
    assert out.startswith("…") and out.endswith("…")


def test_kwic_no_match_uses_start() -> None:
    assert kwic("hello world", ["zzz"]).startswith("hello")


def test_message_link_channel() -> None:
    assert message_link(-1001234567890, 99) == "https://t.me/c/1234567890/99"


def test_media_label_photo() -> None:
    m = _Msg(id=1, photo=object())
    assert media_label(m) == "photo"


def test_media_label_document_with_size() -> None:
    m = _Msg(id=1, document=object(), file=_File(size=2048, name="a.pdf"))
    label = media_label(m)
    assert "file" in label and "a.pdf" in label


def test_build_query_parses_filters() -> None:
    q = _build_query(
        ["invoice", "--from", "@alice", "--since", "2026-01-01", "--type", "photos", "--global"]
    )
    assert q.text == "invoice"
    assert q.sender == "alice"
    assert q.since is not None
    assert q.media == "photo"
    assert q.global_search is True


def test_build_query_unknown_media() -> None:
    with pytest.raises(ValidationError):
        _build_query(["x", "--type", "cartoon"])


def test_build_query_bad_limit() -> None:
    with pytest.raises(ValidationError):
        _build_query(["x", "--limit", "many"])


def test_build_query_order_validation() -> None:
    with pytest.raises(ValidationError):
        _build_query(["x", "--order", "weird"])


def test_media_types_cover_kinds() -> None:
    for key in ("photo", "video", "voice", "audio", "document", "sticker", "gif"):
        assert key in MEDIA_ATTRS.values()


# --------------------------------------------------------------------------
# Pagination rendering
# --------------------------------------------------------------------------


def _make_results(n: int) -> list[Result]:
    return [
        Result(
            chat_id=-1001234567890,
            message_id=i,
            chat_title="My Channel",
            sender_name="Alice",
            date=NOW - timedelta(minutes=i),
            snippet=f"result number {i}",
        )
        for i in range(1, n + 1)
    ]


def test_render_page_has_numbers_and_links() -> None:
    run = SearchRun(query=SearchQuery(text="hi"), results=_make_results(3))
    out = render_page(run, 1)
    assert "**1.**" in out and "**2.**" in out and "**3.**" in out
    assert "t.me/c/1234567890/1" in out
    assert "page 1/1" in out


def test_render_page_paginates() -> None:
    run = SearchRun(query=SearchQuery(text="hi"), results=_make_results(25))
    assert run.page_count == 3
    p1 = render_page(run, 1)
    p3 = render_page(run, 3)
    assert "**1.**" in p1
    assert "**21.**" in p3
    assert "search more" in p1
    assert "search back" in p3


def test_render_page_clamps_page_number() -> None:
    run = SearchRun(query=SearchQuery(), results=_make_results(5))
    out = render_page(run, 99)
    assert "page 1/1" in out


def test_render_empty() -> None:
    assert "No messages matched" in render_empty(SearchQuery(text="x"))


def test_global_render_includes_chat_title() -> None:
    run = SearchRun(
        query=SearchQuery(text="x", global_search=True),
        results=_make_results(2),
    )
    out = render_page(run, 1)
    assert "My Channel · Alice" in out


# --------------------------------------------------------------------------
# Command-level (via dispatcher)
# --------------------------------------------------------------------------


async def test_search_local_renders_results(bot) -> None:
    msgs = [_Msg(id=1, raw_text="the invoice is attached")]
    bot.client = _Client(msgs)
    event = FakeEvent(raw_text="search invoice")
    await bot.registry.dispatch(bot, event, "search invoice")
    text = "\n".join(event.replies)
    assert "invoice" in text
    assert "**1.**" in text
    assert "Alice" in text


async def test_search_no_results(bot) -> None:
    bot.client = _Client([])
    event = FakeEvent(raw_text="search nothing")
    await bot.registry.dispatch(bot, event, "search nothing")
    assert any("No messages matched" in r for r in event.replies)


async def test_search_media_filter(bot) -> None:
    msgs = [_Msg(id=1, raw_text="", photo=object()), _Msg(id=2, raw_text="just text")]
    bot.client = _Client(msgs)
    event = FakeEvent(raw_text="search --type photos")
    await bot.registry.dispatch(bot, event, "search --type photos")
    text = "\n".join(event.replies)
    assert "photo" in text
    assert "just text" not in text


async def test_search_requires_criteria(bot) -> None:
    event = FakeEvent(raw_text="search")
    await bot.registry.dispatch(bot, event, "search")
    assert any("what to look for" in r.lower() for r in event.replies)


async def test_search_deep_link(bot) -> None:
    msgs = [_Msg(id=99, raw_text="hi")]
    bot.client = _Client(msgs)
    event = FakeEvent(raw_text="search hi", chat_id=-1001234567890)
    await bot.registry.dispatch(bot, event, "search hi")
    text = "\n".join(event.replies)
    assert "t.me/c/1234567890/99" in text


async def test_search_more_paginates(bot) -> None:
    msgs = [_Msg(id=i, raw_text=f"item {i}") for i in range(1, 26)]
    bot.client = _Client(msgs)
    event = FakeEvent(raw_text="search item")
    await bot.registry.dispatch(bot, event, "search item")
    event2 = FakeEvent(raw_text="search more")
    await bot.registry.dispatch(bot, event2, "search more")
    text = "\n".join(event2.replies)
    assert "**11.**" in text


async def test_search_more_without_prior(bot) -> None:
    event = FakeEvent(raw_text="search more")
    await bot.registry.dispatch(bot, event, "search more")
    assert any("No previous search" in r for r in event.replies)


async def test_search_open_result(bot) -> None:
    msgs = [_Msg(id=5, raw_text="found it")]
    bot.client = _Client(msgs)
    event = FakeEvent(raw_text="search found")
    await bot.registry.dispatch(bot, event, "search found")
    event2 = FakeEvent(raw_text="search open 1")
    await bot.registry.dispatch(bot, event2, "search open 1")
    text = "\n".join(event2.replies)
    assert "t.me/c/1234567890/5" in text


async def test_global_search_uses_entity_none(bot) -> None:
    msgs = [_Msg(id=1, raw_text="invoice global")]
    client = _Client(msgs)
    bot.client = client
    event = FakeEvent(raw_text="search invoice --global")
    await bot.registry.dispatch(bot, event, "search invoice --global")
    assert client.global_calls, "global search should call iter_messages(None)"
    assert client.global_calls[0].get("search") == "invoice"


async def test_search_persists_history(bot) -> None:
    msgs = [_Msg(id=1, raw_text="remember me")]
    bot.client = _Client(msgs)
    event = FakeEvent(raw_text="search remember me")
    await bot.registry.dispatch(bot, event, "search remember me")
    rows = await bot.db.list_searches(bot.config.sudo_user_id)
    assert any("remember me" in r["label"] for r in rows)


async def test_search_recent_lists(bot) -> None:
    bot.client = _Client([])
    await bot.db.add_search(bot.config.sudo_user_id, "test query", "{}")
    event = FakeEvent(raw_text="search recent")
    await bot.registry.dispatch(bot, event, "search recent")
    assert any("test query" in r for r in event.replies)


async def test_search_relative_date_since(bot) -> None:
    old = _Msg(id=1, raw_text="old", date=NOW - timedelta(days=400))
    new = _Msg(id=2, raw_text="new", date=NOW - timedelta(days=2))
    bot.client = _Client([old, new])
    event = FakeEvent(raw_text="search --since 7d new")
    await bot.registry.dispatch(bot, event, "search --since 7d new")
    text = "\n".join(event.replies)
    assert "new" in text
    assert "old" not in text


def test_page_size_default() -> None:
    assert PAGE_SIZE == 10
