"""Shared fixtures and lightweight Telethon fakes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from selfbot.config import (
    Config,
    SpamConfig,
    StickerConfig,
    SupervisorConfig,
    TelegramConfig,
)
from selfbot.db import Database
from selfbot.registry import CommandRegistry, Context

SUDO_ID = 4242


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A fully populated config pointing at a temporary directory."""
    return Config(
        telegram=TelegramConfig(
            api_id=1, api_hash="hash", phone="", session_name="test"
        ),
        sticker=StickerConfig(bot_token="", bot_username="", watermark=""),
        supervisor=SupervisorConfig(
            config_path="", process_name="", log_file="", executable=""
        ),
        spam=SpamConfig(delay=0.0, limit=10, cooldown=0.0),
        sudo_user_id=SUDO_ID,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        data_dir=tmp_path,
        log_level="CRITICAL",
        log_channel_id=None,
        log_channel_level="WARNING",
        command_prefix="",
        quick_reply_prefix="-",
        startup_notify="off",
        max_file_size_mb=8,
        temp_ttl_minutes=60,
    )


@pytest.fixture
async def db(config: Config) -> Database:
    database = Database(config.database_url)
    await database.connect()
    await database.ensure_sudo(config.sudo_user_id)
    try:
        yield database
    finally:
        await database.close()


# ---------------------------------------------------------------------------
# Telethon doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeMessage:
    """Stands in for a sent/edited Telethon message."""

    id: int = 1
    text: str = ""
    deleted: bool = False
    edits: list[str] = field(default_factory=list)

    async def edit(self, text: str, **_kwargs: Any) -> FakeMessage:
        self.text = text
        self.edits.append(text)
        return self

    async def delete(self) -> None:
        self.deleted = True


@dataclass
class FakeEvent:
    """Minimal stand-in for ``events.NewMessage.Event``."""

    raw_text: str = ""
    sender_id: int = SUDO_ID
    chat_id: int = -100
    id: int = 99
    out: bool = True
    is_reply: bool = False
    reply_message: Any = None
    replies: list[str] = field(default_factory=list)
    responses: list[str] = field(default_factory=list)
    deleted: bool = False
    edited: list[str] = field(default_factory=list)

    @property
    def message(self) -> Any:
        return self

    async def reply(self, text: str, **_kwargs: Any) -> FakeMessage:
        self.replies.append(text)
        return FakeMessage(id=len(self.replies) + 1000, text=text)

    async def respond(self, text: str, **_kwargs: Any) -> FakeMessage:
        self.responses.append(text)
        return FakeMessage(id=len(self.responses) + 2000, text=text)

    async def edit(self, text: str, **_kwargs: Any) -> FakeMessage:
        self.edited.append(text)
        return FakeMessage(text=text)

    async def delete(self) -> None:
        self.deleted = True

    async def get_reply_message(self) -> Any:
        return self.reply_message


class FakeClient:
    """Records calls instead of touching the network."""

    def __init__(self) -> None:
        self.sent_files: list[dict[str, Any]] = []
        self.sent_messages: list[tuple[Any, str]] = []
        self.deleted: list[tuple[Any, Any]] = []
        self.edited: list[tuple[Any, int, str]] = []
        self.entities: dict[Any, Any] = {}

    async def send_file(self, chat_id: Any, file: Any, **kwargs: Any) -> FakeMessage:
        self.sent_files.append({"chat_id": chat_id, "file": file, **kwargs})
        return FakeMessage(id=7777)

    async def send_message(self, chat_id: Any, text: str, **_kwargs: Any) -> FakeMessage:
        self.sent_messages.append((chat_id, text))
        return FakeMessage(text=text)

    async def edit_message(self, chat_id: Any, message_id: int, text: str, **_k: Any) -> FakeMessage:
        self.edited.append((chat_id, message_id, text))
        return FakeMessage(id=message_id, text=text)

    async def delete_messages(self, chat_id: Any, ids: Any) -> None:
        self.deleted.append((chat_id, ids))

    async def get_entity(self, target: Any) -> Any:
        if target in self.entities:
            return self.entities[target]
        raise ValueError(f"No entity {target!r}")

    def is_connected(self) -> bool:
        return False


class FakeBot:
    """A SelfBot-shaped object with no Telethon or network dependency."""

    def __init__(self, config: Config, db: Database, registry: CommandRegistry) -> None:
        self.config = config
        self.db = db
        self.registry = registry
        self.client = FakeClient()
        self.active = True
        self.me = None
        self.uptime = 1.0
        self.spam_tasks: dict[int, asyncio.Task[Any]] = {}
        self.spam_cooldowns: dict[int, float] = {}
        self.timer_tasks: dict[str, asyncio.Task[Any]] = {}
        self.zip_queue: dict[int, list[Any]] = {}
        self.active_sticker_pack: dict[int, dict[str, Any]] = {}
        self.pending_confirmations: dict[Any, Any] = {}
        self.confirm_result = True
        self.confirm_prompts: list[str] = []
        self.http = None
        self._auto_reply_cache: dict[int, list[Any]] = {}
        self.auto_reply_cache_invalidated: list[int | None] = []
        self.reaction_cache_invalidated = False

    def is_sudo(self, event: Any) -> bool:
        return bool(getattr(event, "out", False)) or event.sender_id == self.config.sudo_user_id

    async def is_authorized(self, event: Any) -> bool:
        if getattr(event, "out", False):
            return True
        return await self.db.is_known_user(event.sender_id)

    async def reply(self, event: Any, text: str, **kwargs: Any) -> Any:
        return await event.reply(text, **kwargs)

    async def edit(self, message: Any, text: str, **kwargs: Any) -> Any:
        return await message.edit(text, **kwargs)

    async def confirm(self, event: Any, prompt: str, **_kwargs: Any) -> bool:
        self.confirm_prompts.append(prompt)
        return self.confirm_result

    def invalidate_auto_reply_cache(self, chat_id: int | None = None) -> None:
        self.auto_reply_cache_invalidated.append(chat_id)
        if chat_id is None:
            self._auto_reply_cache.clear()
        else:
            self._auto_reply_cache.pop(chat_id, None)

    def invalidate_reaction_cache(self) -> None:
        self.reaction_cache_invalidated = True


@pytest.fixture
def registry() -> CommandRegistry:
    """A registry preloaded with the real plugins."""
    from selfbot.plugins import load_all
    from selfbot.registry import registry as global_registry

    load_all()
    return global_registry


@pytest.fixture
def bot(config: Config, db: Database, registry: CommandRegistry) -> FakeBot:
    return FakeBot(config, db, registry)


@pytest.fixture
def make_ctx(bot: FakeBot):
    """Factory building a Context for a given command line."""

    def _make(command: str, raw_args: str = "", **event_kwargs: Any) -> Context:
        event = FakeEvent(raw_text=f"{command} {raw_args}".strip(), **event_kwargs)
        return Context(
            event=event,
            bot=bot,
            args=CommandRegistry.split_args(raw_args),
            raw_args=raw_args,
            command=command,
        )

    return _make
