"""Behavioural tests for the bundled command plugins."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from conftest import SUDO_ID, FakeClient, FakeEvent, FakeMessage
from selfbot.bot import SelfBot
from selfbot.db import Timer, utcnow
from selfbot.plugins.timers import render_finished, render_timer
from selfbot.plugins.utilities import _scrape_tgju, _weather_icon

# ---------------------------------------------------------------------------
# help / core
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_lists_every_category(bot, registry):
    event = FakeEvent(raw_text="help")
    await registry.dispatch(bot, event, "help")
    output = " ".join(event.replies)
    for category in ("Core", "Files", "Timers", "Utilities"):
        assert category in output


@pytest.mark.asyncio
async def test_help_for_single_command(bot, registry):
    event = FakeEvent(raw_text="help settimer")
    await registry.dispatch(bot, event, "help settimer")
    output = " ".join(event.replies)
    assert "settimer" in output
    assert "Usage" in output


@pytest.mark.asyncio
async def test_whoami_reports_ids(bot, registry):
    event = FakeEvent(raw_text="whoami", sender_id=SUDO_ID, chat_id=-4242)
    await registry.dispatch(bot, event, "whoami")
    output = " ".join(event.replies)
    assert str(SUDO_ID) in output
    assert "-4242" in output


@pytest.mark.asyncio
async def test_status_reports_runtime_state(bot, registry):
    event = FakeEvent(raw_text="status")
    await registry.dispatch(bot, event, "status")
    output = " ".join(event.replies)
    assert "Uptime" in output
    assert "sqlite" in output


@pytest.mark.asyncio
async def test_self_off_then_on(bot, registry):
    event = FakeEvent(raw_text="self off")
    await registry.dispatch(bot, event, "self off")
    assert bot.active is False

    event = FakeEvent(raw_text="self on")
    await registry.dispatch(bot, event, "self on")
    assert bot.active is True


@pytest.mark.asyncio
async def test_self_restart_without_supervisor_reexecs_gracefully(bot, registry):
    """No supervisorctl available -> replace the current Python process."""
    event = FakeEvent(raw_text="self restart")
    await registry.dispatch(bot, event, "self restart")
    assert any("Restarting" in r for r in event.replies)


@pytest.mark.asyncio
async def test_self_diag_runs_without_configuration(bot, registry):
    """`self diag` must work even when nothing is set up — that is its job."""
    event = FakeEvent(raw_text="self diag")
    await registry.dispatch(bot, event, "self diag")
    output = " ".join(event.replies)
    assert "Supervisor diagnostics" in output
    assert "SUPERVISOR_PROCESS" in output


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adminlist_shows_owner(bot, registry):
    event = FakeEvent(raw_text="adminlist")
    await registry.dispatch(bot, event, "adminlist")
    output = " ".join(event.replies)
    assert str(SUDO_ID) in output
    assert "sudo" in output


@pytest.mark.asyncio
async def test_remadmin_rejects_non_numeric(bot, registry):
    event = FakeEvent(raw_text="remadmin notanumber")
    await registry.dispatch(bot, event, "remadmin notanumber")
    assert any("numeric" in r.lower() for r in event.replies)


# ---------------------------------------------------------------------------
# Message deletion
# ---------------------------------------------------------------------------


def _delete_message(message_id: int, **media):
    values = {"id": message_id, **media}
    return type("DeleteMessage", (), values)()


def test_del_is_the_only_deletion_command(registry):
    assert registry.get("del") is not None
    assert registry.get("purge") is None


@pytest.mark.asyncio
async def test_del_count_defaults_to_everyone_in_current_chat(bot, registry):
    iterator_calls = []

    async def iter_messages(chat_id, *, limit, from_user):
        iterator_calls.append((chat_id, limit, from_user))
        for message_id in (30, 29, 28):
            yield _delete_message(message_id)

    bot.client.iter_messages = iter_messages
    event = FakeEvent(raw_text="del 3", chat_id=-777)

    await registry.dispatch(bot, event, event.raw_text)

    assert iterator_calls == [(-777, 3, None)]
    assert bot.client.deleted == [(-777, [30, 29, 28])]
    assert bot.confirm_prompts and "from everyone in this chat" in bot.confirm_prompts[0]


@pytest.mark.asyncio
async def test_del_count_me_targets_only_own_messages_in_current_chat(bot, registry):
    iterator_calls = []

    async def iter_messages(chat_id, *, limit, from_user):
        iterator_calls.append((chat_id, limit, from_user))
        yield _delete_message(12)
        yield _delete_message(11)

    bot.client.iter_messages = iter_messages
    event = FakeEvent(raw_text="del 2 -me", chat_id=-888)

    await registry.dispatch(bot, event, event.raw_text)

    assert iterator_calls == [(-888, 2, "me")]
    assert bot.client.deleted == [(-888, [12, 11])]
    assert bot.confirm_prompts == []


@pytest.mark.asyncio
async def test_del_type_me_filters_only_own_matching_media(bot, registry):
    iterator_calls = []

    async def iter_messages(chat_id, *, limit, from_user):
        iterator_calls.append((chat_id, limit, from_user))
        yield _delete_message(3, photo=object())
        yield _delete_message(2, photo=None)
        yield _delete_message(1, photo=object())

    bot.client.iter_messages = iter_messages
    event = FakeEvent(raw_text="del photos -me", chat_id=-999)

    await registry.dispatch(bot, event, event.raw_text)

    assert iterator_calls == [(-999, None, "me")]
    assert bot.client.deleted == [(-999, [3, 1])]


@pytest.mark.asyncio
async def test_del_all_scans_full_current_chat_and_batches_deletes(bot, registry):
    iterator_calls = []

    async def iter_messages(chat_id, *, limit, from_user):
        iterator_calls.append((chat_id, limit, from_user))
        for message_id in range(205, 0, -1):
            yield _delete_message(message_id)

    bot.client.iter_messages = iter_messages
    event = FakeEvent(raw_text="del all", chat_id=-1234)

    await registry.dispatch(bot, event, event.raw_text)

    assert iterator_calls == [(-1234, None, None)]
    assert [chat_id for chat_id, _ids in bot.client.deleted] == [-1234, -1234, -1234]
    assert [len(ids) for _chat_id, ids in bot.client.deleted] == [100, 100, 5]
    deleted_ids = [
        message_id
        for _chat_id, ids in bot.client.deleted
        for message_id in ids
    ]
    assert deleted_ids == list(range(205, 0, -1))


@pytest.mark.asyncio
async def test_del_rejects_me_flag_before_target(bot, registry):
    event = FakeEvent(raw_text="del -me photos")

    await registry.dispatch(bot, event, event.raw_text)

    assert any("final argument" in reply for reply in event.replies)
    assert bot.client.deleted == []


# ---------------------------------------------------------------------------
# User info
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_info_sends_profile_picture_as_a_named_photo(bot, registry):
    entity = type(
        "Entity",
        (),
        {
            "id": 123,
            "first_name": "Alice",
            "last_name": "Example",
            "username": "alice",
            "premium": False,
            "bot": False,
            "photo": object(),
        },
    )()
    bot.client.entities["@alice"] = entity

    async def download_profile_photo(target, *, file, download_big):
        assert target is entity
        assert file is bytes
        assert download_big is True
        return b"\xff\xd8\xff\xe0fake-jpeg"

    bot.client.download_profile_photo = download_profile_photo
    event = FakeEvent(raw_text="info @alice")

    await registry.dispatch(bot, event, event.raw_text)

    assert len(bot.client.sent_files) == 1
    upload = bot.client.sent_files[0]
    uploaded_file = upload["file"]
    assert isinstance(uploaded_file, BytesIO)
    assert uploaded_file.name == "profile_123.jpg"
    assert uploaded_file.closed
    assert upload["force_document"] is False
    assert "Alice Example" in upload["caption"]


# ---------------------------------------------------------------------------
# Quick replies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qreply_set_list_and_remove(bot, registry, db):
    event = FakeEvent(raw_text="qreply set email me@example.com")
    await registry.dispatch(bot, event, event.raw_text)
    assert await db.get_quick_reply(SUDO_ID, "email") == "me@example.com"

    event = FakeEvent(raw_text="qreply list")
    await registry.dispatch(bot, event, event.raw_text)
    assert any("email" in r for r in event.replies)

    event = FakeEvent(raw_text="qreply remove email")
    await registry.dispatch(bot, event, event.raw_text)
    assert await db.get_quick_reply(SUDO_ID, "email") is None


@pytest.mark.asyncio
async def test_qreply_preserves_multiword_message(bot, registry, db):
    event = FakeEvent(raw_text="qreply set sig Best regards, Alireza")
    await registry.dispatch(bot, event, event.raw_text)
    assert await db.get_quick_reply(SUDO_ID, "sig") == "Best regards, Alireza"


@pytest.mark.asyncio
async def test_qreply_rejects_non_alphanumeric_alias(bot, registry):
    event = FakeEvent(raw_text="qreply set my-alias text")
    await registry.dispatch(bot, event, event.raw_text)
    assert any("letters and numbers" in r for r in event.replies)


@pytest.mark.asyncio
async def test_qreply_from_replied_message(bot, registry, db):
    replied = FakeMessage(text="captured text")
    replied.raw_text = "captured text"
    event = FakeEvent(
        raw_text="qreply set saved", is_reply=True, reply_message=replied
    )
    await registry.dispatch(bot, event, event.raw_text)
    assert await db.get_quick_reply(SUDO_ID, "saved") == "captured text"


# ---------------------------------------------------------------------------
# Auto replies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setautoreply_list_and_remove(bot, registry, db):
    event = FakeEvent(
        raw_text='setautoreply contain "hello there" "General Kenobi"',
        chat_id=-100,
    )
    await registry.dispatch(bot, event, event.raw_text)

    rules = await db.list_auto_replies(-100)
    assert len(rules) == 1
    assert rules[0].mode == "contain"
    assert rules[0].trigger == "hello there"
    assert rules[0].reply_text == "General Kenobi"
    assert rules[0].reply_condition == "any"
    assert bot.auto_reply_cache_invalidated == [-100]

    event = FakeEvent(raw_text="autoreplylist", chat_id=-100)
    await registry.dispatch(bot, event, event.raw_text)
    assert any("hello there" in r for r in event.replies)
    assert any("any message" in r for r in event.replies)

    event = FakeEvent(
        raw_text='remautoreply contain "hello there"',
        chat_id=-100,
    )
    await registry.dispatch(bot, event, event.raw_text)
    assert await db.list_auto_replies(-100) == []


@pytest.mark.asyncio
async def test_setautoreply_with_nr_flag(bot, registry, db):
    event = FakeEvent(
        raw_text='setautoreply contain "hello" "hi" -nr',
        chat_id=-100,
    )
    await registry.dispatch(bot, event, event.raw_text)

    rules = await db.list_auto_replies(-100)
    assert len(rules) == 1
    assert rules[0].reply_condition == "nr"
    assert any("non-reply only" in r for r in event.replies)


@pytest.mark.asyncio
async def test_setautoreply_with_sr_flag(bot, registry, db):
    event = FakeEvent(
        raw_text='setautoreply contain "hello" "hi" -sr',
        chat_id=-100,
    )
    await registry.dispatch(bot, event, event.raw_text)

    rules = await db.list_auto_replies(-100)
    assert len(rules) == 1
    assert rules[0].reply_condition == "sr"
    assert any("reply-to-me only" in r for r in event.replies)


@pytest.mark.asyncio
async def test_remautoreply_allchats(bot, registry, db):
    await db.set_auto_reply(-100, "contain", "hello", "hi")
    await db.set_auto_reply(-200, "match", "ping", "pong")

    event = FakeEvent(raw_text="remautoreply -allchats")
    await registry.dispatch(bot, event, event.raw_text)
    assert any("Removed" in r or "2" in r for r in event.replies)
    assert await db.list_all_auto_replies() == []
    assert None in bot.auto_reply_cache_invalidated


@pytest.mark.asyncio
async def test_autoreply_only_triggers_in_the_configured_chat(config, registry, db):
    bot = SelfBot(config, registry=registry, client=FakeClient(), db=db)
    await db.set_auto_reply(-100, "contain", "hello", "hi there")

    event = FakeEvent(raw_text="well hello friend", out=False, sender_id=999, chat_id=-100)
    assert await bot._try_auto_reply(event, event.raw_text)
    assert event.replies == ["hi there"]

    other = FakeEvent(raw_text="well hello friend", out=False, sender_id=999, chat_id=-200)
    assert not await bot._try_auto_reply(other, other.raw_text)
    assert other.replies == []


@pytest.mark.asyncio
async def test_autoreply_match_beats_contain(config, registry, db):
    bot = SelfBot(config, registry=registry, client=FakeClient(), db=db)
    await db.set_auto_reply(-100, "contain", "ping", "generic")
    await db.set_auto_reply(-100, "match", "ping", "exact")

    event = FakeEvent(raw_text="ping", out=False, sender_id=999, chat_id=-100)
    assert await bot._try_auto_reply(event, event.raw_text)
    assert event.replies == ["exact"]


@pytest.mark.asyncio
async def test_autoreply_contain_requires_word_boundaries_for_persian(config, registry, db):
    bot = SelfBot(config, registry=registry, client=FakeClient(), db=db)
    await db.set_auto_reply(-100, "contain", "سلام", "درود")

    embedded = FakeEvent(raw_text="سلامتی", out=False, sender_id=999, chat_id=-100)
    assert not await bot._try_auto_reply(embedded, embedded.raw_text)
    assert embedded.replies == []

    isolated = FakeEvent(raw_text="سلام رفیق", out=False, sender_id=999, chat_id=-100)
    assert await bot._try_auto_reply(isolated, isolated.raw_text)
    assert isolated.replies == ["درود"]


@pytest.mark.asyncio
async def test_autoreply_contain_allows_punctuation_around_word(config, registry, db):
    bot = SelfBot(config, registry=registry, client=FakeClient(), db=db)
    await db.set_auto_reply(-100, "contain", "hello", "hi")

    event = FakeEvent(raw_text="hello!", out=False, sender_id=999, chat_id=-100)
    assert await bot._try_auto_reply(event, event.raw_text)
    assert event.replies == ["hi"]


@pytest.mark.asyncio
async def test_autoreply_nr_skips_reply_messages(config, registry, db):
    bot = SelfBot(config, registry=registry, client=FakeClient(), db=db)
    await db.set_auto_reply(-100, "contain", "hello", "hi", reply_condition="nr")
    bot.me = type("Me", (), {"id": SUDO_ID})()

    # Non-reply message should trigger.
    event = FakeEvent(raw_text="hello", out=False, sender_id=999, chat_id=-100, reply_to=None)
    assert await bot._try_auto_reply(event, event.raw_text)
    assert event.replies == ["hi"]

    # Reply message should be skipped.
    from conftest import FakeReplyTo
    reply_event = FakeEvent(
        raw_text="hello", out=False, sender_id=999, chat_id=-100,
        reply_to=FakeReplyTo(reply_to_msg_id=42),
    )
    assert not await bot._try_auto_reply(reply_event, reply_event.raw_text)
    assert reply_event.replies == []


@pytest.mark.asyncio
async def test_autoreply_sr_only_replies_to_me(config, registry, db):
    bot = SelfBot(config, registry=registry, client=FakeClient(), db=db)
    await db.set_auto_reply(-100, "contain", "hello", "hi", reply_condition="sr")
    bot.me = type("Me", (), {"id": SUDO_ID})()

    # Non-reply message should be skipped.
    event = FakeEvent(raw_text="hello", out=False, sender_id=999, chat_id=-100, reply_to=None)
    assert not await bot._try_auto_reply(event, event.raw_text)
    assert event.replies == []

    # Reply to someone else should be skipped.
    from conftest import FakeReplyTo
    other_reply_event = FakeEvent(
        raw_text="hello", out=False, sender_id=999, chat_id=-100,
        reply_to=FakeReplyTo(reply_to_msg_id=50),
    )
    # FakeClient.get_messages will fail; the bot catches it and skips.
    assert not await bot._try_auto_reply(other_reply_event, other_reply_event.raw_text)


@pytest.mark.asyncio
async def test_autoreply_any_condition_replies_always(config, registry, db):
    bot = SelfBot(config, registry=registry, client=FakeClient(), db=db)
    await db.set_auto_reply(-100, "contain", "hello", "hi", reply_condition="any")
    bot.me = type("Me", (), {"id": SUDO_ID})()

    # Should reply regardless of reply_to status.
    event = FakeEvent(raw_text="hello", out=False, sender_id=999, chat_id=-100, reply_to=None)
    assert await bot._try_auto_reply(event, event.raw_text)
    assert event.replies == ["hi"]

    from conftest import FakeReplyTo
    reply_event = FakeEvent(
        raw_text="hello", out=False, sender_id=999, chat_id=-100,
        reply_to=FakeReplyTo(reply_to_msg_id=42),
    )
    assert await bot._try_auto_reply(reply_event, reply_event.raw_text)
    assert reply_event.replies == ["hi"]


# ---------------------------------------------------------------------------
# Timers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settimer_persists_and_renders(bot, registry, db):
    event = FakeEvent(raw_text="settimer 1:00 tea break")
    await registry.dispatch(bot, event, event.raw_text)

    timers = await db.list_active_timers()
    assert len(timers) == 1
    assert timers[0].title == "tea break"
    assert timers[0].duration_seconds == 60
    assert event.replies and "TEA BREAK" in event.replies[0]

    # Clean up the spawned background task.
    for task in bot.timer_tasks.values():
        task.cancel()


@pytest.mark.asyncio
async def test_settimer_rejects_bad_duration(bot, registry):
    event = FakeEvent(raw_text="settimer banana lunch")
    await registry.dispatch(bot, event, event.raw_text)
    assert any("duration" in r.lower() for r in event.replies)


@pytest.mark.asyncio
async def test_settimer_rejects_excessive_duration(bot, registry):
    event = FakeEvent(raw_text="settimer 400:0:0:0 forever")
    await registry.dispatch(bot, event, event.raw_text)
    assert any("one year" in r for r in event.replies)


@pytest.mark.asyncio
async def test_dismiss_rejects_other_users_timer(bot, registry, db):
    await db.create_timer(
        Timer(
            hash="someone01",
            user_id=98765,
            chat_id=-100,
            title="not yours",
            duration_seconds=600,
            end_time=utcnow().replace(microsecond=0),
        )
    )
    # Non-sudo caller must be refused.
    bot.config = bot.config
    event = FakeEvent(raw_text="dismiss someone01", out=False, sender_id=111)
    await registry.dispatch(bot, event, event.raw_text)
    joined = " ".join(event.replies)
    assert "someone else" in joined or "No active timer" in joined


@pytest.mark.asyncio
async def test_activetimers_when_empty(bot, registry):
    event = FakeEvent(raw_text="activetimers")
    await registry.dispatch(bot, event, event.raw_text)
    assert any("No active timers" in r for r in event.replies)


def test_timer_rendering_includes_controls():
    timer = Timer(
        hash="abcd1234",
        user_id=1,
        chat_id=-1,
        title="focus",
        duration_seconds=600,
        end_time=utcnow(),
    )
    rendered = render_timer(timer, 300)
    assert "FOCUS" in rendered
    assert "dismiss abcd1234" in rendered
    assert "resend abcd1234" in rendered

    finished = render_finished(timer)
    assert "Time's up" in finished


def test_timer_render_survives_zero_duration():
    """A zero-length timer must not divide by zero."""
    timer = Timer(
        hash="zero0000",
        user_id=1,
        chat_id=-1,
        title="instant",
        duration_seconds=0,
        end_time=utcnow(),
    )
    assert render_timer(timer, 0)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_requires_a_file(bot, registry):
    replied = FakeMessage()
    replied.file = None
    event = FakeEvent(raw_text="add", is_reply=True, reply_message=replied)
    await registry.dispatch(bot, event, "add")
    assert any("Reply to a file" in r for r in event.replies)


@pytest.mark.asyncio
async def test_zip_queue_reports_empty(bot, registry):
    event = FakeEvent(raw_text="zipqueue")
    await registry.dispatch(bot, event, "zipqueue")
    assert any("empty" in r.lower() for r in event.replies)


@pytest.mark.asyncio
async def test_zipit_without_queue_errors(bot, registry):
    event = FakeEvent(raw_text="zipit")
    await registry.dispatch(bot, event, "zipit")
    assert any("empty" in r.lower() for r in event.replies)


@pytest.mark.asyncio
async def test_split_rejects_bad_range(bot, registry):
    replied = FakeMessage()
    event = FakeEvent(raw_text="split 5-2", is_reply=True, reply_message=replied)
    await registry.dispatch(bot, event, "split 5-2")
    assert any("Invalid range" in r for r in event.replies)


@pytest.mark.asyncio
async def test_split_rejects_malformed_argument(bot, registry):
    replied = FakeMessage()
    event = FakeEvent(raw_text="split abc", is_reply=True, reply_message=replied)
    await registry.dispatch(bot, event, "split abc")
    assert any("split" in r.lower() for r in event.replies)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qr_rejects_unknown_colour(bot, registry):
    event = FakeEvent(raw_text="qr hello -fg chartreuse")
    await registry.dispatch(bot, event, "qr hello -fg chartreuse")
    assert any("Unknown colour" in r for r in event.replies)


@pytest.mark.asyncio
async def test_qr_rejects_out_of_range_size(bot, registry):
    event = FakeEvent(raw_text="qr hello -size 99")
    await registry.dispatch(bot, event, "qr hello -size 99")
    assert any("between 1 and 40" in r for r in event.replies)


@pytest.mark.asyncio
async def test_qr_generates_an_image(bot, registry):
    event = FakeEvent(raw_text="qr https://example.com")
    await registry.dispatch(bot, event, "qr https://example.com")
    assert bot.client.sent_files, "a QR image should have been sent"


@pytest.mark.asyncio
async def test_topdf_rejects_bad_font_size(bot, registry):
    replied = FakeMessage()
    replied.raw_text = "some text"
    event = FakeEvent(raw_text="topdf 200", is_reply=True, reply_message=replied)
    await registry.dispatch(bot, event, "topdf 200")
    assert any("between 6 and 72" in r for r in event.replies)


@pytest.mark.asyncio
async def test_topdf_creates_a_document(bot, registry):
    replied = FakeMessage()
    replied.raw_text = "Hello world.\n\nSecond paragraph."
    event = FakeEvent(raw_text="topdf en", is_reply=True, reply_message=replied)
    await registry.dispatch(bot, event, "topdf en")
    assert bot.client.sent_files, "a PDF should have been sent"


@pytest.mark.asyncio
async def test_dic_rejects_non_words(bot, registry):
    event = FakeEvent(raw_text="dic 12345")
    await registry.dispatch(bot, event, "dic 12345")
    assert any("Letters" in r for r in event.replies)


def test_weather_icon_lookup():
    assert _weather_icon("113") == "☀️"
    assert _weather_icon("386") == "⛈"
    assert _weather_icon(9999) == "🌡"  # unknown code falls back


def test_tgju_scraper_converts_rial_to_toman():
    html = """
    <table><tbody>
      <tr data-market-nameslug="price_dollar_rl" data-price="1,000,000"></tr>
      <tr data-market-nameslug="geram18" data-price="5,500,000"></tr>
      <tr data-market-nameslug="broken"></tr>
    </tbody></table>
    """
    prices = _scrape_tgju(html)
    assert prices["price_dollar_rl"] == 100_000
    assert prices["geram18"] == 550_000
    assert "broken" not in prices


def test_tgju_scraper_handles_empty_page():
    assert _scrape_tgju("<html></html>") == {}


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaction_lifecycle(bot, registry, db):
    event = FakeEvent(raw_text="setreact @mychannel 🔥")
    await registry.dispatch(bot, event, event.raw_text)
    assert await db.list_reactions() == {"mychannel": "🔥"}
    assert bot.reaction_cache_invalidated

    event = FakeEvent(raw_text="reactlist")
    await registry.dispatch(bot, event, "reactlist")
    assert any("mychannel" in r for r in event.replies)

    event = FakeEvent(raw_text="remreact @mychannel")
    await registry.dispatch(bot, event, event.raw_text)
    assert await db.list_reactions() == {}


@pytest.mark.asyncio
async def test_reactlist_when_empty(bot, registry):
    event = FakeEvent(raw_text="reactlist")
    await registry.dispatch(bot, event, "reactlist")
    assert any("No auto-reactions" in r for r in event.replies)


# ---------------------------------------------------------------------------
# Stickers
# ---------------------------------------------------------------------------


def test_sticker_rendering_produces_valid_webp(tmp_path: Path):
    from PIL import Image

    from selfbot.plugins.stickers import render_sticker

    output = tmp_path / "sticker.webp"
    render_sticker("Hello\nWorld", output, watermark="@test")

    assert output.is_file()
    with Image.open(output) as image:
        assert image.size == (512, 512)


def test_sticker_rendering_handles_rtl(tmp_path: Path):
    from selfbot.plugins.stickers import render_sticker

    output = tmp_path / "rtl.webp"
    render_sticker("سلام دنیا", output)
    assert output.is_file()


def test_sticker_rtl_preparation_avoids_double_bidi_processing():
    from selfbot.plugins.stickers import _prepare_sticker_text
    from selfbot.utils.text import shape_rtl

    logical = "سلام دنیا"

    # libraqm expects logical text and handles shaping/bidi itself.
    native_text, native_direction = _prepare_sticker_text(logical, native_rtl=True)
    assert native_text == logical
    assert native_direction == "rtl"

    # Pillow builds without libraqm need pre-shaped visual-order glyphs.
    fallback_text, fallback_direction = _prepare_sticker_text(
        logical, native_rtl=False
    )
    assert fallback_text == shape_rtl(logical)
    assert fallback_text != logical
    assert fallback_direction is None


def test_sticker_rendering_handles_long_text(tmp_path: Path):
    from selfbot.plugins.stickers import render_sticker

    output = tmp_path / "long.webp"
    render_sticker("word " * 50, output)
    assert output.is_file()


def test_sticker_rendering_scales_short_text_boldly(tmp_path: Path):
    from selfbot.plugins.stickers import render_sticker

    output = tmp_path / "short.webp"
    render_sticker("سلام", output)
    assert output.is_file()


@pytest.mark.asyncio
async def test_stick_replies_to_the_commands_reply_target(bot, registry):
    replied = FakeMessage(id=321, text="original message")
    event = FakeEvent(
        raw_text="stick سلام", is_reply=True, reply_message=replied
    )

    await registry.dispatch(bot, event, event.raw_text)

    assert event.deleted
    assert len(bot.client.sent_files) == 1
    assert bot.client.sent_files[0]["reply_to"] == replied.id


@pytest.mark.asyncio
async def test_stickerpack_help(bot, registry):
    event = FakeEvent(raw_text="stickerpack")
    await registry.dispatch(bot, event, "stickerpack")
    assert any("Sticker packs" in r for r in event.replies)


@pytest.mark.asyncio
async def test_stickerpack_create_requires_title(bot, registry):
    event = FakeEvent(raw_text="stickerpack create mypack")
    await registry.dispatch(bot, event, event.raw_text)
    assert any("Usage" in r for r in event.replies)


@pytest.mark.asyncio
async def test_emojinfo_extracts_custom_emojis(bot, registry):
    from telethon import types

    msg = FakeMessage(id=10, text="🔥 Cool")
    msg.raw_text = "🔥 Cool"  # type: ignore[attr-defined]
    msg.entities = [  # type: ignore[attr-defined]
        types.MessageEntityCustomEmoji(offset=0, length=2, document_id=5368324170671202286)
    ]

    event = FakeEvent(raw_text="emojinfo", is_reply=True, reply_message=msg)
    await registry.dispatch(bot, event, "emojinfo")

    assert any("5368324170671202286" in r for r in event.replies)
    assert any("<tg-emoji" in r for r in event.replies)


@pytest.mark.asyncio
async def test_html_sends_html_message(bot, registry):
    event = FakeEvent(raw_text='html Hello <tg-emoji emoji-id="123">🔥</tg-emoji>')
    await registry.dispatch(bot, event, event.raw_text)

    assert len(bot.client.sent_messages) == 1
    assert 'emoji-id="123"' in bot.client.sent_messages[0][1]
