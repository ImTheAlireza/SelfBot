"""Database layer: schema, CRUD and UTC correctness."""

from __future__ import annotations

from datetime import timedelta

import pytest

from selfbot.db import Database, Timer, utcnow


@pytest.mark.asyncio
async def test_sudo_is_seeded_and_idempotent(db):
    assert await db.get_role(4242) == "sudo"
    await db.ensure_sudo(4242)
    users = await db.list_users()
    assert len([u for u in users if u["id"] == 4242]) == 1


@pytest.mark.asyncio
async def test_admin_lifecycle(db):
    await db.add_admin(555, "Alice")
    assert await db.is_known_user(555)
    assert await db.get_role(555) == "admin"

    # Re-adding updates rather than duplicating.
    await db.add_admin(555, "Alice Renamed")
    users = [u for u in await db.list_users() if u["id"] == 555]
    assert len(users) == 1
    assert users[0]["username"] == "Alice Renamed"

    assert await db.remove_admin(555) == 1
    assert not await db.is_known_user(555)


@pytest.mark.asyncio
async def test_sudo_cannot_be_removed_as_admin(db):
    assert await db.remove_admin(4242) == 0
    assert await db.get_role(4242) == "sudo"


@pytest.mark.asyncio
async def test_quick_reply_crud(db):
    await db.set_quick_reply(1, "email", "me@example.com")
    assert await db.get_quick_reply(1, "email") == "me@example.com"

    await db.set_quick_reply(1, "email", "new@example.com")
    assert await db.get_quick_reply(1, "email") == "new@example.com"

    replies = await db.list_quick_replies(1)
    assert len(replies) == 1
    assert replies[0].alias == "email"

    assert await db.delete_quick_reply(1, "email") == 1
    assert await db.get_quick_reply(1, "email") is None


@pytest.mark.asyncio
async def test_quick_replies_are_per_user(db):
    await db.set_quick_reply(1, "sig", "user one")
    await db.set_quick_reply(2, "sig", "user two")
    assert await db.get_quick_reply(1, "sig") == "user one"
    assert await db.get_quick_reply(2, "sig") == "user two"


@pytest.mark.asyncio
async def test_reaction_crud(db):
    await db.set_reaction("channelone", "🔥")
    assert await db.list_reactions() == {"channelone": "🔥"}

    await db.set_reaction("channelone", "❤️")
    assert (await db.list_reactions())["channelone"] == "❤️"

    assert await db.delete_reaction("channelone") == 1
    assert await db.list_reactions() == {}


@pytest.mark.asyncio
async def test_timer_roundtrip_preserves_utc(db):
    """Timer end times must survive the DB round trip as aware UTC."""
    end = utcnow() + timedelta(hours=2)
    timer = Timer(
        hash="abcd1234",
        user_id=1,
        chat_id=-100,
        title="test",
        duration_seconds=7200,
        end_time=end,
        message_id=42,
    )
    await db.create_timer(timer)

    loaded = await db.get_timer("abcd1234")
    assert loaded is not None
    assert loaded.end_time.tzinfo is not None
    assert abs((loaded.end_time - end).total_seconds()) < 2
    assert 7100 < loaded.remaining_seconds <= 7200
    assert loaded.message_id == 42


@pytest.mark.asyncio
async def test_expired_timer_reports_zero_remaining(db):
    timer = Timer(
        hash="expired1",
        user_id=1,
        chat_id=-100,
        title="past",
        duration_seconds=60,
        end_time=utcnow() - timedelta(hours=1),
    )
    await db.create_timer(timer)
    loaded = await db.get_timer("expired1")
    assert loaded.remaining_seconds == 0


@pytest.mark.asyncio
async def test_active_timer_filtering(db):
    for index in range(3):
        await db.create_timer(
            Timer(
                hash=f"hash{index:04d}",
                user_id=1 if index < 2 else 2,
                chat_id=-100,
                title=f"t{index}",
                duration_seconds=600,
                end_time=utcnow() + timedelta(minutes=10),
            )
        )

    assert len(await db.list_active_timers()) == 3
    assert len(await db.list_active_timers(user_id=1)) == 2
    assert len(await db.list_active_timers(user_id=2)) == 1

    await db.deactivate_timer("hash0000")
    assert len(await db.list_active_timers()) == 2


@pytest.mark.asyncio
async def test_sticker_pack_crud(db):
    await db.add_sticker_pack("mypack", "My Pack", 1)
    pack = await db.get_sticker_pack("mypack")
    assert pack is not None
    assert pack.title == "My Pack"
    assert pack.owner_id == 1

    assert len(await db.list_sticker_packs(owner_id=1)) == 1
    assert len(await db.list_sticker_packs(owner_id=999)) == 0

    assert await db.delete_sticker_pack("mypack") == 1
    assert await db.get_sticker_pack("mypack") is None


@pytest.mark.asyncio
async def test_parameters_are_bound_not_interpolated(db):
    """SQL injection attempt must be stored as literal data."""
    nasty = "'; DROP TABLE users; --"
    await db.set_quick_reply(1, "evil", nasty)
    assert await db.get_quick_reply(1, "evil") == nasty
    # users table must still exist
    assert await db.get_role(4242) == "sudo"


@pytest.mark.asyncio
async def test_unknown_database_url_is_rejected():
    from selfbot.errors import ConfigError

    with pytest.raises(ConfigError, match="Unsupported DATABASE_URL"):
        Database("postgres://user@host/db")
