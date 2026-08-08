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
    "AutoReply",
    "Database",
    "QuickReply",
    "StickerPack",
    "Timer",
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
class StickerPack:
    name: str
    title: str
    owner_id: int
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
                "CREATE INDEX IF NOT EXISTS idx_timers_active ON timers (is_active, end_time)",
                "CREATE INDEX IF NOT EXISTS idx_timers_user ON timers (user_id)",
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
