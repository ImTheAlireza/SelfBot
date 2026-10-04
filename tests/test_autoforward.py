"""`autoforward` — mirroring a source chat's new messages into a chat."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.errors import ChatForwardsRestrictedError

from conftest import FakeEvent
from selfbot.plugins import forwarding
from selfbot.plugins.forwarding import classify_message, matches_types

DEST = -100111
SOURCE_ID = -100555


@dataclass
class FakeChannel:
    """A channel as Telethon would hand it back from ``get_entity``."""

    id: int
    title: str = "Music Channel"
    username: str | None = "music"
    broadcast: bool = True
    megagroup: bool = False


@dataclass
class FakeMessage:
    """Only the attributes the mirror code inspects."""

    id: int = 1
    message: str = ""
    media: Any = None
    entities: Any = None
    chat_id: int = SOURCE_ID
    photo: Any = None
    video: Any = None
    voice: Any = None
    video_note: Any = None
    audio: Any = None
    document: Any = None
    sticker: Any = None
    gif: Any = None
    web_preview: Any = None


@dataclass
class MirrorEvent:
    chat_id: int
    message: FakeMessage
    chat: Any = None
    out: bool = False
    sender_id: int = 999


def register_source(bot: Any, key: str = "@music", chat_id: int = 555, **kwargs: Any) -> FakeChannel:
    channel = FakeChannel(id=chat_id, **kwargs)
    bot.client.entities[key] = channel
    return channel


async def dispatch(bot: Any, text: str, chat_id: int = DEST) -> FakeEvent:
    event = FakeEvent(raw_text=text, chat_id=chat_id)
    assert await bot.registry.dispatch(bot, event, text)
    return event


def reply_of(event: FakeEvent) -> str:
    return " ".join(event.replies)


# ---------------------------------------------------------------------------
# Saving rules
# ---------------------------------------------------------------------------


async def test_rule_with_type_flag_is_saved(bot) -> None:
    register_source(bot)

    event = await dispatch(bot, "autoforward @music -music")

    rules = await bot.db.list_auto_forwards(DEST)
    assert len(rules) == 1
    rule = rules[0]
    assert rule.source_key == "@music"
    assert rule.source_id == SOURCE_ID  # channels are stored with their -100 mark
    assert rule.media_types == ("audio",)
    assert rule.hide_sender is False
    assert rule.enabled is True
    assert rule.source_title == "Music Channel"
    assert "Auto-forward saved" in reply_of(event)
    assert bot.auto_forward_cache_invalidated == 1


async def test_without_flags_every_message_is_mirrored(bot) -> None:
    register_source(bot)

    await dispatch(bot, "autoforward @music")

    rule = (await bot.db.list_auto_forwards(DEST))[0]
    assert rule.media_types == ()


@pytest.mark.parametrize(
    ("flags", "expected", "hidden"),
    [
        ("-music -video", ("audio", "video"), False),
        ("-musics -videomsg", ("audio", "video_note"), False),
        ("-photo -photo", ("photo",), False),
        ("-music -hide", ("audio",), True),
        ("-hide", (), True),
        ("-text -link -file", ("text", "link", "file"), False),
    ],
)
async def test_flags_are_combined(bot, flags: str, expected: tuple[str, ...], hidden: bool) -> None:
    register_source(bot)

    await dispatch(bot, f"autoforward @music {flags}")

    rule = (await bot.db.list_auto_forwards(DEST))[0]
    assert rule.media_types == expected
    assert rule.hide_sender is hidden


async def test_tme_link_and_numeric_id_resolve_to_the_same_source(bot) -> None:
    register_source(bot)
    bot.client.entities[-100555] = FakeChannel(id=555)

    await dispatch(bot, "autoforward https://t.me/music -photo")
    await dispatch(bot, "autoforward -100555 -video")

    rules = {rule.source_key for rule in await bot.db.list_auto_forwards(DEST)}
    assert rules == {"@music", "id:-100555"}


async def test_saving_the_same_source_twice_replaces_the_rule(bot) -> None:
    register_source(bot)

    await dispatch(bot, "autoforward @music -music")
    await dispatch(bot, "autoforward @music -video -hide")

    rules = await bot.db.list_auto_forwards(DEST)
    assert len(rules) == 1
    assert rules[0].media_types == ("video",)
    assert rules[0].hide_sender is True


async def test_re_adding_a_paused_rule_turns_it_on_again(bot) -> None:
    register_source(bot)
    await dispatch(bot, "autoforward @music")
    await dispatch(bot, "autoforward off @music")
    assert (await bot.db.list_auto_forwards(DEST))[0].enabled is False

    await dispatch(bot, "autoforward @music -photo")

    rule = (await bot.db.list_auto_forwards(DEST))[0]
    assert rule.enabled is True
    assert rule.media_types == ("photo",)


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("autoforward @music -banana", "Unknown flag"),
        ("autoforward @nope -music", "Could not resolve"),
        ("autoforward https://t.me/+AbCdEf", "Invite links are not supported"),
        ("autoforward", "No rules in this chat yet"),
        ("autoforward -music", "autoforward <source>"),
        ("autoforward @music @other", "exactly one source"),
    ],
)
async def test_bad_input_is_explained(bot, text: str, fragment: str) -> None:
    register_source(bot)

    event = await dispatch(bot, text)

    assert fragment in reply_of(event)


async def test_source_cannot_be_the_destination_chat(bot) -> None:
    # A channel whose marked id is exactly the chat the command was typed in.
    bot.client.entities["@here"] = FakeChannel(id=111, username="here", title="Here")

    event = await dispatch(bot, "autoforward @here", chat_id=DEST)

    assert "would only loop" in reply_of(event)
    assert await bot.db.list_auto_forwards(DEST) == []


async def test_the_command_requires_owner(bot, registry) -> None:
    register_source(bot)
    event = FakeEvent(raw_text="autoforward @music", out=False, sender_id=12345)

    assert await bot.registry.dispatch(bot, event, "autoforward @music")

    assert "owner" in reply_of(event).lower()


# ---------------------------------------------------------------------------
# Mirroring
# ---------------------------------------------------------------------------


async def test_mirror_forwards_matching_types_only(bot) -> None:
    register_source(bot)
    await dispatch(bot, "autoforward @music -music")
    bot.client.forwarded.clear()

    audio = FakeMessage(id=11, audio=object(), document=object(), media=object())
    assert await forwarding.maybe_auto_forward(bot, MirrorEvent(SOURCE_ID, audio)) is True
    assert len(bot.client.forwarded) == 1
    assert bot.client.forwarded[0]["chat_id"] == DEST

    photo = FakeMessage(id=12, photo=object(), media=object())
    assert await forwarding.maybe_auto_forward(bot, MirrorEvent(SOURCE_ID, photo)) is False
    assert len(bot.client.forwarded) == 1


async def test_mirror_ignores_other_chats_and_unconfigured_sources(bot) -> None:
    register_source(bot)
    await dispatch(bot, "autoforward @music")

    other = FakeMessage(id=13, message="hi")
    assert await forwarding.maybe_auto_forward(bot, MirrorEvent(-100999, other)) is False
    assert bot.client.forwarded == []


async def test_hide_sends_media_without_a_forward_header(bot) -> None:
    register_source(bot)
    await dispatch(bot, "autoforward @music -music -hide")
    bot.client.forwarded.clear()

    message = FakeMessage(id=21, message="listen", audio=object(), media=object(), entities=["e"])
    assert await forwarding.maybe_auto_forward(bot, MirrorEvent(SOURCE_ID, message)) is True

    assert bot.client.forwarded == []
    assert len(bot.client.sent_files) == 1
    sent = bot.client.sent_files[0]
    assert sent["chat_id"] == DEST
    assert sent["caption"] == "listen"
    assert sent["formatting_entities"] == ["e"]


async def test_hide_sends_text_as_a_plain_message(bot) -> None:
    register_source(bot)
    await dispatch(bot, "autoforward @music -text -hide")

    message = FakeMessage(id=22, message="hello")
    assert await forwarding.maybe_auto_forward(bot, MirrorEvent(SOURCE_ID, message)) is True

    assert bot.client.sent_files == []
    assert (DEST, "hello") in bot.client.sent_messages


async def test_hide_rebuilds_link_previews_from_the_text(bot) -> None:
    register_source(bot)
    await dispatch(bot, "autoforward @music -link -hide")

    message = FakeMessage(
        id=23, message="https://example.com", media=object(), web_preview=object()
    )
    assert await forwarding.maybe_auto_forward(bot, MirrorEvent(SOURCE_ID, message)) is True

    assert bot.client.sent_files == []
    assert (DEST, "https://example.com") in bot.client.sent_messages


async def test_restricted_chats_fall_back_to_a_copy(bot) -> None:
    register_source(bot)
    await dispatch(bot, "autoforward @music")
    bot.client.forward_error = ChatForwardsRestrictedError(None)

    message = FakeMessage(id=31, message="protected")
    assert await forwarding.maybe_auto_forward(bot, MirrorEvent(SOURCE_ID, message)) is True

    assert bot.client.forwarded == []
    assert (DEST, "protected") in bot.client.sent_messages


async def test_forwarding_failures_never_break_the_message_handler(bot) -> None:
    register_source(bot)
    await dispatch(bot, "autoforward @music")
    bot.client.forward_error = RuntimeError("network is on fire")

    message = FakeMessage(id=32, message="still fine")
    assert await forwarding.maybe_auto_forward(bot, MirrorEvent(SOURCE_ID, message)) is False


async def test_restricted_chats_with_an_uncopyable_message_are_swallowed(bot) -> None:
    register_source(bot)
    await dispatch(bot, "autoforward @music")
    bot.client.forward_error = ChatForwardsRestrictedError(None)

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("copy failed too")

    bot.client.send_file = boom
    message = FakeMessage(id=33, message="unforwardable", media=object(), audio=object())

    assert await forwarding.maybe_auto_forward(bot, MirrorEvent(SOURCE_ID, message)) is False


async def test_flood_wait_is_retried_once(bot) -> None:
    from telethon.errors import FloodWaitError

    register_source(bot)
    await dispatch(bot, "autoforward @music")

    attempts: list[int] = []
    real_forward = bot.client.forward_messages

    async def flaky(chat_id: Any, messages: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            raise FloodWaitError(request=None, capture=0)
        return await real_forward(chat_id, messages, **kwargs)

    bot.client.forward_messages = flaky
    message = FakeMessage(id=34, message="retry me")

    assert await forwarding.maybe_auto_forward(bot, MirrorEvent(SOURCE_ID, message)) is True
    assert len(attempts) == 2
    assert len(bot.client.forwarded) == 1


async def test_paused_rules_are_skipped(bot) -> None:
    register_source(bot)
    await dispatch(bot, "autoforward @music")
    await dispatch(bot, "autoforward off")

    assert (await bot.db.list_auto_forwards(DEST))[0].enabled is False
    message = FakeMessage(id=41, message="nope")
    assert await forwarding.maybe_auto_forward(bot, MirrorEvent(SOURCE_ID, message)) is False


async def test_loop_guard_stops_a_two_way_cycle(bot) -> None:
    register_source(bot)
    # This chat mirrors the channel...
    await dispatch(bot, "autoforward @music")
    # ...and the channel mirrors this chat back.
    await bot.db.set_auto_forward(SOURCE_ID, "id:dest", source_id=DEST)
    bot.invalidate_auto_forward_cache()

    original = FakeMessage(id=51, message="ping")
    assert await forwarding.maybe_auto_forward(bot, MirrorEvent(SOURCE_ID, original)) is True
    assert len(bot.client.forwarded) == 1
    ((dest, forwarded_id),) = bot._auto_forward_hops.keys()
    assert dest == DEST

    bounce = FakeMessage(id=forwarded_id, message="ping", chat_id=DEST)
    assert await forwarding.maybe_auto_forward(bot, MirrorEvent(DEST, bounce)) is False
    assert len(bot.client.forwarded) == 1


async def test_the_message_handler_runs_the_mirror_even_without_text(config, db) -> None:
    """The hook lives in SelfBot._handle_message, before the "no text" return."""
    from conftest import FakeClient
    from selfbot.bot import SelfBot

    client = FakeClient()
    bot = SelfBot(config, client=client, db=db)
    bot.me = SimpleNamespace(id=999)
    register_source(bot)
    await bot.db.set_auto_forward(DEST, "@music", source_id=SOURCE_ID, media_types=("audio",))
    bot.invalidate_auto_forward_cache()

    class EventWithMessage(FakeEvent):
        """FakeEvent.message is a property; this one carries a real payload."""

        def __init__(self, message: FakeMessage, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._payload = message

        @property
        def message(self) -> FakeMessage:
            return self._payload

    event = EventWithMessage(
        FakeMessage(id=77, audio=object(), document=object(), media=object()),
        raw_text="",
        chat_id=SOURCE_ID,
        out=False,
        sender_id=1,
    )
    await bot._handle_message(event)

    assert len(client.forwarded) == 1
    assert client.forwarded[0]["chat_id"] == DEST


async def test_rules_are_cached_until_a_command_invalidates_them(bot) -> None:
    register_source(bot)

    assert await forwarding.mirror_rules(bot) == []
    await dispatch(bot, "autoforward @music -music")

    cached = await forwarding.mirror_rules(bot)
    assert len(cached) == 1
    assert bot._auto_forward_cache is cached

    # A direct DB change stays invisible until the cache is dropped.
    await bot.db.set_auto_forward(111, "id:x", source_id=111)
    assert len(await forwarding.mirror_rules(bot)) == 1
    bot.invalidate_auto_forward_cache()
    assert len(await forwarding.mirror_rules(bot)) == 2


# ---------------------------------------------------------------------------
# Managing rules
# ---------------------------------------------------------------------------


async def test_list_shows_this_chat_then_every_chat(bot) -> None:
    register_source(bot)
    await dispatch(bot, "autoforward @music -music -hide")

    event = await dispatch(bot, "autoforward list")
    assert "@music" in reply_of(event)
    assert "hidden" in reply_of(event)

    await bot.db.set_auto_forward(-100777, "@music", source_id=SOURCE_ID)
    event = await dispatch(bot, "autoforward list -all")
    assert "all chats" in reply_of(event)
    assert "-100777" in reply_of(event)


async def test_off_on_and_remove_target_one_source(bot) -> None:
    register_source(bot)
    await dispatch(bot, "autoforward @music")
    await dispatch(bot, "autoforward @music -video")

    event = await dispatch(bot, "autoforward off @music")
    assert "Paused" in reply_of(event)
    assert not any(rule.enabled for rule in await bot.db.list_auto_forwards(DEST))

    event = await dispatch(bot, "autoforward on @music")
    assert "Resumed" in reply_of(event)
    assert all(rule.enabled for rule in await bot.db.list_auto_forwards(DEST))

    event = await dispatch(bot, "autoforward remove @music")
    assert "Removed" in reply_of(event)
    assert await bot.db.list_auto_forwards(DEST) == []


async def test_off_without_a_source_pauses_every_rule(bot) -> None:
    register_source(bot)
    register_source(bot, "@videos", chat_id=556, username="videos", title="Video Channel")
    await dispatch(bot, "autoforward @music")
    await dispatch(bot, "autoforward @videos -video")

    event = await dispatch(bot, "autoforward off")

    assert "Paused 2" in reply_of(event)
    assert all(not rule.enabled for rule in await bot.db.list_auto_forwards(DEST))


async def test_remove_without_a_source_requires_all(bot) -> None:
    register_source(bot)
    await dispatch(bot, "autoforward @music")

    event = await dispatch(bot, "autoforward remove")
    assert "remove -all" in reply_of(event)

    event = await dispatch(bot, "autoforward remove -all")
    assert "Removed" in reply_of(event)
    assert await bot.db.list_auto_forwards(DEST) == []


async def test_unknown_subcommand_target_is_ignored(bot) -> None:
    register_source(bot)
    await dispatch(bot, "autoforward @music")

    event = await dispatch(bot, "autoforward off @nothing")

    assert "No rule for" in reply_of(event)
    assert (await bot.db.list_auto_forwards(DEST))[0].enabled is True


async def test_actions_without_rules_say_so(bot) -> None:
    event = await dispatch(bot, "autoforward list")
    assert "No auto-forward rules" in reply_of(event)

    event = await dispatch(bot, "autoforward off")
    assert "No auto-forward rules" in reply_of(event)


# ---------------------------------------------------------------------------
# Message inspection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "kind"),
    [
        ({"message": "hello"}, "text"),
        ({"message": "cutie", "photo": object()}, "photo"),
        ({"video": object()}, "video"),
        ({"voice": object(), "document": object()}, "voice"),
        ({"video_note": object(), "document": object()}, "video_note"),
        ({"audio": object(), "document": object()}, "audio"),
        ({"document": object()}, "file"),
        ({"sticker": object(), "document": object()}, "sticker"),
        ({"gif": object(), "document": object()}, "gif"),
        ({"message": "https://x.dev", "web_preview": object()}, "link"),
    ],
)
def test_classify_message(kwargs: dict[str, Any], kind: str) -> None:
    assert classify_message(FakeMessage(**kwargs)) == kind


@pytest.mark.parametrize(
    ("types", "kwargs", "expected"),
    [
        ((), {"message": "hi"}, True),
        (("text",), {"message": "hi"}, True),
        (("photo",), {"message": "hi"}, False),
        (("audio", "video"), {"audio": object(), "document": object()}, True),
        (("audio", "video"), {"voice": object(), "document": object()}, False),
    ],
)
def test_matches_types(types: tuple[str, ...], kwargs: dict[str, Any], expected: bool) -> None:
    rule = SimpleNamespace(media_types=types)
    assert matches_types(rule, FakeMessage(**kwargs)) is expected


def test_types_label_is_human_readable() -> None:
    assert forwarding.types_label(()) == "every message"
    assert forwarding.types_label(("audio", "video_note")) == "music, video message"
