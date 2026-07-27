"""Behavioural tests for the bundled command plugins."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import SUDO_ID, FakeEvent, FakeMessage
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
async def test_self_restart_without_supervisor_is_graceful(bot, registry):
    """No SUPERVISOR_PROCESS configured -> actionable hint, not a crash."""
    event = FakeEvent(raw_text="self restart")
    await registry.dispatch(bot, event, "self restart")
    assert any("SUPERVISOR_PROCESS" in r for r in event.replies)


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
# AI (disabled provider path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gpt_reports_disabled_provider(bot, registry, config):
    from selfbot.services.ai import build_provider

    bot.ai = build_provider(config.ai)
    event = FakeEvent(raw_text="gpt hello there")
    await registry.dispatch(bot, event, event.raw_text)
    assert any("not configured" in r for r in event.replies)


@pytest.mark.asyncio
async def test_imagine_reports_disabled_provider(bot, registry, config):
    from selfbot.services.ai import build_image_provider

    bot.image_ai = build_image_provider(config.image)
    event = FakeEvent(raw_text="imagine a cat")
    await registry.dispatch(bot, event, event.raw_text)
    assert any("not configured" in r for r in event.replies)


@pytest.mark.asyncio
async def test_tts_reports_disabled_provider(bot, registry):
    replied = FakeMessage()
    replied.raw_text = "read me"
    event = FakeEvent(raw_text="tts", is_reply=True, reply_message=replied)
    await registry.dispatch(bot, event, "tts")
    assert any("not configured" in r for r in event.replies)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qr_rejects_unknown_colour(bot, registry):
    event = FakeEvent(raw_text="qr hello --fg chartreuse")
    await registry.dispatch(bot, event, "qr hello --fg chartreuse")
    assert any("Unknown colour" in r for r in event.replies)


@pytest.mark.asyncio
async def test_qr_rejects_out_of_range_size(bot, registry):
    event = FakeEvent(raw_text="qr hello --size 99")
    await registry.dispatch(bot, event, "qr hello --size 99")
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
    assert _weather_icon(0) == "☀️"
    assert _weather_icon(95) == "⛈"
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


def test_sticker_rendering_handles_long_text(tmp_path: Path):
    from selfbot.plugins.stickers import render_sticker

    output = tmp_path / "long.webp"
    render_sticker("word " * 50, output)
    assert output.is_file()


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
