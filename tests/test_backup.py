"""Tests for the backup/restore commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conftest import FakeEvent, FakeMessage
from selfbot.plugins.backup import _build_dump, _import_dump


@dataclass
class _File:
    name: str


@dataclass
class _Replied:
    document: bool = True
    file: _File | None = None
    _bytes: bytes = b""

    async def download_media(self, file: str) -> str:
        path = Path(file) / self.file.name
        path.write_bytes(self._bytes)
        return str(path)


@dataclass
class _Ctx:
    """Minimal Context-shaped object for direct export/import tests."""

    bot: Any
    event: Any
    db: Any
    args: list[str] = field(default_factory=list)
    raw_args: str = ""


async def test_build_dump_redacts_provider_keys(bot) -> None:
    await bot.db.add_provider("a", "https://a/v1", "sk-secret-1234")
    ctx = _Ctx(bot=bot, event=FakeEvent(), db=bot.db)
    dump = await _build_dump(ctx, include_secrets=False)
    provider = dump["ai_providers"][0]
    assert provider["api_key"].startswith("redacted:")
    assert "secret" not in provider["api_key"]
    assert dump["version"] == 1


async def test_build_dump_includes_secrets(bot) -> None:
    await bot.db.add_provider("a", "https://a/v1", "sk-secret-1234")
    ctx = _Ctx(bot=bot, event=FakeEvent(), db=bot.db)
    dump = await _build_dump(ctx, include_secrets=True)
    assert dump["ai_providers"][0]["api_key"] == "sk-secret-1234"


async def test_import_roundtrip(bot, tmp_path) -> None:
    db = bot.db
    await db.set_quick_reply(bot.config.sudo_user_id, "email", "me@example.com")
    await db.set_reaction("mychannel", "🔥")
    await db.set_welcome_message(-100, "hi there")
    await db.set_welcome_enabled(-100, True)
    await db.add_admin(999, "someone")
    await db.set_setting("ai.default_model", "gpt-x")

    src_ctx = _Ctx(bot=bot, event=FakeEvent(), db=db)
    dump = await _build_dump(src_ctx, include_secrets=True)

    # Import into a second, empty database.
    from selfbot.db import Database

    new_db = Database(f"sqlite+aiosqlite:///{tmp_path / 'restore.db'}")
    await new_db.connect()
    bot.db = new_db

    ctx = _Ctx(bot=bot, event=FakeEvent(), db=new_db)
    counts = await _import_dump(ctx, dump)
    assert counts["quick_replies"] >= 1
    assert counts["channel_reactions"] >= 1
    assert counts["welcomes"] >= 1
    assert counts["users"] >= 1
    assert counts["app_settings"] >= 1

    assert await new_db.get_quick_reply(bot.config.sudo_user_id, "email") == "me@example.com"
    reactions = await new_db.list_reactions()
    assert reactions.get("mychannel") == "🔥"
    welcome = await new_db.get_welcome(-100)
    assert welcome is not None and welcome.message == "hi there" and welcome.enabled
    assert await new_db.get_setting("ai.default_model") == "gpt-x"
    await new_db.close()
    bot.db = db


async def test_import_skips_redacted_provider_keys(bot, tmp_path) -> None:
    await bot.db.add_provider("a", "https://a/v1", "sk-real")
    src_ctx = _Ctx(bot=bot, event=FakeEvent(), db=bot.db)
    dump = await _build_dump(src_ctx, include_secrets=False)

    from selfbot.db import Database

    new_db = Database(f"sqlite+aiosqlite:///{tmp_path / 'redacted.db'}")
    await new_db.connect()
    bot.db = new_db
    ctx = _Ctx(bot=bot, event=FakeEvent(), db=new_db)
    counts = await _import_dump(ctx, dump)
    assert counts["ai_providers"] == 0
    assert await new_db.get_provider("a") is None
    await new_db.close()


async def test_backup_command_sends_file(bot) -> None:
    sent: list[dict[str, Any]] = []

    async def send_file(chat_id, file, **kwargs):
        sent.append({"chat_id": chat_id, "file": file, **kwargs})
        return FakeMessage()

    bot.client.send_file = send_file
    event = FakeEvent(raw_text="backup")
    await bot.registry.dispatch(bot, event, "backup")
    assert sent, "backup should upload a file"
    assert sent[0]["file"].endswith(".json")


async def test_restore_rejects_non_reply(bot) -> None:
    event = FakeEvent(raw_text="restore")
    await bot.registry.dispatch(bot, event, "restore")
    assert any("Reply to" in r for r in event.replies)


async def test_restore_validates_backup_version(bot, tmp_path) -> None:
    import json

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 999}), encoding="utf-8")

    replied = _Replied(file=_File(name="bad.json"), _bytes=bad.read_bytes())
    event = FakeEvent(
        raw_text="restore -force", is_reply=True, reply_message=replied
    )
    bot.confirm_result = True
    await bot.registry.dispatch(bot, event, "restore -force")
    assert any("version" in r.lower() or "unrecognised" in r.lower() for r in event.replies)


async def test_restore_round_trip_via_command(bot, tmp_path) -> None:
    import json

    # Seed data, build a dump file, then restore into a fresh db.
    await bot.db.set_quick_reply(bot.config.sudo_user_id, "hi", "hello")
    ctx = _Ctx(bot=bot, event=FakeEvent(), db=bot.db)
    dump = await _build_dump(ctx, include_secrets=True)
    payload = json.dumps(dump).encode()

    from selfbot.db import Database

    fresh = Database(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    await fresh.connect()
    bot.db = fresh
    bot.ai = None
    bot.confirm_result = True

    replied = _Replied(
        file=_File(name="selfbot-backup.json"), _bytes=payload
    )
    event = FakeEvent(
        raw_text="restore", is_reply=True, reply_message=replied
    )
    await bot.registry.dispatch(bot, event, "restore")

    assert await fresh.get_quick_reply(bot.config.sudo_user_id, "hi") == "hello"
    assert any("Restore complete" in r for r in event.replies)
    await fresh.close()
