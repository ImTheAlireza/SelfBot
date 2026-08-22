"""Tests for the search command."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from conftest import FakeEvent
from selfbot.errors import ValidationError
from selfbot.plugins.search import (
    MEDIA_TYPES,
    _parse_args,
    _parse_date,
)


@dataclass
class _Sender:
    first_name: str = "Alice"
    last_name: str = ""
    username: str = "alice"
    id: int = 111

    async def get_sender(self):
        return self


@dataclass
class _Msg:
    id: int
    raw_text: str = ""
    sender_id: int = 111
    date: datetime = field(
        default_factory=lambda: datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    )
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

    async def get_sender(self) -> _Sender:
        return _Sender()


class _Client:
    def __init__(self, messages: list[_Msg]) -> None:
        self.messages = messages
        self.calls: list[dict[str, Any]] = []

    async def iter_messages(self, chat_id: Any, **kwargs: Any):
        self.calls.append({"chat_id": chat_id, **kwargs})
        for m in self.messages:
            yield m


async def _run(bot, text: str, messages: list[_Msg]) -> tuple[FakeEvent, _Client]:
    bot.client = _Client(messages)
    event = FakeEvent(raw_text=text)
    await bot.registry.dispatch(bot, event, text)
    return event, bot.client


def test_parse_args_splits_flags() -> None:
    tokens, flags = _parse_args(
        ["hello", "--from", "@alice", "--since", "2026-01-01", "--limit", "5"]
    )
    assert tokens == ["hello"]
    assert flags == {"from": "@alice", "since": "2026-01-01", "limit": "5"}


def test_parse_date_accepts_iso_and_slashes() -> None:
    assert _parse_date("2026-08-22").day == 22
    assert _parse_date("2026/08/22").month == 8


def test_parse_date_rejects_garbage() -> None:
    with pytest.raises(ValidationError):
        _parse_date("not-a-date")


async def test_search_text_returns_match(bot) -> None:
    msgs = [_Msg(id=1, raw_text="the invoice is attached")]
    event, client = await _run(bot, "search invoice", msgs)
    assert any("invoice is attached" in r for r in event.replies)
    assert client.calls[0]["search"] == "invoice"


async def test_search_no_results(bot) -> None:
    event, _ = await _run(bot, "search nothing", [])
    assert any("No messages matched" in r for r in event.replies)


async def test_search_media_filter(bot) -> None:
    msgs = [
        _Msg(id=1, raw_text="", photo=object(), media=True),
        _Msg(id=2, raw_text="just text"),
    ]
    event, _client = await _run(bot, "search --type photos --limit 10", msgs)
    # The text-only message must be filtered out client-side.
    assert not any("just text" in r for r in event.replies)
    assert any("result" in r for r in event.replies)


async def test_search_unknown_media_type(bot) -> None:
    event, _ = await _run(bot, "search --type cartoon", [])
    assert any("Unknown media type" in r for r in event.replies)


async def test_search_from_me(bot) -> None:
    msgs = [_Msg(id=1, raw_text="mine")]
    _, client = await _run(bot, "search mine --from me", msgs)
    assert client.calls[0]["from_user"] == "me"


async def test_search_since_filter(bot) -> None:
    old = _Msg(id=1, raw_text="old", date=datetime(2020, 1, 1, tzinfo=timezone.utc))
    new = _Msg(id=2, raw_text="new", date=datetime(2026, 6, 1, tzinfo=timezone.utc))
    event, _ = await _run(bot, "search --since 2026-01-01 new", [old, new])
    assert any("new" in r for r in event.replies)
    assert not any("old" in r for r in event.replies)


async def test_search_requires_criteria(bot) -> None:
    event = FakeEvent(raw_text="search")
    await bot.registry.dispatch(bot, event, "search")
    assert any("what to look for" in r.lower() for r in event.replies)


async def test_search_deep_link_for_channel(bot) -> None:
    msgs = [_Msg(id=99, raw_text="hi")]
    bot.chat_id_for = -1001234567890  # type: ignore[attr-defined]
    event = FakeEvent(raw_text="search hi", chat_id=-1001234567890)
    bot.client = _Client(msgs)
    await bot.registry.dispatch(bot, event, event.raw_text)
    assert any("t.me/c/1234567890/99" in r for r in event.replies)


def test_media_types_cover_delete_types() -> None:
    # The search types should at least know the kinds used by the del command.
    for key in ("photo", "video", "voice", "audio", "document", "sticker", "gif"):
        assert key in MEDIA_TYPES.values()
