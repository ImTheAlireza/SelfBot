"""Async database layer.

The original bot opened a fresh blocking ``pymysql`` connection for every
query — including one on *every inbound message* for the authorisation check.
This module replaces that with a single async engine plus a small repository
API, and defaults to SQLite so the bot runs with zero setup.

``DATABASE_URL`` selects the backend:

* ``sqlite+aiosqlite:///./data/selfbot.db``  (default)
* ``mysql+aiomysql://user:pass@host/dbname``
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .errors import ConfigError

logger = logging.getLogger(__name__)

__all__ = [
    "AIMessage",
    "AIProvider",
    "AutoReply",
    "Database",
    "QuickReply",
    "StickerPack",
    "Timer",
    "Welcome",
    "utcnow",
]


def utcnow() -> datetime:
    """Timezone-aware UTC now.

    The original code mixed naive ``datetime.now()`` with database ``NOW()``,
    which silently broke timers whenever the server clock was not in the same
    zone as MySQL. Everything here is UTC, end to end.
    """
    return datetime.now(timezone.utc)


def _as_aware(value: Any) -> datetime | None:
    """Normalise a value coming out of the DB into an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    value = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Row types
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Timer:
    hash: str
    user_id: int
    chat_id: int
    title: str
    duration_seconds: int
    end_time: datetime
    message_id: int | None = None
    is_active: bool = True
    created_at: datetime | None = None

    @property
    def remaining_seconds(self) -> int:
        return max(0, int((self.end_time - utcnow()).total_seconds()))

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Timer:
        return cls(
            hash=row["hash"],
            user_id=int(row["user_id"]),
            chat_id=int(row["chat_id"]),
            title=row["title"],
            duration_seconds=int(row["duration_seconds"]),
            end_time=_as_aware(row["end_time"]) or utcnow(),
            message_id=int(row["message_id"]) if row.get("message_id") else None,
            is_active=bool(row.get("is_active", True)),
            created_at=_as_aware(row.get("created_at")),
        )


@dataclass(slots=True)
class QuickReply:
    alias: str
    message: str
    created_at: datetime | None = None


@dataclass(slots=True)
class AutoReply:
    chat_id: int
    mode: str
    trigger: str
    reply_text: str
    reply_condition: str = "any"  # "any" | "nr" | "sr"
    created_at: datetime | None = None


@dataclass(slots=True)
class Welcome:
    """A per-chat welcome message template."""

    chat_id: int
    message: str
    enabled: bool = False
    created_at: datetime | None = None


@dataclass(slots=True)
class StickerPack:
    name: str
    title: str
    owner_id: int
    created_at: datetime | None = None


@dataclass(slots=True)
class AIProvider:
    """A configured AI provider (OpenAI-compatible or RapidAPI)."""

    name: str
    base_url: str
    api_key: str  # decrypted by the repository when a SecretBox is attached
    kind: str = "openai"  # "openai" | "rapidapi" | "rapidapi_backup"
    model: str = ""
    is_default: bool = False
    enabled: bool = True
    cooldown_until: datetime | None = None
    last_error: str | None = None
    success_count: int = 0
    failure_count: int = 0
    id: int | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AIProvider:
        return cls(
            id=int(row["id"]) if row.get("id") is not None else None,
            name=row["name"],
            base_url=row.get("base_url", ""),
            api_key=row.get("api_key", ""),
            kind=row.get("kind", "openai"),
            model=row.get("model", ""),
            is_default=bool(row.get("is_default", 0)),
            enabled=bool(row.get("enabled", 1)),
            cooldown_until=_as_aware(row.get("cooldown_until")),
            last_error=row.get("last_error"),
            success_count=int(row.get("success_count", 0)),
            failure_count=int(row.get("failure_count", 0)),
            created_at=_as_aware(row.get("created_at")),
        )

    @property
    def redacted_key(self) -> str:
        if not self.api_key:
            return "(none)"
        tail = self.api_key[-4:] if len(self.api_key) >= 4 else "••••"
        return f"••••{tail}"


@dataclass(slots=True)
class AIMessage:
    """One turn stored for conversation memory."""

    chat_id: int
    role: str
    content: str
    provider: str | None = None
    id: int | None = None
    created_at: datetime | None = None


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

_SQLITE_PREFIXES = ("sqlite://", "sqlite+aiosqlite://")
_MYSQL_PREFIXES = ("mysql://", "mysql+aiomysql://", "mysql+pymysql://")


class Database:
    """Thin async wrapper over aiosqlite / aiomysql with a repository API."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._backend = self._detect_backend(url)
        self._conn: Any = None  # aiosqlite connection
        self._pool: Any = None  # aiomysql pool
        self._lock: Any = None
        self._secrets: Any = None  # SecretBox, attached by the application

    def attach_secrets(self, secrets: Any) -> None:
        """Attach a :class:`~selfbot.security.SecretBox` for encrypted columns.

        When attached, ``api_key`` values in ``ai_providers`` are encrypted on
        write and decrypted on read. Without it, values are stored as-is
        (useful in tests that do not exercise encryption).
        """
        self._secrets = secrets

    def _encrypt(self, plaintext: str) -> str:
        if self._secrets is None or not plaintext:
            return plaintext
        return self._secrets.encrypt(plaintext)

    def _decrypt(self, value: str) -> str:
        if self._secrets is None or not value:
            return value
        return self._secrets.decrypt(value)

    # -- lifecycle ---------------------------------------------------------

    @staticmethod
    def _detect_backend(url: str) -> str:
        low = url.lower()
        if low.startswith(_SQLITE_PREFIXES):
            return "sqlite"
        if low.startswith(_MYSQL_PREFIXES):
            return "mysql"
        raise ConfigError(
            f"Unsupported DATABASE_URL {url!r}. "
            "Use sqlite+aiosqlite:///path.db or mysql+aiomysql://user:pass@host/db"
        )

    @property
    def backend(self) -> str:
        return self._backend

    async def connect(self) -> None:
        if self._backend == "sqlite":
            await self._connect_sqlite()
        else:
            await self._connect_mysql()
        await self._create_schema()
        logger.info("Database ready (%s)", self._backend)

    async def _connect_sqlite(self) -> None:
        try:
            import aiosqlite
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ConfigError(
                "aiosqlite is required for the SQLite backend: pip install aiosqlite"
            ) from exc
        import asyncio

        path = re.sub(r"^sqlite(\+aiosqlite)?:///?", "", self._url) or ":memory:"
        if path != ":memory:":
            from pathlib import Path

            Path(path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(path)
        self._conn.row_factory = aiosqlite.Row
        self._lock = asyncio.Lock()
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.commit()

    async def _connect_mysql(self) -> None:
        try:
            import aiomysql
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ConfigError(
                "aiomysql is required for the MySQL backend: pip install aiomysql"
            ) from exc

        match = re.match(
            r"^mysql(?:\+\w+)?://(?P<user>[^:@]+)(?::(?P<password>[^@]*))?"
            r"@(?P<host>[^:/]+)(?::(?P<port>\d+))?/(?P<db>[^?]+)",
            self._url,
        )
        if not match:
            raise ConfigError(f"Could not parse MySQL URL: {self._url!r}")

        parts = match.groupdict()
        self._pool = await aiomysql.create_pool(
            host=parts["host"],
            port=int(parts["port"] or 3306),
            user=parts["user"],
            password=parts["password"] or "",
            db=parts["db"],
            charset="utf8mb4",
            autocommit=True,
            minsize=1,
            maxsize=10,
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def __aenter__(self) -> Database:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # -- low level ---------------------------------------------------------

    def _sql(self, query: str) -> str:
        """Translate placeholder style and dialect quirks."""
        if self._backend == "sqlite":
            return query.replace("%s", "?")
        return query

    async def execute(self, query: str, params: Sequence[Any] = ()) -> int:
        """Run a write. Returns affected row count."""
        sql = self._sql(query)
        if self._backend == "sqlite":
            async with self._lock:
                cursor = await self._conn.execute(sql, tuple(params))
                await self._conn.commit()
                count = cursor.rowcount
                await cursor.close()
                return count
        async with self._pool.acquire() as conn, conn.cursor() as cursor:
            await cursor.execute(sql, tuple(params))
            return cursor.rowcount

    async def fetch_one(
        self, query: str, params: Sequence[Any] = ()
    ) -> dict[str, Any] | None:
        rows = await self.fetch_all(query, params, limit_hint=1)
        return rows[0] if rows else None

    async def fetch_all(
        self,
        query: str,
        params: Sequence[Any] = (),
        *,
        limit_hint: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = self._sql(query)
        if self._backend == "sqlite":
            async with self._lock:
                cursor = await self._conn.execute(sql, tuple(params))
                rows = await (cursor.fetchmany(limit_hint) if limit_hint else cursor.fetchall())
                await cursor.close()
                return [dict(row) for row in rows]

        import aiomysql

        async with self._pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute(sql, tuple(params))
            rows = await (
                cursor.fetchmany(limit_hint) if limit_hint else cursor.fetchall()
            )
            return [dict(row) for row in rows]

    # -- schema ------------------------------------------------------------

    async def _create_schema(self) -> None:
        if self._backend == "sqlite":
            statements = [
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    role TEXT NOT NULL DEFAULT 'admin',
                    username TEXT,
                    added_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS channel_reactions (
                    channel TEXT PRIMARY KEY,
                    emoji TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS quick_replies (
                    user_id INTEGER NOT NULL,
                    alias TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, alias)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS auto_replies (
                    chat_id INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    trigger_text TEXT NOT NULL,
                    reply_text TEXT NOT NULL,
                    reply_condition TEXT NOT NULL DEFAULT 'any',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, mode, trigger_text)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS timers (
                    hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    end_time TEXT NOT NULL,
                    message_id INTEGER,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS sticker_packs (
                    name TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    owner_id INTEGER NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS welcomes (
                    chat_id INTEGER PRIMARY KEY,
                    message TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS ai_providers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    base_url TEXT NOT NULL,
                    api_key TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'openai',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    cooldown_until TEXT,
                    last_error TEXT,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS ai_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    provider TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS plugin_state (
                    name TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    version TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    loaded_at TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """,
                "CREATE INDEX IF NOT EXISTS idx_timers_active ON timers (is_active, end_time)",
                "CREATE INDEX IF NOT EXISTS idx_timers_user ON timers (user_id)",
                "CREATE INDEX IF NOT EXISTS idx_ai_messages_chat ON ai_messages (chat_id, id)",
                "CREATE INDEX IF NOT EXISTS idx_ai_providers_default ON ai_providers (is_default, enabled)",
                """
                CREATE TABLE IF NOT EXISTS search_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    is_saved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """,
                "CREATE INDEX IF NOT EXISTS idx_search_history_user ON search_history (user_id, created_at)",
            ]
        else:
            charset = "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            statements = [
                f"""
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT PRIMARY KEY,
                    role VARCHAR(32) NOT NULL DEFAULT 'admin',
                    username VARCHAR(128) DEFAULT NULL,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) {charset}
                """,
                f"""
                CREATE TABLE IF NOT EXISTS channel_reactions (
                    channel VARCHAR(191) PRIMARY KEY,
                    emoji VARCHAR(16) NOT NULL
                ) {charset}
                """,
                f"""
                CREATE TABLE IF NOT EXISTS quick_replies (
                    user_id BIGINT NOT NULL,
                    alias VARCHAR(64) NOT NULL,
                    message TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, alias)
                ) {charset}
                """,
                f"""
                CREATE TABLE IF NOT EXISTS auto_replies (
                    chat_id BIGINT NOT NULL,
                    mode VARCHAR(16) NOT NULL,
                    trigger_text VARCHAR(255) NOT NULL,
                    reply_text TEXT NOT NULL,
                    reply_condition VARCHAR(8) NOT NULL DEFAULT 'any',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, mode, trigger_text)
                ) {charset}
                """,
                f"""
                CREATE TABLE IF NOT EXISTS timers (
                    hash VARCHAR(16) PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    chat_id BIGINT NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    duration_seconds INT NOT NULL,
                    end_time DATETIME NOT NULL,
                    message_id BIGINT DEFAULT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_timers_active (is_active, end_time),
                    INDEX idx_timers_user (user_id)
                ) {charset}
                """,
                f"""
                CREATE TABLE IF NOT EXISTS sticker_packs (
                    name VARCHAR(64) PRIMARY KEY,
                    title VARCHAR(255) NOT NULL,
                    owner_id BIGINT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) {charset}
                """,
                f"""
                CREATE TABLE IF NOT EXISTS welcomes (
                    chat_id BIGINT PRIMARY KEY,
                    message TEXT NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) {charset}
                """,
                f"""
                CREATE TABLE IF NOT EXISTS ai_providers (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(64) NOT NULL UNIQUE,
                    base_url VARCHAR(512) NOT NULL,
                    api_key TEXT NOT NULL,
                    model VARCHAR(255) NOT NULL DEFAULT '',
                    kind VARCHAR(32) NOT NULL DEFAULT 'openai',
                    is_default BOOLEAN NOT NULL DEFAULT FALSE,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    cooldown_until DATETIME NULL,
                    last_error TEXT,
                    success_count INT NOT NULL DEFAULT 0,
                    failure_count INT NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_ai_providers_default (is_default, enabled)
                ) {charset}
                """,
                f"""
                CREATE TABLE IF NOT EXISTS ai_messages (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    role VARCHAR(16) NOT NULL,
                    content MEDIUMTEXT NOT NULL,
                    provider VARCHAR(64),
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_ai_messages_chat (chat_id, id)
                ) {charset}
                """,
                f"""
                CREATE TABLE IF NOT EXISTS app_settings (
                    `key` VARCHAR(128) PRIMARY KEY,
                    value MEDIUMTEXT NOT NULL
                ) {charset}
                """,
                f"""
                CREATE TABLE IF NOT EXISTS plugin_state (
                    name VARCHAR(128) PRIMARY KEY,
                    source VARCHAR(512) NOT NULL,
                    version VARCHAR(64),
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    loaded_at TIMESTAMP NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) {charset}
                """,
                f"""
                CREATE TABLE IF NOT EXISTS search_history (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    label VARCHAR(512) NOT NULL,
                    payload MEDIUMTEXT NOT NULL,
                    is_saved BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_search_history_user (user_id, created_at)
                ) {charset}
                """,
            ]

        for statement in statements:
            await self.execute(statement)

        # Migrate existing tables that predate the reply_condition column.
        if self._backend == "sqlite":
            try:
                cols = await self.fetch_all("PRAGMA table_info(auto_replies)")
                col_names = {c["name"] for c in cols}
                if "reply_condition" not in col_names:
                    await self.execute(
                        "ALTER TABLE auto_replies ADD COLUMN reply_condition TEXT NOT NULL DEFAULT 'any'"
                    )
            except Exception:
                logger.debug("auto_replies migration check failed", exc_info=True)
        else:
            try:
                row = await self.fetch_one(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_NAME = 'auto_replies' AND COLUMN_NAME = 'reply_condition'"
                )
                if not row:
                    await self.execute(
                        "ALTER TABLE auto_replies ADD COLUMN reply_condition "
                        "VARCHAR(8) NOT NULL DEFAULT 'any'"
                    )
            except Exception:
                logger.debug("auto_replies migration check failed", exc_info=True)

    # -- users -------------------------------------------------------------

    async def ensure_sudo(self, user_id: int) -> None:
        existing = await self.fetch_one("SELECT id FROM users WHERE id = %s", (user_id,))
        if existing:
            await self.execute("UPDATE users SET role = 'sudo' WHERE id = %s", (user_id,))
        else:
            await self.execute(
                "INSERT INTO users (id, role, username) VALUES (%s, 'sudo', %s)",
                (user_id, "owner"),
            )

    async def is_known_user(self, user_id: int) -> bool:
        row = await self.fetch_one("SELECT 1 AS ok FROM users WHERE id = %s", (user_id,))
        return row is not None

    async def get_role(self, user_id: int) -> str | None:
        row = await self.fetch_one("SELECT role FROM users WHERE id = %s", (user_id,))
        return row["role"] if row else None

    async def add_admin(self, user_id: int, username: str | None) -> None:
        exists = await self.fetch_one("SELECT id FROM users WHERE id = %s", (user_id,))
        if exists:
            await self.execute(
                "UPDATE users SET role = 'admin', username = %s WHERE id = %s",
                (username, user_id),
            )
        else:
            await self.execute(
                "INSERT INTO users (id, role, username) VALUES (%s, 'admin', %s)",
                (user_id, username),
            )

    async def remove_admin(self, user_id: int) -> int:
        return await self.execute(
            "DELETE FROM users WHERE id = %s AND role <> 'sudo'", (user_id,)
        )

    async def list_users(self) -> list[dict[str, Any]]:
        return await self.fetch_all("SELECT id, role, username FROM users ORDER BY role, id")

    # -- quick replies -----------------------------------------------------

    async def set_quick_reply(self, user_id: int, alias: str, message: str) -> None:
        exists = await self.fetch_one(
            "SELECT alias FROM quick_replies WHERE user_id = %s AND alias = %s",
            (user_id, alias),
        )
        if exists:
            await self.execute(
                "UPDATE quick_replies SET message = %s WHERE user_id = %s AND alias = %s",
                (message, user_id, alias),
            )
        else:
            await self.execute(
                "INSERT INTO quick_replies (user_id, alias, message) VALUES (%s, %s, %s)",
                (user_id, alias, message),
            )

    async def get_quick_reply(self, user_id: int, alias: str) -> str | None:
        row = await self.fetch_one(
            "SELECT message FROM quick_replies WHERE user_id = %s AND alias = %s",
            (user_id, alias),
        )
        return row["message"] if row else None

    async def delete_quick_reply(self, user_id: int, alias: str) -> int:
        return await self.execute(
            "DELETE FROM quick_replies WHERE user_id = %s AND alias = %s", (user_id, alias)
        )

    async def list_quick_replies(self, user_id: int) -> list[QuickReply]:
        rows = await self.fetch_all(
            "SELECT alias, message, created_at FROM quick_replies "
            "WHERE user_id = %s ORDER BY alias",
            (user_id,),
        )
        return [
            QuickReply(
                alias=row["alias"],
                message=row["message"],
                created_at=_as_aware(row.get("created_at")),
            )
            for row in rows
        ]

    # -- auto replies ------------------------------------------------------

    async def set_auto_reply(
        self, chat_id: int, mode: str, trigger: str, reply_text: str, *,
        reply_condition: str = "any",
    ) -> None:
        exists = await self.fetch_one(
            "SELECT trigger_text FROM auto_replies "
            "WHERE chat_id = %s AND mode = %s AND trigger_text = %s",
            (chat_id, mode, trigger),
        )
        if exists:
            await self.execute(
                "UPDATE auto_replies SET reply_text = %s, reply_condition = %s "
                "WHERE chat_id = %s AND mode = %s AND trigger_text = %s",
                (reply_text, reply_condition, chat_id, mode, trigger),
            )
        else:
            await self.execute(
                "INSERT INTO auto_replies (chat_id, mode, trigger_text, reply_text, reply_condition) "
                "VALUES (%s, %s, %s, %s, %s)",
                (chat_id, mode, trigger, reply_text, reply_condition),
            )

    async def delete_auto_reply(self, chat_id: int, mode: str, trigger: str) -> int:
        return await self.execute(
            "DELETE FROM auto_replies WHERE chat_id = %s AND mode = %s AND trigger_text = %s",
            (chat_id, mode, trigger),
        )

    async def delete_all_auto_replies(self) -> int:
        """Remove every auto-reply rule across all chats."""
        return await self.execute("DELETE FROM auto_replies")

    async def list_all_auto_replies(self) -> list[AutoReply]:
        """Return every auto-reply rule across all chats."""
        rows = await self.fetch_all(
            "SELECT chat_id, mode, trigger_text, reply_text, reply_condition, created_at "
            "FROM auto_replies ORDER BY chat_id, mode, trigger_text"
        )
        return [
            AutoReply(
                chat_id=int(row["chat_id"]),
                mode=row["mode"],
                trigger=row["trigger_text"],
                reply_text=row["reply_text"],
                reply_condition=row.get("reply_condition", "any"),
                created_at=_as_aware(row.get("created_at")),
            )
            for row in rows
        ]

    async def list_auto_replies(self, chat_id: int) -> list[AutoReply]:
        rows = await self.fetch_all(
            "SELECT chat_id, mode, trigger_text, reply_text, reply_condition, created_at FROM auto_replies "
            "WHERE chat_id = %s",
            (chat_id,),
        )
        rules = [
            AutoReply(
                chat_id=int(row["chat_id"]),
                mode=row["mode"],
                trigger=row["trigger_text"],
                reply_text=row["reply_text"],
                reply_condition=row.get("reply_condition", "any"),
                created_at=_as_aware(row.get("created_at")),
            )
            for row in rows
        ]
        return sorted(
            rules,
            key=lambda rule: (rule.mode != "match", -len(rule.trigger), rule.trigger.casefold()),
        )

    # -- welcomes ----------------------------------------------------------

    async def set_welcome_message(self, chat_id: int, message: str) -> None:
        """Save (or replace) the welcome template for a chat.

        Keeps the enabled flag as-is when a row already exists, so updating
        the text of an active welcome does not silently switch it off.
        """
        exists = await self.fetch_one(
            "SELECT chat_id FROM welcomes WHERE chat_id = %s", (chat_id,)
        )
        if exists:
            await self.execute(
                "UPDATE welcomes SET message = %s WHERE chat_id = %s",
                (message, chat_id),
            )
        else:
            await self.execute(
                "INSERT INTO welcomes (chat_id, message, enabled) VALUES (%s, %s, 0)",
                (chat_id, message),
            )

    async def get_welcome(self, chat_id: int) -> Welcome | None:
        row = await self.fetch_one(
            "SELECT chat_id, message, enabled, created_at FROM welcomes WHERE chat_id = %s",
            (chat_id,),
        )
        if not row:
            return None
        return Welcome(
            chat_id=int(row["chat_id"]),
            message=row["message"],
            enabled=bool(row["enabled"]),
            created_at=_as_aware(row.get("created_at")),
        )

    async def set_welcome_enabled(self, chat_id: int, enabled: bool) -> int:
        """Toggle a chat's welcome. Returns affected rows (0 = no template saved)."""
        return await self.execute(
            "UPDATE welcomes SET enabled = %s WHERE chat_id = %s",
            (1 if enabled else 0, chat_id),
        )

    async def disable_all_welcomes(self) -> int:
        """Switch every welcome off (templates stay saved)."""
        return await self.execute("UPDATE welcomes SET enabled = 0 WHERE enabled = 1")

    async def delete_welcome(self, chat_id: int) -> int:
        return await self.execute("DELETE FROM welcomes WHERE chat_id = %s", (chat_id,))

    async def delete_all_welcomes(self) -> int:
        return await self.execute("DELETE FROM welcomes")

    async def list_welcomes(self) -> list[Welcome]:
        rows = await self.fetch_all(
            "SELECT chat_id, message, enabled, created_at FROM welcomes ORDER BY chat_id"
        )
        return [
            Welcome(
                chat_id=int(row["chat_id"]),
                message=row["message"],
                enabled=bool(row["enabled"]),
                created_at=_as_aware(row.get("created_at")),
            )
            for row in rows
        ]

    # -- reactions ---------------------------------------------------------

    async def set_reaction(self, channel: str, emoji: str) -> None:
        exists = await self.fetch_one(
            "SELECT channel FROM channel_reactions WHERE channel = %s", (channel,)
        )
        if exists:
            await self.execute(
                "UPDATE channel_reactions SET emoji = %s WHERE channel = %s", (emoji, channel)
            )
        else:
            await self.execute(
                "INSERT INTO channel_reactions (channel, emoji) VALUES (%s, %s)",
                (channel, emoji),
            )

    async def delete_reaction(self, channel: str) -> int:
        return await self.execute(
            "DELETE FROM channel_reactions WHERE channel = %s", (channel,)
        )

    async def list_reactions(self) -> dict[str, str]:
        rows = await self.fetch_all("SELECT channel, emoji FROM channel_reactions")
        return {row["channel"]: row["emoji"] for row in rows}

    # -- timers ------------------------------------------------------------

    async def create_timer(self, timer: Timer) -> None:
        await self.execute(
            "INSERT INTO timers (hash, user_id, chat_id, title, duration_seconds, "
            "end_time, message_id, is_active) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                timer.hash,
                timer.user_id,
                timer.chat_id,
                timer.title,
                timer.duration_seconds,
                self._fmt_dt(timer.end_time),
                timer.message_id,
                1,
            ),
        )

    def _fmt_dt(self, value: datetime) -> Any:
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        utc = aware.astimezone(timezone.utc)
        if self._backend == "sqlite":
            return utc.isoformat()
        return utc.replace(tzinfo=None)

    async def get_timer(self, timer_hash: str) -> Timer | None:
        row = await self.fetch_one("SELECT * FROM timers WHERE hash = %s", (timer_hash,))
        return Timer.from_row(row) if row else None

    async def list_active_timers(self, user_id: int | None = None) -> list[Timer]:
        if user_id is None:
            rows = await self.fetch_all(
                "SELECT * FROM timers WHERE is_active = 1 ORDER BY end_time ASC"
            )
        else:
            rows = await self.fetch_all(
                "SELECT * FROM timers WHERE is_active = 1 AND user_id = %s ORDER BY end_time ASC",
                (user_id,),
            )
        return [Timer.from_row(row) for row in rows]

    async def deactivate_timer(self, timer_hash: str) -> int:
        return await self.execute(
            "UPDATE timers SET is_active = 0 WHERE hash = %s", (timer_hash,)
        )

    async def update_timer_message(
        self, timer_hash: str, message_id: int, chat_id: int
    ) -> None:
        await self.execute(
            "UPDATE timers SET message_id = %s, chat_id = %s WHERE hash = %s",
            (message_id, chat_id, timer_hash),
        )

    async def purge_finished_timers(self, older_than_days: int = 7) -> int:
        cutoff = utcnow().timestamp() - older_than_days * 86400
        cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
        return await self.execute(
            "DELETE FROM timers WHERE is_active = 0 AND end_time < %s",
            (self._fmt_dt(cutoff_dt),),
        )

    # -- sticker packs -----------------------------------------------------

    async def add_sticker_pack(self, name: str, title: str, owner_id: int) -> None:
        exists = await self.fetch_one(
            "SELECT name FROM sticker_packs WHERE name = %s", (name,)
        )
        if not exists:
            await self.execute(
                "INSERT INTO sticker_packs (name, title, owner_id) VALUES (%s, %s, %s)",
                (name, title, owner_id),
            )

    async def get_sticker_pack(self, name: str) -> StickerPack | None:
        row = await self.fetch_one(
            "SELECT name, title, owner_id, created_at FROM sticker_packs WHERE name = %s",
            (name,),
        )
        if not row:
            return None
        return StickerPack(
            name=row["name"],
            title=row["title"],
            owner_id=int(row["owner_id"]),
            created_at=_as_aware(row.get("created_at")),
        )

    async def list_sticker_packs(self, owner_id: int | None = None) -> list[StickerPack]:
        if owner_id is None:
            rows = await self.fetch_all(
                "SELECT name, title, owner_id, created_at FROM sticker_packs "
                "ORDER BY created_at DESC"
            )
        else:
            rows = await self.fetch_all(
                "SELECT name, title, owner_id, created_at FROM sticker_packs "
                "WHERE owner_id = %s ORDER BY created_at DESC",
                (owner_id,),
            )
        return [
            StickerPack(
                name=row["name"],
                title=row["title"],
                owner_id=int(row["owner_id"]),
                created_at=_as_aware(row.get("created_at")),
            )
            for row in rows
        ]

    async def delete_sticker_pack(self, name: str) -> int:
        return await self.execute("DELETE FROM sticker_packs WHERE name = %s", (name,))

    # -- ai providers ------------------------------------------------------

    async def list_providers(self, *, enabled_only: bool = False) -> list[AIProvider]:
        query = "SELECT * FROM ai_providers"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY is_default DESC, id ASC"
        rows = await self.fetch_all(query)
        providers = []
        for row in rows:
            row = dict(row)
            row["api_key"] = self._decrypt(row.get("api_key", ""))
            providers.append(AIProvider.from_row(row))
        return providers

    async def get_provider(self, name: str) -> AIProvider | None:
        row = await self.fetch_one(
            "SELECT * FROM ai_providers WHERE name = %s", (name,)
        )
        if row is None:
            return None
        row = dict(row)
        row["api_key"] = self._decrypt(row.get("api_key", ""))
        return AIProvider.from_row(row)

    async def get_default_provider(self) -> AIProvider | None:
        row = await self.fetch_one(
            "SELECT * FROM ai_providers WHERE is_default = 1 AND enabled = 1 "
            "ORDER BY id ASC LIMIT 1"
        )
        if row is None:
            return None
        row = dict(row)
        row["api_key"] = self._decrypt(row.get("api_key", ""))
        return AIProvider.from_row(row)

    async def add_provider(
        self,
        name: str,
        base_url: str,
        api_key: str,
        *,
        model: str = "",
        kind: str = "openai",
        is_default: bool = False,
    ) -> AIProvider:
        if is_default:
            await self.execute("UPDATE ai_providers SET is_default = 0")
        await self.execute(
            "INSERT INTO ai_providers "
            "(name, base_url, api_key, model, kind, is_default, enabled) "
            "VALUES (%s, %s, %s, %s, %s, %s, 1)",
            (
                name,
                base_url,
                self._encrypt(api_key),
                model,
                kind,
                1 if is_default else 0,
            ),
        )
        row = await self.fetch_one(
            "SELECT * FROM ai_providers WHERE name = %s", (name,)
        )
        assert row is not None
        row = dict(row)
        row["api_key"] = self._decrypt(row.get("api_key", ""))
        return AIProvider.from_row(row)

    async def update_provider(self, name: str, **fields: Any) -> int:
        allowed = {"base_url", "api_key", "model", "kind", "enabled", "is_default"}
        updates: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"Cannot update provider field {key!r}")
            if key == "api_key":
                value = self._encrypt(value or "")
            elif key in {"enabled", "is_default"}:
                value = 1 if value else 0
            updates.append(f"{key} = %s")
            params.append(value)
        if not updates:
            return 0
        if fields.get("is_default"):
            await self.execute("UPDATE ai_providers SET is_default = 0")
        params.append(name)
        return await self.execute(
            f"UPDATE ai_providers SET {', '.join(updates)} WHERE name = %s",
            tuple(params),
        )

    async def set_default_provider(self, name: str) -> int:
        await self.execute("UPDATE ai_providers SET is_default = 0")
        return await self.execute(
            "UPDATE ai_providers SET is_default = 1 WHERE name = %s", (name,)
        )

    async def delete_provider(self, name: str) -> int:
        return await self.execute(
            "DELETE FROM ai_providers WHERE name = %s", (name,)
        )

    async def set_provider_cooldown(
        self, name: str, until: datetime | None, error: str | None = None
    ) -> int:
        return await self.execute(
            "UPDATE ai_providers SET cooldown_until = %s, last_error = %s "
            "WHERE name = %s",
            (self._fmt_dt(until) if until else None, error, name),
        )

    async def clear_provider_cooldown(self, name: str) -> int:
        return await self.execute(
            "UPDATE ai_providers SET cooldown_until = NULL WHERE name = %s",
            (name,),
        )

    async def record_provider_result(
        self, name: str, *, success: bool, error: str | None = None
    ) -> None:
        if success:
            await self.execute(
                "UPDATE ai_providers SET success_count = success_count + 1, "
                "cooldown_until = NULL, last_error = NULL WHERE name = %s",
                (name,),
            )
        else:
            await self.execute(
                "UPDATE ai_providers SET failure_count = failure_count + 1, "
                "last_error = %s WHERE name = %s",
                (error, name),
            )

    async def count_providers(self) -> int:
        row = await self.fetch_one(
            "SELECT COUNT(*) AS n FROM ai_providers WHERE enabled = 1"
        )
        return int(row["n"]) if row else 0

    # -- ai conversation memory -------------------------------------------

    async def add_ai_message(
        self,
        chat_id: int,
        role: str,
        content: str,
        *,
        provider: str | None = None,
    ) -> int:
        await self.execute(
            "INSERT INTO ai_messages (chat_id, role, content, provider) "
            "VALUES (%s, %s, %s, %s)",
            (chat_id, role, content, provider),
        )
        if self._backend == "sqlite":
            row = await self.fetch_one("SELECT last_insert_rowid() AS id")
            return int(row["id"]) if row else 0
        # MySQL returns lastrowid through the cursor; fetch the latest by
        # chat/time instead of reaching into the cursor abstraction.
        row = await self.fetch_one(
            "SELECT id FROM ai_messages WHERE chat_id = %s "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,),
        )
        return int(row["id"]) if row else 0

    async def recent_ai_messages(
        self, chat_id: int, limit: int = 20
    ) -> list[AIMessage]:
        rows = await self.fetch_all(
            "SELECT id, chat_id, role, content, provider, created_at FROM "
            "(SELECT * FROM ai_messages WHERE chat_id = %s ORDER BY id DESC "
            "LIMIT %s) ORDER BY id ASC",
            (chat_id, limit),
        )
        return [
            AIMessage(
                id=int(r["id"]),
                chat_id=int(r["chat_id"]),
                role=r["role"],
                content=r["content"],
                provider=r.get("provider"),
                created_at=_as_aware(r.get("created_at")),
            )
            for r in rows
        ]

    async def count_ai_messages(self, chat_id: int) -> int:
        row = await self.fetch_one(
            "SELECT COUNT(*) AS n FROM ai_messages WHERE chat_id = %s",
            (chat_id,),
        )
        return int(row["n"]) if row else 0

    async def clear_ai_messages(self, chat_id: int) -> int:
        return await self.execute(
            "DELETE FROM ai_messages WHERE chat_id = %s", (chat_id,)
        )

    async def prune_ai_messages(self, chat_id: int, keep: int) -> int:
        if keep < 0:
            keep = 0
        if self._backend == "sqlite":
            return await self.execute(
                "DELETE FROM ai_messages WHERE chat_id = %s AND id NOT IN "
                "(SELECT id FROM (SELECT id FROM ai_messages WHERE chat_id = %s "
                "ORDER BY id DESC LIMIT %s))",
                (chat_id, chat_id, keep),
            )
        return await self.execute(
            "DELETE m FROM ai_messages m LEFT JOIN ("
            "  SELECT id FROM ai_messages WHERE chat_id = %s "
            "  ORDER BY id DESC LIMIT %s"
            ") keep ON m.id = keep.id "
            "WHERE m.chat_id = %s AND keep.id IS NULL",
            (chat_id, keep, chat_id),
        )

    # -- generic settings --------------------------------------------------

    async def get_setting(self, key: str, default: Any = None) -> Any:
        import json

        row = await self.fetch_one(
            "SELECT value FROM app_settings WHERE key = %s", (key,)
        )
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return row["value"]

    async def set_setting(self, key: str, value: Any) -> None:
        import json

        serialized = value if isinstance(value, str) else json.dumps(value)
        exists = await self.fetch_one(
            "SELECT 1 FROM app_settings WHERE key = %s", (key,)
        )
        if exists:
            await self.execute(
                "UPDATE app_settings SET value = %s WHERE key = %s",
                (serialized, key),
            )
        else:
            await self.execute(
                "INSERT INTO app_settings (key, value) VALUES (%s, %s)",
                (key, serialized),
            )

    async def delete_setting(self, key: str) -> int:
        return await self.execute(
            "DELETE FROM app_settings WHERE key = %s", (key,)
        )

    async def all_settings(self) -> dict[str, Any]:
        import json

        rows = await self.fetch_all("SELECT key, value FROM app_settings")
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value"])
            except (ValueError, TypeError):
                out[row["key"]] = row["value"]
        return out

    # -- external plugin state --------------------------------------------

    async def list_plugin_state(self) -> list[dict[str, Any]]:
        rows = await self.fetch_all(
            "SELECT name, source, version, enabled, loaded_at, created_at "
            "FROM plugin_state ORDER BY name"
        )
        return [
            {
                "name": r["name"],
                "source": r["source"],
                "version": r.get("version"),
                "enabled": bool(r.get("enabled", 1)),
                "loaded_at": _as_aware(r.get("loaded_at")),
                "created_at": _as_aware(r.get("created_at")),
            }
            for r in rows
        ]

    async def get_plugin_state(self, name: str) -> dict[str, Any] | None:
        row = await self.fetch_one(
            "SELECT name, source, version, enabled, loaded_at FROM "
            "plugin_state WHERE name = %s",
            (name,),
        )
        if row is None:
            return None
        return {
            "name": row["name"],
            "source": row["source"],
            "version": row.get("version"),
            "enabled": bool(row.get("enabled", 1)),
            "loaded_at": _as_aware(row.get("loaded_at")),
        }

    async def set_plugin_state(
        self,
        name: str,
        source: str,
        *,
        version: str | None = None,
        enabled: bool = True,
    ) -> None:
        exists = await self.fetch_one(
            "SELECT 1 FROM plugin_state WHERE name = %s", (name,)
        )
        if exists:
            await self.execute(
                "UPDATE plugin_state SET source = %s, version = %s, "
                "enabled = %s WHERE name = %s",
                (source, version, 1 if enabled else 0, name),
            )
        else:
            await self.execute(
                "INSERT INTO plugin_state (name, source, version, enabled) "
                "VALUES (%s, %s, %s, %s)",
                (name, source, version, 1 if enabled else 0),
            )

    async def set_plugin_enabled(self, name: str, enabled: bool) -> int:
        exists = await self.fetch_one(
            "SELECT 1 FROM plugin_state WHERE name = %s", (name,)
        )
        if exists:
            return await self.execute(
                "UPDATE plugin_state SET enabled = %s WHERE name = %s",
                (1 if enabled else 0, name),
            )
        await self.execute(
            "INSERT INTO plugin_state (name, source, enabled) VALUES (%s, %s, %s)",
            (name, "unknown", 1 if enabled else 0),
        )
        return 1

    async def delete_plugin_state(self, name: str) -> int:
        return await self.execute(
            "DELETE FROM plugin_state WHERE name = %s", (name,)
        )

    # -- search history ----------------------------------------------------

    async def add_search(
        self,
        user_id: int,
        label: str,
        payload: str,
        *,
        saved: bool = False,
    ) -> int:
        await self.execute(
            "INSERT INTO search_history (user_id, label, payload, is_saved) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, label, payload, 1 if saved else 0),
        )
        if self._backend == "sqlite":
            row = await self.fetch_one("SELECT last_insert_rowid() AS id")
            return int(row["id"]) if row else 0
        row = await self.fetch_one(
            "SELECT id FROM search_history WHERE user_id = %s "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        return int(row["id"]) if row else 0

    async def list_searches(
        self, user_id: int, *, saved_only: bool = False, limit: int = 20
    ) -> list[dict[str, Any]]:
        if saved_only:
            rows = await self.fetch_all(
                "SELECT id, label, payload, is_saved, created_at "
                "FROM search_history WHERE user_id = %s AND is_saved = 1 "
                "ORDER BY id DESC LIMIT %s",
                (user_id, limit),
            )
        else:
            rows = await self.fetch_all(
                "SELECT id, label, payload, is_saved, created_at "
                "FROM search_history WHERE user_id = %s "
                "ORDER BY id DESC LIMIT %s",
                (user_id, limit),
            )
        return [
            {
                "id": int(r["id"]),
                "label": r["label"],
                "payload": r["payload"],
                "saved": bool(r.get("is_saved", 0)),
                "created_at": _as_aware(r.get("created_at")),
            }
            for r in rows
        ]

    async def get_search(self, search_id: int) -> dict[str, Any] | None:
        row = await self.fetch_one(
            "SELECT id, user_id, label, payload, is_saved, created_at "
            "FROM search_history WHERE id = %s",
            (search_id,),
        )
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "label": row["label"],
            "payload": row["payload"],
            "saved": bool(row.get("is_saved", 0)),
            "created_at": _as_aware(row.get("created_at")),
        }

    async def set_search_saved(self, search_id: int, saved: bool) -> int:
        return await self.execute(
            "UPDATE search_history SET is_saved = %s WHERE id = %s",
            (1 if saved else 0, search_id),
        )

    async def delete_search(self, search_id: int) -> int:
        return await self.execute(
            "DELETE FROM search_history WHERE id = %s", (search_id,)
        )

    async def prune_searches(self, user_id: int, keep: int = 50) -> int:
        if self._backend == "sqlite":
            return await self.execute(
                "DELETE FROM search_history WHERE user_id = %s AND id NOT IN "
                "(SELECT id FROM (SELECT id FROM search_history "
                "WHERE user_id = %s ORDER BY id DESC LIMIT %s))",
                (user_id, user_id, keep),
            )
        return await self.execute(
            "DELETE h FROM search_history h LEFT JOIN ("
            "  SELECT id FROM search_history WHERE user_id = %s "
            "  ORDER BY id DESC LIMIT %s"
            ") k ON h.id = k.id WHERE h.user_id = %s AND k.id IS NULL",
            (user_id, keep, user_id),
        )

    # -- backup / restore --------------------------------------------------

    async def export_rows(self) -> dict[str, list[dict[str, Any]]]:
        """Dump user-managed tables for the backup command.

        Provider API keys are left to the caller to redact/encrypt; raw keys
        come through decrypted so an explicitly-authorized backup can include
        them.
        """
        tables: dict[str, str] = {
            "users": "SELECT id, role, username, added_at FROM users",
            "channel_reactions": "SELECT channel, emoji FROM channel_reactions",
            "quick_replies": (
                "SELECT user_id, alias, message, created_at FROM quick_replies"
            ),
            "auto_replies": (
                "SELECT chat_id, mode, trigger_text, reply_text, "
                "reply_condition, created_at FROM auto_replies"
            ),
            "welcomes": (
                "SELECT chat_id, message, enabled, created_at FROM welcomes"
            ),
            "timers": (
                "SELECT hash, user_id, chat_id, title, duration_seconds, "
                "end_time, message_id, is_active, created_at FROM timers "
                "WHERE is_active = 1"
            ),
            "sticker_packs": (
                "SELECT name, title, owner_id, created_at FROM sticker_packs"
            ),
            "app_settings": "SELECT key, value FROM app_settings",
            "plugin_state": (
                "SELECT name, source, version, enabled, loaded_at, created_at "
                "FROM plugin_state"
            ),
        }
        dump: dict[str, list[dict[str, Any]]] = {}
        for label, query in tables.items():
            dump[label] = [dict(r) for r in await self.fetch_all(query)]

        providers = []
        for row in await self.fetch_all(
            "SELECT id, name, base_url, api_key, model, kind, is_default, "
            "enabled, created_at FROM ai_providers ORDER BY id"
        ):
            row = dict(row)
            row["api_key"] = self._decrypt(row.get("api_key", ""))
            providers.append(row)
        dump["ai_providers"] = providers
        return dump
