"""The SelfBot application object.

Owns the Telethon client, the database, the command registry and the event
handlers. Everything that used to be a module-level global in ``self.py`` now
lives on an instance, which makes the bot testable and lets state be reset
cleanly between runs.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Awaitable
from datetime import datetime, timezone
from typing import Any

from telethon import TelegramClient, events, functions, types
from telethon.errors import FloodWaitError, MessageNotModifiedError

from .config import Config
from .db import Database
from .errors import ConfigError
from .logging_setup import TelegramLogHandler
from .registry import CommandRegistry
from .registry import registry as global_registry
from .services.ai import build_image_provider, build_provider
from .utils.files import cleanup_old_files
from .utils.http import close_client, get_client
from .utils.text import TELEGRAM_LIMIT, chunk_text

logger = logging.getLogger(__name__)

__all__ = ["SelfBot"]

_REACTION_CACHE_TTL = 300.0


class SelfBot:
    """Wires together the client, database, providers and command dispatch."""

    def __init__(
        self,
        config: Config,
        *,
        registry: CommandRegistry | None = None,
        client: TelegramClient | None = None,
        db: Database | None = None,
    ) -> None:
        self.config = config
        self.registry = registry or global_registry
        self.db = db or Database(config.database_url)
        self.started_at = time.monotonic()

        config.ensure_dirs()
        self.client = client or TelegramClient(
            str(config.session_path),
            config.telegram.api_id,
            config.telegram.api_hash,
            device_model="SelfBot",
            system_version="1.0",
            app_version="2.0",
        )

        self.ai = build_provider(config.ai)
        self.image_ai = build_image_provider(config.image)

        # Mutable runtime state, previously scattered across module globals.
        self.active = True
        self.me: Any = None
        self.spam_tasks: dict[int, asyncio.Task[Any]] = {}
        self.spam_cooldowns: dict[int, float] = {}
        self.timer_tasks: dict[str, asyncio.Task[Any]] = {}
        self.zip_queue: dict[int, list[Any]] = {}
        self.active_sticker_pack: dict[int, dict[str, Any]] = {}
        self.pending_confirmations: dict[tuple[int, int], asyncio.Future[bool]] = {}

        self._reaction_cache: dict[str, str] = {}
        self._reaction_cache_at = 0.0
        self._telegram_log_handler: TelegramLogHandler | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._shutting_down = False

    # -- properties --------------------------------------------------------

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def http(self) -> Any:
        return get_client()

    def is_sudo(self, event: Any) -> bool:
        """True for the account owner.

        ``event.out`` alone is not sufficient: it is also true for messages the
        account sent from another device, which is exactly what we want, but we
        additionally accept the configured sudo ID for admin-forwarded use.
        """
        return bool(getattr(event, "out", False)) or event.sender_id == self.config.sudo_user_id

    async def is_authorized(self, event: Any) -> bool:
        """True when the sender may run commands."""
        if getattr(event, "out", False):
            return True
        sender_id = event.sender_id
        if sender_id is None:
            return False
        if sender_id == self.config.sudo_user_id:
            return True
        return await self.db.is_known_user(sender_id)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        logger.info("Starting SelfBot…")
        await self.db.connect()
        await self.db.ensure_sudo(self.config.sudo_user_id)

        self._register_events()

        await self._sign_in()
        self.me = await self.client.get_me()

        logger.info(
            "Signed in as %s (id=%s)",
            getattr(self.me, "first_name", "?"),
            getattr(self.me, "id", "?"),
        )
        if self.me and self.me.id != self.config.sudo_user_id:
            logger.warning(
                "SUDO_USER_ID (%s) does not match the logged-in account (%s). "
                "Owner-only commands may not work as expected.",
                self.config.sudo_user_id,
                self.me.id,
            )

        self._attach_telegram_logging()
        restored, expired = await self._restore_timers()
        self._spawn(self._janitor(), name="janitor")

        logger.info("Ready — %d commands registered", len(self.registry))
        await self._announce_online(restored, expired)

    async def _announce_online(self, timers_restored: int, timers_expired: int) -> None:
        """Post a startup summary so you know the bot is back.

        Sent to Saved Messages by default; set STARTUP_NOTIFY to `off`, or to
        a chat ID, to change that. Never fatal — a bot that cannot post its
        own greeting should still run.
        """
        target = self.config.startup_notify
        if target == "off":
            return

        started = datetime.now(timezone.utc)
        boot_seconds = time.monotonic() - self.started_at

        lines = [
            "🤖 **SelfBot online**",
            "",
            f"👤 {self._describe_account()}",
            f"⚡ {len(self.registry)} commands · 🗄 {self.db.backend}",
            f"🧠 AI: {self.ai.name} · 🎨 images: {self.image_ai.name}",
        ]

        if timers_restored or timers_expired:
            timer_bits = []
            if timers_restored:
                timer_bits.append(f"{timers_restored} resumed")
            if timers_expired:
                timer_bits.append(f"{timers_expired} expired")
            lines.append(f"⏰ Timers: {', '.join(timer_bits)}")

        lines += [
            f"🕐 {started:%Y-%m-%d %H:%M:%S} UTC · ready in {boot_seconds:.1f}s",
            "",
            "Type `help` to get started.",
        ]

        message = "\n".join(lines)

        try:
            entity = "me" if target == "me" else int(target)
            await self.client.send_message(entity, message)
        except ValueError:
            logger.warning(
                "STARTUP_NOTIFY=%r is not 'me', 'off' or a chat ID; "
                "sending to Saved Messages instead",
                target,
            )
            try:
                await self.client.send_message("me", message)
            except Exception:
                logger.warning("Could not send the startup message", exc_info=True)
        except Exception:
            logger.warning("Could not send the startup message", exc_info=True)

    def _describe_account(self) -> str:
        name = " ".join(
            filter(
                None,
                [
                    getattr(self.me, "first_name", "") or "",
                    getattr(self.me, "last_name", "") or "",
                ],
            )
        ).strip()
        username = getattr(self.me, "username", None)
        if username:
            return f"{name or 'You'} (@{username})"
        return name or f"ID {getattr(self.me, 'id', '?')}"

    async def _sign_in(self) -> None:
        """Authenticate, failing loudly when running unattended.

        Telethon falls back to prompting on stdin. Under a process manager
        there is no stdin, so it dies with `ValueError: No phone number or bot
        token provided.` — which says nothing about the real cause. Detect the
        situation first and name the fix.
        """
        await self.client.connect()

        if not await self.client.is_user_authorized():
            interactive = sys.stdin is not None and sys.stdin.isatty()
            if not interactive:
                raise ConfigError(
                    "Not logged in, and there is no terminal to log in from.\n"
                    f"The session file is missing or expired: "
                    f"{self.config.session_path}.session\n\n"
                    "Stop the service, run `python -m selfbot --login` from a "
                    "shell in the project directory, then start it again."
                )

        await self.client.start(phone=self.config.telegram.phone or None)

    async def run(self) -> None:
        await self.start()
        try:
            await self.client.run_until_disconnected()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("Shutting down…")

        for task in list(self.timer_tasks.values()) + list(self.spam_tasks.values()):
            task.cancel()
        for task in list(self._background):
            task.cancel()

        pending = [
            *self.timer_tasks.values(),
            *self.spam_tasks.values(),
            *self._background,
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        if self._telegram_log_handler is not None:
            await self._telegram_log_handler.stop()
            logging.getLogger().removeHandler(self._telegram_log_handler)

        await close_client()
        await self.db.close()

        if self.client.is_connected():
            await self.client.disconnect()
        logger.info("Goodbye.")

    def _spawn(self, coro: Awaitable[Any], *, name: str | None = None) -> asyncio.Task[Any]:
        """Track a background task so it can be cancelled on shutdown.

        Bare ``asyncio.create_task`` calls in the original code were never kept
        alive, so the garbage collector could cancel them mid-flight.
        """
        task = asyncio.ensure_future(coro)
        if name:
            task.set_name(name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _attach_telegram_logging(self) -> None:
        if not self.config.log_channel_id:
            return
        handler = TelegramLogHandler(self.config.log_channel_id)
        handler.setLevel(
            getattr(logging, self.config.log_channel_level, logging.WARNING)
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s\n%(message)s")
        )
        handler.attach(self.client)
        logging.getLogger().addHandler(handler)
        self._telegram_log_handler = handler
        logger.info("Mirroring logs to channel %s", self.config.log_channel_id)

    # -- events ------------------------------------------------------------

    def _register_events(self) -> None:
        self.client.add_event_handler(self._on_new_message, events.NewMessage())
        self.client.add_event_handler(self._on_message_edited, events.MessageEdited())

    async def _on_new_message(self, event: Any) -> None:
        try:
            await self._handle_message(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unhandled error while processing message")

    async def _on_message_edited(self, event: Any) -> None:
        # Auto-reactions should still fire on edited channel posts.
        try:
            await self._maybe_react(event)
        except Exception:
            logger.debug("Reaction on edited message failed", exc_info=True)

    async def _handle_message(self, event: Any) -> None:
        await self._maybe_react(event)

        text = (event.raw_text or "").strip()
        if not text:
            return

        is_own = bool(getattr(event, "out", False))

        # Pending yes/no confirmation takes priority over command parsing.
        key = (event.chat_id, event.sender_id)
        future = self.pending_confirmations.get(key)
        if future is not None and not future.done():
            answer = text.strip().lower()
            if answer in {"yes", "y", "confirm"}:
                future.set_result(True)
                return
            if answer in {"no", "n", "cancel", "stop"}:
                future.set_result(False)
                return

        if (
            is_own
            and text.startswith(self.config.quick_reply_prefix)
            and await self._try_quick_reply(event, text)
        ):
            return

        if not is_own and not await self.is_authorized(event):
            return

        # Always allow the owner to switch the bot back on.
        if not self.active and not (is_own and self._is_reactivation(text)):
            return

        await self.registry.dispatch(self, event, text)

    def _is_reactivation(self, text: str) -> bool:
        prefix = self.config.command_prefix
        candidate = text[len(prefix):] if prefix and text.startswith(prefix) else text
        return candidate.strip().lower() in {"self on", "on"}

    async def _try_quick_reply(self, event: Any, text: str) -> bool:
        prefix = self.config.quick_reply_prefix
        alias = text[len(prefix):].strip().lower()
        if not alias or " " in alias or not alias.isalnum():
            return False

        message = await self.db.get_quick_reply(event.sender_id, alias)
        if not message:
            return False

        try:
            await event.edit(message)
            logger.debug("Quick reply %r expanded", alias)
            return True
        except Exception as exc:
            logger.warning("Could not expand quick reply %r: %s", alias, exc)
            return False

    async def _maybe_react(self, event: Any) -> None:
        """Apply a configured auto-reaction to a channel post."""
        chat = getattr(event, "chat", None)
        username = getattr(chat, "username", None) if chat else None
        if not username:
            return

        now = time.monotonic()
        if now - self._reaction_cache_at > _REACTION_CACHE_TTL:
            try:
                self._reaction_cache = await self.db.list_reactions()
                self._reaction_cache_at = now
            except Exception:
                logger.debug("Could not refresh reaction cache", exc_info=True)
                return

        emoji = self._reaction_cache.get(username.lower())
        if not emoji:
            return

        try:
            await self.client(
                functions.messages.SendReactionRequest(
                    peer=event.chat_id,
                    msg_id=event.message.id,
                    reaction=[types.ReactionEmoji(emoticon=emoji)],
                )
            )
        except FloodWaitError as exc:
            logger.warning("Rate limited while reacting; pausing %ss", exc.seconds)
        except Exception as exc:
            logger.debug("Reaction failed in @%s: %s", username, exc)

    def invalidate_reaction_cache(self) -> None:
        self._reaction_cache_at = 0.0

    # -- messaging helpers -------------------------------------------------

    async def reply(self, event: Any, text: str, **kwargs: Any) -> Any:
        """Reply, splitting oversized messages instead of letting them fail."""
        chunks = chunk_text(text, TELEGRAM_LIMIT)
        if not chunks:
            return None
        first = await event.reply(chunks[0], **kwargs)
        for chunk in chunks[1:]:
            await asyncio.sleep(0.3)
            await event.respond(chunk, **kwargs)
        return first

    async def edit(self, message: Any, text: str, **kwargs: Any) -> Any:
        """Edit a message, tolerating the no-op case."""
        try:
            return await message.edit(text, **kwargs)
        except MessageNotModifiedError:
            return message
        except FloodWaitError as exc:
            logger.warning("Rate limited on edit; sleeping %ss", exc.seconds)
            await asyncio.sleep(min(exc.seconds, 60))
            return message
        except Exception as exc:
            logger.debug("Edit failed: %s", exc)
            return message

    async def confirm(
        self,
        event: Any,
        prompt: str,
        *,
        timeout: float = 30.0,
    ) -> bool:
        """Ask for a yes/no confirmation in-chat.

        The original implementation registered a *new* Telethon event handler
        per confirmation and removed it in a ``finally`` that could be skipped,
        leaking handlers. This uses a future keyed by (chat, sender) instead.
        """
        key = (event.chat_id, event.sender_id)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self.pending_confirmations[key] = future

        message = await event.reply(
            f"{prompt}\n\nReply `yes` within {int(timeout)}s to confirm, or `no` to cancel."
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            await self.edit(message, "⏳ Timed out — cancelled.")
            return False
        finally:
            self.pending_confirmations.pop(key, None)

    # -- background --------------------------------------------------------

    async def _restore_timers(self) -> tuple[int, int]:
        from .plugins.timers import restore_timers

        try:
            return await restore_timers(self)
        except Exception:
            logger.exception("Could not restore timers")
            return 0, 0

    async def _janitor(self) -> None:
        """Periodic housekeeping: temp files and stale timer rows."""
        while True:
            try:
                await asyncio.sleep(600)
                removed = cleanup_old_files(
                    self.config.downloads_dir, self.config.temp_ttl_minutes
                )
                if removed:
                    logger.debug("Janitor removed %d stale temp file(s)", removed)
                await self.db.purge_finished_timers(older_than_days=7)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Janitor pass failed", exc_info=True)
