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
    _parse_args,
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
CHAT_ID = -1001234567890


@pytest.fixture(autouse=True)
def _reset_search_state():
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
    size: int = 2048
    name: str = "doc.pdf"


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
    id: int = CHAT_ID
    name: str = "My Channel"


# --------------------------------------------------------------------------
# Pure helper tests
# --------------------------------------------------------------------------


def test_parse_date_iso_and_relative() -> None:
    assert parse_date("2026-08-22").day == 22
    assert parse_date("today").hour == 0


def test_parse_date_relative_units() -> None:
    d = parse_date("7d")
    age = datetime.now(timezone.utc) - d
    assert abs(age - timedelta(days=7)) < timedelta(seconds=1)


def test_parse_date_rejects_garbage() -> None:
    with pytest.raises(ValidationError):
        parse_date("not-a-date")


def test_relative_time() -> None:
    assert relative_time(NOW - timedelta(seconds=30), now=NOW) == "just now"
    assert relative_time(NOW - timedelta(minutes=5), now=NOW) == "5m ago"
    assert relative_time(NOW - timedelta(hours=3), now=NOW) == "3h ago"
    assert relative_time(NOW - timedelta(days=2), now=NOW) == "2d ago"


def test_highlight_bolds_terms() -> None:
    assert "**Invoice**" in highlight("The Invoice is here", ["invoice"])


def test_highlight_escapes_html() -> None:
    assert "<script>" not in highlight("<script>x</script>", [])


def test_kwic_centers_on_match() -> None:
    text = "prefix " * 20 + "FINDME " + "suffix " * 20
    out = kwic(text, ["findme"])
    assert "FINDME" in out and out.startswith("…") and out.endswith("…")


def test_message_link_channel() -> None:
    assert message_link(CHAT_ID, 99) == "https://t.me/c/1234567890/99"


def test_media_label_photo() -> None:
    assert media_label(_Msg(id=1, photo=object())) == "photo"


def test_media_label_document() -> None:
    m = _Msg(id=1, document=object(), file=_File())
    label = media_label(m)
    assert "file" in label and "doc.pdf" in label


def test_single_dash_flags_parsed() -> None:
    tokens, values, booleans = _parse_args(
        ["invoice", "-from", "alice", "-since", "7d", "-here"]
    )
    assert tokens == ["invoice"]
    assert values["from"] == "alice"
    assert values["since"] == "7d"
    assert "here" in booleans


def test_build_query_global_by_default() -> None:
    q = _build_query(["invoice"], chat_id=CHAT_ID)
    assert q.global_search is True


def test_build_query_here_restricts_to_chat() -> None:
    q = _build_query(["invoice", "-here"], chat_id=CHAT_ID)
    assert q.global_search is False
    assert q.chat_id == CHAT_ID


def test_build_query_single_dash_filters() -> None:
    q = _build_query(
        ["invoice", "-from", "alice", "-since", "2026-01-01", "-type", "photos"],
        chat_id=CHAT_ID,
    )
    assert q.text == "invoice"
    assert q.sender == "alice"
    assert q.media == "photo"


def test_build_query_chat_scope() -> None:
    q = _build_query(["x", "-chat", "work"], chat_id=CHAT_ID)
    assert q.chat == "work"


def test_build_query_unknown_media() -> None:
    with pytest.raises(ValidationError):
        _build_query(["x", "-type", "cartoon"], chat_id=CHAT_ID)


def test_build_query_bad_limit() -> None:
    with pytest.raises(ValidationError):
        _build_query(["x", "-limit", "many"], chat_id=CHAT_ID)


def test_media_types_cover_kinds() -> None:
    for key in ("photo", "video", "voice", "audio", "document", "sticker", "gif"):
        assert key in MEDIA_ATTRS.values()


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _make_results(n: int) -> list[Result]:
    return [
        Result(
            chat_id=CHAT_ID,
            message_id=i,
            chat_title="My Channel",
            sender_name="Alice",
            date=NOW - timedelta(minutes=i),
            snippet=f"result {i}",
        )
        for i in range(1, n + 1)
    ]


def test_render_page_uses_backtick_numbers() -> None:
    run = SearchRun(query=SearchQuery(text="hi"), results=_make_results(2))
    out = render_page(run, 1)
    assert "`1` **Alice**" in out
    assert "`2` **Alice**" in out
    assert "t.me/c/1234567890/1" in out


def test_render_page_groups_global_by_chat() -> None:
    results = _make_results(2)
    results[0].chat_title = "Work"
    results[1].chat_title = "Work"
    run = SearchRun(query=SearchQuery(text="hi", global_search=True), results=results)
    out = render_page(run, 1)
    assert "💬 **Work**" in out


def test_render_page_paginates() -> None:
    run = SearchRun(query=SearchQuery(text="hi"), results=_make_results(25))
    assert render_page(run, 1).count("`1`") == 1
    p3 = render_page(run, 3)
    assert "`21`" in p3
    assert "`back`" in p3


def test_render_page_header_shows_scope() -> None:
    run = SearchRun(query=SearchQuery(text="invoice", global_search=True), results=_make_results(3))
    out = render_page(run, 1)
    assert "all chats" in out
    local = SearchRun(
        query=SearchQuery(text="invoice", global_search=False), results=_make_results(3)
    )
    assert "this chat" in render_page(local, 1)


def test_render_empty() -> None:
    assert "No messages matched" in render_empty(SearchQuery(text="x"))


# --------------------------------------------------------------------------
# Command dispatch
# --------------------------------------------------------------------------


async def test_search_is_global_by_default(bot) -> None:
    client = _Client([_Msg(id=1, raw_text="invoice global")])
    bot.client = client
    event = FakeEvent(raw_text="search invoice", chat_id=CHAT_ID)
    await bot.registry.dispatch(bot, event, "search invoice")
    assert client.global_calls, "default search must be account-wide (entity=None)"


async def test_search_here_uses_current_chat(bot) -> None:
    client = _Client([_Msg(id=1, raw_text="invoice local")])
    bot.client = client
    event = FakeEvent(raw_text="search invoice -here", chat_id=CHAT_ID)
    await bot.registry.dispatch(bot, event, "search invoice -here")
    assert client.chat_calls and not client.global_calls


async def test_search_no_args_shows_help(bot) -> None:
    event = FakeEvent(raw_text="search")
    await bot.registry.dispatch(bot, event, "search")
    text = "\n".join(event.replies)
    assert "Search" in text and "-here" in text and "-from" in text


async def test_search_no_results(bot) -> None:
    bot.client = _Client([])
    event = FakeEvent(raw_text="search nothing", chat_id=CHAT_ID)
    await bot.registry.dispatch(bot, event, "search nothing")
    assert any("No messages matched" in r for r in event.replies)


async def test_search_media_filter(bot) -> None:
    msgs = [_Msg(id=1, raw_text="", photo=object()), _Msg(id=2, raw_text="text")]
    bot.client = _Client(msgs)
    event = FakeEvent(raw_text="search -type photos -here", chat_id=CHAT_ID)
    await bot.registry.dispatch(bot, event, "search -type photos -here")
    text = "\n".join(event.replies)
    assert "photo" in text and "text" not in text


async def test_search_more_paginates(bot) -> None:
    msgs = [_Msg(id=i, raw_text=f"item {i}") for i in range(1, 26)]
    bot.client = _Client(msgs)
    event = FakeEvent(raw_text="search item", chat_id=CHAT_ID)
    await bot.registry.dispatch(bot, event, "search item")
    event2 = FakeEvent(raw_text="search more", chat_id=CHAT_ID)
    await bot.registry.dispatch(bot, event2, "search more")
    assert any("`11`" in r for r in event2.replies)


async def test_search_open_result(bot) -> None:
    bot.client = _Client([_Msg(id=5, raw_text="found it")])
    event = FakeEvent(raw_text="search found", chat_id=CHAT_ID)
    await bot.registry.dispatch(bot, event, "search found")
    event2 = FakeEvent(raw_text="search open 1", chat_id=CHAT_ID)
    await bot.registry.dispatch(bot, event2, "search open 1")
    assert any("t.me/c/1234567890/5" in r for r in event2.replies)


async def test_search_persists_history(bot) -> None:
    bot.client = _Client([_Msg(id=1, raw_text="remember me")])
    event = FakeEvent(raw_text="search remember me", chat_id=CHAT_ID)
    await bot.registry.dispatch(bot, event, "search remember me")
    rows = await bot.db.list_searches(bot.config.sudo_user_id)
    assert any("remember me" in r["label"] for r in rows)


def test_page_size_default() -> None:
    assert PAGE_SIZE == 10
