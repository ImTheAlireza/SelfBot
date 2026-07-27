"""The online announcement.

v1 posted a startup message; the v2 rewrite dropped it, so a supervisor
restart gave no sign the bot had come back. These lock the behaviour in.
"""

from __future__ import annotations

import dataclasses

import pytest

from selfbot.bot import SelfBot


class RecordingClient:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[object, str]] = []
        self.fail = fail

    async def send_message(self, entity, text, **_kwargs):
        if self.fail:
            raise RuntimeError("chat not found")
        self.sent.append((entity, text))


class FakeMe:
    id = 1038991065
    first_name = "Alireza"
    last_name = None
    username = "trrauuma"


def build(config, notify: str, *, fail: bool = False) -> tuple[SelfBot, RecordingClient]:
    client = RecordingClient(fail=fail)
    bot = SelfBot(dataclasses.replace(config, startup_notify=notify), client=client)
    bot.me = FakeMe()
    return bot, client


@pytest.mark.asyncio
async def test_announces_to_saved_messages_by_default(config):
    bot, client = build(config, "me")
    await bot._announce_online(0, 0)

    assert len(client.sent) == 1
    entity, text = client.sent[0]
    assert entity == "me"
    assert "SelfBot online" in text


@pytest.mark.asyncio
async def test_announcement_reports_real_state(config):
    bot, client = build(config, "me")
    await bot._announce_online(0, 0)

    text = client.sent[0][1]
    assert "@trrauuma" in text
    assert f"{len(bot.registry)} commands" in text
    assert "sqlite" in text
    assert "help" in text


@pytest.mark.asyncio
async def test_timer_counts_are_included_when_present(config):
    bot, client = build(config, "me")
    await bot._announce_online(3, 2)

    text = client.sent[0][1]
    assert "3 resumed" in text
    assert "2 expired" in text


@pytest.mark.asyncio
async def test_timer_line_is_omitted_when_there_are_none(config):
    """Don't report 'Timers: ' with nothing after it."""
    bot, client = build(config, "me")
    await bot._announce_online(0, 0)

    assert "Timers" not in client.sent[0][1]


@pytest.mark.asyncio
async def test_off_stays_silent(config):
    bot, client = build(config, "off")
    await bot._announce_online(1, 1)

    assert client.sent == []


@pytest.mark.asyncio
async def test_numeric_target_is_sent_as_int(config):
    """Telethon needs a real int for a channel, not the string from .env."""
    bot, client = build(config, "-1001234567890")
    await bot._announce_online(0, 0)

    entity = client.sent[0][0]
    assert entity == -1001234567890
    assert isinstance(entity, int)


@pytest.mark.asyncio
async def test_invalid_target_falls_back_to_saved_messages(config):
    bot, client = build(config, "not-a-chat-id")
    await bot._announce_online(0, 0)

    assert client.sent[0][0] == "me"


@pytest.mark.asyncio
async def test_send_failure_never_blocks_startup(config):
    """A greeting that cannot be delivered must not stop the bot running."""
    bot, _client = build(config, "me", fail=True)
    await bot._announce_online(0, 0)  # must not raise


@pytest.mark.asyncio
async def test_missing_account_details_do_not_crash(config):
    bot, client = build(config, "me")
    bot.me = None
    await bot._announce_online(0, 0)

    assert client.sent, "should still announce"


@pytest.mark.asyncio
async def test_restore_timers_returns_counts(db, config):
    """The announcement's numbers come from here, so they must be real."""
    from datetime import timedelta

    from selfbot.db import Timer, utcnow
    from selfbot.plugins.timers import restore_timers

    bot, _client = build(config, "off")
    bot.db = db

    await db.create_timer(
        Timer(
            hash="live0001", user_id=1, chat_id=-1, title="live",
            duration_seconds=600, end_time=utcnow() + timedelta(minutes=10),
        )
    )
    await db.create_timer(
        Timer(
            hash="dead0001", user_id=1, chat_id=-1, title="dead",
            duration_seconds=600, end_time=utcnow() - timedelta(minutes=10),
        )
    )

    restored, expired = await restore_timers(bot)
    assert (restored, expired) == (1, 1)

    for task in bot.timer_tasks.values():
        task.cancel()
