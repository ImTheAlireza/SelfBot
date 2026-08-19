"""Tests for the automated challenge member tagging plugin."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

import pytest
from telethon import types

from conftest import SUDO_ID, FakeEvent, FakeMessage
from selfbot.plugins.challenge import (
    extract_mentions,
    format_user_mention,
    is_active_user,
)


class DummyUser:
    def __init__(
        self,
        user_id: int = 100,
        first_name: str = "Ali",
        username: str | None = None,
        status: Any = None,
        bot: bool = False,
        deleted: bool = False,
    ) -> None:
        self.id = user_id
        self.first_name = first_name
        self.username = username
        self.status = status
        self.bot = bot
        self.deleted = deleted


# ---------------------------------------------------------------------------
# Filters and helpers
# ---------------------------------------------------------------------------


def test_is_active_user_filters_properly():
    now = datetime.now(timezone.utc)

    # Active statuses
    assert is_active_user(DummyUser(status=types.UserStatusOnline(expires=now)), now)
    assert is_active_user(DummyUser(status=types.UserStatusRecently()), now)
    assert is_active_user(
        DummyUser(status=types.UserStatusOffline(was_online=now - timedelta(days=1))), now
    )

    # Inactive / ghost statuses
    assert not is_active_user(
        DummyUser(status=types.UserStatusOffline(was_online=now - timedelta(days=5))), now
    )
    assert not is_active_user(DummyUser(status=types.UserStatusLastMonth()), now)
    assert not is_active_user(DummyUser(status=types.UserStatusLastWeek()), now)
    assert not is_active_user(DummyUser(status=types.UserStatusEmpty()), now)
    assert not is_active_user(DummyUser(status=None), now)

    # Bots and deleted accounts
    assert not is_active_user(DummyUser(status=types.UserStatusOnline(expires=now), bot=True), now)
    assert not is_active_user(
        DummyUser(status=types.UserStatusOnline(expires=now), deleted=True), now
    )


def test_format_user_mention():
    assert format_user_mention(DummyUser(username="alireza")) == "@alireza"
    assert (
        format_user_mention(DummyUser(user_id=123, first_name="Sara", username=None))
        == "[Sara](tg://user?id=123)"
    )


def test_extract_mentions_from_message_entities():
    class DummyMsg:
        text = "@user1 and @user2 and [Sara](tg://user?id=555)"
        message = text
        entities: ClassVar[list[Any]] = [
            types.MessageEntityMention(offset=0, length=6),
            types.MessageEntityMention(offset=11, length=6),
            types.MessageEntityMentionName(offset=22, length=4, user_id=999),
            types.MessageEntityTextUrl(offset=22, length=4, url="tg://user?id=555"),
        ]

    uids, unames = extract_mentions(DummyMsg())
    assert uids == {555, 999}
    assert unames == {"user1", "user2"}


def test_extract_mentions_from_plain_text_without_entities():
    """Messages like in real group chats often contain plain text @usernames."""
    class DummyPlainMsg:
        raw_text = "@hendona\n@reyhaneh362 @mina_saadatt\n[Ali](tg://user?id=789)"
        entities: ClassVar[list[Any]] = []

    uids, unames = extract_mentions(DummyPlainMsg())
    assert uids == {789}
    assert unames == {"hendona", "reyhaneh362", "mina_saadatt"}


# ---------------------------------------------------------------------------
# Commands: startchallenge, stopchallenge, challengestatus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startchallenge_requires_reply(bot, registry):
    event = FakeEvent(raw_text="startchallenge", is_reply=False)
    await registry.dispatch(bot, event, "startchallenge")
    assert any("Reply to a message" in r or "Reply to the challenge" in r for r in event.replies)


@pytest.mark.asyncio
async def test_startchallenge_and_stopchallenge_flow(bot, registry):
    chat_id = -100123
    challenge_msg = FakeMessage(id=50, text="Challenge post")
    now = datetime.now(timezone.utc)

    # Setup fake participants
    candidates = [
        DummyUser(user_id=1, first_name="User1", status=types.UserStatusRecently()),
        DummyUser(user_id=2, first_name="User2", username="user2", status=types.UserStatusOnline(expires=now)),
        DummyUser(user_id=3, first_name="BotUser", bot=True),
        DummyUser(user_id=4, first_name="OldUser", status=types.UserStatusLastMonth()),
    ]

    async def iter_participants(_chat_id):
        for u in candidates:
            yield u

    async def get_participants(_chat_id, **_k):
        return [DummyUser(user_id=SUDO_ID, first_name="Owner")]

    async def iter_messages(_chat_id, **_k):
        if False:
            yield None

    bot.client.iter_participants = iter_participants
    bot.client.get_participants = get_participants
    bot.client.iter_messages = iter_messages
    bot.challenge_tasks = {}

    # Test default: 1 tag per message
    event_default = FakeEvent(
        raw_text="startchallenge",
        chat_id=chat_id,
        is_reply=True,
        reply_message=challenge_msg,
    )
    await registry.dispatch(bot, event_default, "startchallenge")
    assert chat_id in bot.challenge_tasks
    assert bot.challenge_tasks[chat_id].batch_size == 1
    # Stop it
    await registry.dispatch(bot, FakeEvent(raw_text="stopchallenge", chat_id=chat_id), "stopchallenge")
    assert chat_id not in bot.challenge_tasks

    # Test custom: startchallenge 2 10-20
    event = FakeEvent(
        raw_text="startchallenge 2 10-20",
        chat_id=chat_id,
        is_reply=True,
        reply_message=challenge_msg,
    )

    await registry.dispatch(bot, event, "startchallenge 2 10-20")

    # Verify session was created
    assert chat_id in bot.challenge_tasks
    state = bot.challenge_tasks[chat_id]
    assert state.challenge_msg_id == 50
    assert state.batch_size == 2
    assert state.min_delay == 10.0
    assert state.max_delay == 20.0
    assert state.total_candidates == 2  # Only User1 and User2 qualify

    # Check status
    status_event = FakeEvent(raw_text="challengestatus", chat_id=chat_id)
    await registry.dispatch(bot, status_event, "challengestatus")
    assert any("Challenge Tagging Status" in r for r in status_event.replies)

    # Stop session
    stop_event = FakeEvent(raw_text="stopchallenge", chat_id=chat_id)
    await registry.dispatch(bot, stop_event, "stopchallenge")

    assert chat_id not in bot.challenge_tasks
    assert any("Challenge tagging stopped" in r for r in stop_event.replies)
    assert any("All session data cleared from memory" in r for r in stop_event.replies)


@pytest.mark.asyncio
async def test_startchallenge_scans_previous_tags_from_everyone(bot, registry):
    """Scans all mentions since challenge msg from self and others to prevent duplicates."""
    chat_id = -100999
    challenge_msg = FakeMessage(id=100, text="Challenge post")

    candidates = [
        DummyUser(user_id=1, first_name="User1", username="hendona", status=types.UserStatusRecently()),
        DummyUser(user_id=2, first_name="User2", username="reyhaneh362", status=types.UserStatusRecently()),
        DummyUser(user_id=3, first_name="User3", username="mina_saadatt", status=types.UserStatusRecently()),
        DummyUser(user_id=4, first_name="NewUser", username="newuser", status=types.UserStatusRecently()),
    ]

    # Prior messages from HOSein, Abolfazl, or ourselves
    past_messages = [
        FakeMessage(id=101, text="@hendona"),
        FakeMessage(id=102, text="@reyhaneh362 @mina_saadatt"),
    ]

    async def iter_participants(_chat_id):
        for u in candidates:
            yield u

    async def get_participants(_chat_id, **_k):
        return [DummyUser(user_id=SUDO_ID, first_name="Owner")]

    async def iter_messages(_chat_id, **_k):
        for m in past_messages:
            yield m

    bot.client.iter_participants = iter_participants
    bot.client.get_participants = get_participants
    bot.client.iter_messages = iter_messages
    bot.challenge_tasks = {}

    event = FakeEvent(
        raw_text="startchallenge",
        chat_id=chat_id,
        is_reply=True,
        reply_message=challenge_msg,
    )
    await registry.dispatch(bot, event, "startchallenge")

    assert chat_id in bot.challenge_tasks
    state = bot.challenge_tasks[chat_id]
    # hendona, reyhaneh362, mina_saadatt were already tagged in chat -> only newuser remains!
    assert state.total_candidates == 1
    assert state.candidates[0].username == "newuser"
    assert state.tagged_usernames >= {"hendona", "reyhaneh362", "mina_saadatt"}

    await registry.dispatch(bot, FakeEvent(raw_text="stopchallenge", chat_id=chat_id), "stopchallenge")


@pytest.mark.asyncio
async def test_stopchallenge_when_inactive(bot, registry):
    event = FakeEvent(raw_text="stopchallenge", chat_id=-999)
    await registry.dispatch(bot, event, "stopchallenge")
    assert any("No challenge tagging session is active" in r for r in event.replies)
