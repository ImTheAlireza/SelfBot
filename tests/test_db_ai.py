"""Tests for the AI/settings/plugin repository methods and schema."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from selfbot.db import Database, utcnow
from selfbot.security import SecretBox


@pytest.fixture
def box(tmp_path: Path) -> SecretBox:
    return SecretBox(tmp_path / "secret.key")


@pytest.fixture
async def encrypted_db(tmp_path: Path, box: SecretBox) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'enc.db'}")
    db.attach_secrets(box)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
async def plain_db(tmp_path: Path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'plain.db'}")
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


async def test_provider_keys_are_encrypted_at_rest(encrypted_db: Database) -> None:
    await encrypted_db.add_provider(
        "anyapi", "https://api.example.com/v1", "sk-secret", model="m"
    )
    raw = await encrypted_db.fetch_one(
        "SELECT api_key FROM ai_providers WHERE name = %s", ("anyapi",)
    )
    assert raw is not None
    assert raw["api_key"].startswith("enc::")
    assert "sk-secret" not in raw["api_key"]

    fetched = await encrypted_db.get_provider("anyapi")
    assert fetched is not None
    assert fetched.api_key == "sk-secret"
    assert fetched.redacted_key == "••••cret"


async def test_works_without_encryption(plain_db: Database) -> None:
    await plain_db.add_provider("p", "https://x/v1", "plain-key")
    p = await plain_db.get_provider("p")
    assert p is not None and p.api_key == "plain-key"


async def test_default_provider_logic(encrypted_db: Database) -> None:
    await encrypted_db.add_provider(
        "a", "https://a/v1", "ka", model="ma", is_default=True
    )
    await encrypted_db.add_provider("b", "https://b/v1", "kb")
    # Setting b as default must clear a's default flag.
    await encrypted_db.set_default_provider("b")
    assert (await encrypted_db.get_provider("a")).is_default is False
    assert (await encrypted_db.get_default_provider()).name == "b"


async def test_only_one_default_on_insert(encrypted_db: Database) -> None:
    await encrypted_db.add_provider("a", "https://a/v1", "ka", is_default=True)
    await encrypted_db.add_provider("b", "https://b/v1", "kb", is_default=True)
    rows = await encrypted_db.fetch_all(
        "SELECT name FROM ai_providers WHERE is_default = 1"
    )
    assert [r["name"] for r in rows] == ["b"]


async def test_provider_cooldown_and_counters(encrypted_db: Database) -> None:
    await encrypted_db.add_provider("a", "https://a/v1", "ka")
    until = utcnow() + timedelta(seconds=60)
    await encrypted_db.set_provider_cooldown("a", until, error="quota")
    p = await encrypted_db.get_provider("a")
    assert p.cooldown_until is not None
    assert p.last_error == "quota"

    await encrypted_db.record_provider_result("a", success=True)
    p = await encrypted_db.get_provider("a")
    assert p.success_count == 1
    assert p.cooldown_until is None
    assert p.last_error is None

    await encrypted_db.record_provider_result("a", success=False, error="boom")
    p = await encrypted_db.get_provider("a")
    assert p.failure_count == 1
    assert p.last_error == "boom"


async def test_update_provider_fields(encrypted_db: Database) -> None:
    await encrypted_db.add_provider("a", "https://a/v1", "ka", model="old")
    await encrypted_db.update_provider(
        "a", model="new", api_key="kb", enabled=False
    )
    p = await encrypted_db.get_provider("a")
    assert p.model == "new"
    assert p.api_key == "kb"
    assert p.enabled is False


async def test_update_rejects_unknown_fields(encrypted_db: Database) -> None:
    await encrypted_db.add_provider("a", "https://a/v1", "ka")
    with pytest.raises(ValueError):
        await encrypted_db.update_provider("a", nope="x")


async def test_conversation_memory_ordering_and_prune(
    encrypted_db: Database,
) -> None:
    for i in range(5):
        await encrypted_db.add_ai_message(-100, "user", f"u{i}")
        await encrypted_db.add_ai_message(-100, "assistant", f"a{i}")

    recent = await encrypted_db.recent_ai_messages(-100, limit=4)
    assert [m.content for m in recent] == ["u3", "a3", "u4", "a4"]
    assert recent[0].role == "user"

    assert await encrypted_db.count_ai_messages(-100) == 10
    removed = await encrypted_db.prune_ai_messages(-100, keep=4)
    assert removed >= 6
    assert await encrypted_db.count_ai_messages(-100) == 4


async def test_clear_ai_messages(encrypted_db: Database) -> None:
    await encrypted_db.add_ai_message(-100, "user", "hi")
    assert await encrypted_db.clear_ai_messages(-100) >= 1
    assert await encrypted_db.count_ai_messages(-100) == 0


async def test_settings_round_trip(encrypted_db: Database) -> None:
    assert await encrypted_db.get_setting("missing", 42) == 42
    await encrypted_db.set_setting("flag", True)
    assert await encrypted_db.get_setting("flag") is True
    await encrypted_db.set_setting("nested", {"a": [1, 2]})
    assert await encrypted_db.get_setting("nested") == {"a": [1, 2]}
    await encrypted_db.set_setting("flag", False)
    assert await encrypted_db.get_setting("flag") is False
    all_settings = await encrypted_db.all_settings()
    assert "nested" in all_settings
    assert await encrypted_db.delete_setting("nested") >= 1


async def test_plugin_state(encrypted_db: Database) -> None:
    await encrypted_db.set_plugin_state(
        "demo", "local:/tmp/demo.py", version="1.0"
    )
    state = await encrypted_db.get_plugin_state("demo")
    assert state is not None and state["enabled"] is True
    await encrypted_db.set_plugin_enabled("demo", False)
    states = {s["name"]: s for s in await encrypted_db.list_plugin_state()}
    assert states["demo"]["enabled"] is False
    assert await encrypted_db.delete_plugin_state("demo") >= 1


async def test_export_rows_includes_all_sections(encrypted_db: Database) -> None:
    await encrypted_db.add_provider("a", "https://a/v1", "ka")
    dump = await encrypted_db.export_rows()
    for section in (
        "users",
        "channel_reactions",
        "quick_replies",
        "auto_replies",
        "welcomes",
        "timers",
        "sticker_packs",
        "app_settings",
        "plugin_state",
        "ai_providers",
    ):
        assert section in dump
    # export decrypts keys so an authorized backup can include them.
    assert dump["ai_providers"][0]["api_key"] == "ka"


async def test_schema_is_idempotent(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    await db.connect()
    await db.close()
    # Re-opening against the same file must not error.
    db2 = Database(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    await db2.connect()
    await db2.add_provider("x", "https://x/v1", "kx")
    await db2.close()


async def test_count_providers(encrypted_db: Database) -> None:
    assert await encrypted_db.count_providers() == 0
    await encrypted_db.add_provider("a", "https://a/v1", "ka")
    await encrypted_db.add_provider("b", "https://b/v1", "kb")
    await encrypted_db.update_provider("b", enabled=False)
    assert await encrypted_db.count_providers() == 1
