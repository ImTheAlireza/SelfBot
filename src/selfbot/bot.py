"""The SelfBot application object.

Owns the Telethon client, the database, the command registry and the event
handlers. Everything that used to be a module-level global in ``self.py`` now
lives on an instance, which makes the bot testable and lets state be reset
cleanly between runs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from collections.abc import Awaitable
from datetime import datetime, timezone
from typing import Any

from telethon import TelegramClient, events, functions, types, utils
from telethon.errors import FloodWaitError, MessageNotModifiedError

from .config import Config
from .db import Database
from .errors import ConfigError
from .logging_setup import TelegramLogHandler
from .registry import CommandRegistry
from .registry import registry as global_registry
from .utils.files import cleanup_old_files
from .utils.http import close_client, get_client
from .utils.text import TELEGRAM_LIMIT, chunk_text

logger = logging.getLogger(__name__)

__all__ = ["SelfBot"]

_REACTION_CACHE_TTL = 300.0
_WELCOME_DEDUP_TTL = 300.0
_WELCOME_POLL_INTERVAL = 30.0


class SelfBot:
    """Wires together the client, database and command dispatch."""

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

        # Mutable runtime state, previously scattered across module globals.
        self.active = True
        self.me: Any = None
        self.spam_tasks: dict[int, asyncio.Task[Any]] = {}
        self.spam_cooldowns: dict[int, float] = {}
        self.timer_tasks: dict[str, asyncio.Task[Any]] = {}
        self.zip_queue: dict[int, list[Any]] = {}
        self.active_sticker_pack: dict[int, dict[str, Any]] = {}
        self.challenge_tasks: dict[int, Any] = {}
        self.pending_confirmations: dict[tuple[int, int], asyncio.Future[bool]] = {}

        self._auto_reply_cache: dict[int, list[Any]] = {}
        self._reaction_cache: dict[str, str] = {}
        self._reaction_cache_at = 0.0
        self._recent_welcomes: dict[tuple[int, int], float] = {}
        self._welcome_log_ids: dict[int, int] = {}
        self._welcome_poll_warned: set[int] = set()
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
        self._spawn(self._welcome_watcher(), name="welcome-watcher")

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
        for state in list(self.challenge_tasks.values()):
            state.cancel()
        for task in list(self._background):
            task.cancel()

        pending = [
            *self.timer_tasks.values(),
            *self.spam_tasks.values(),
            *[s.task for s in self.challenge_tasks.values() if s.task],
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
        self.client.add_event_handler(self._on_chat_action, events.ChatAction())
        # Telethon's ChatAction does not cover "join request approved" service
        # messages (MessageActionChatJoinedByRequest), so catch those raw.
        self.client.add_event_handler(
            self._on_raw_update,
            events.Raw((types.UpdateNewMessage, types.UpdateNewChannelMessage)),
        )

    async def _on_chat_action(self, event: Any) -> None:
        try:
            await self._maybe_welcome(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unhandled error while processing chat action")

    async def _on_raw_update(self, update: Any) -> None:
        try:
            await self._maybe_welcome_join_request(update)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unhandled error while processing raw update")

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

        # Real-time collision detection for active challenge sessions
        challenge_state = self.challenge_tasks.get(event.chat_id)
        if challenge_state and not challenge_state.is_cancelled:
            reply_to = getattr(event.message, "reply_to", None)
            reply_to_msg_id = getattr(reply_to, "reply_to_msg_id", None)
            if reply_to_msg_id is None:
                reply_to_msg_id = getattr(event.message, "reply_to_msg_id", None)

            if reply_to_msg_id == challenge_state.challenge_msg_id:
                from .plugins.challenge import extract_mentions

                uids, unames = extract_mentions(event.message)
                if uids or unames:
                    challenge_state.tagged_user_ids.update(uids)
                    challenge_state.tagged_usernames.update(unames)
                    challenge_state.tagged_by_others_count += len(uids | unames)

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

        handled = False
        # Always allow the owner to switch the bot back on.
        if (is_own or await self.is_authorized(event)) and (
            self.active or (is_own and self._is_reactivation(text))
        ):
            handled = await self.registry.dispatch(self, event, text)

        if not is_own and not handled:
            await self._try_auto_reply(event, text)

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

    async def _get_auto_replies(self, chat_id: int) -> list[Any]:
        cached = self._auto_reply_cache.get(chat_id)
        if cached is not None:
            return cached

        rules = await self.db.list_auto_replies(chat_id)
        self._auto_reply_cache[chat_id] = rules
        return rules

    def _contains_isolated_phrase(self, text: str, trigger: str) -> bool:
        """True when ``trigger`` appears as a standalone word/phrase.

        `contain` should not fire for partial word matches like `سلام` inside
        `سلامتی`. Python's ``\b`` is close, but we also treat ZWNJ/ZWJ as part
        of a word so Persian compounds do not produce false positives.
        """
        trigger = trigger.strip()
        if not trigger:
            return False

        pattern = re.compile(
            rf"(?<![\w\u200c\u200d]){re.escape(trigger)}(?![\w\u200c\u200d])",
            flags=re.IGNORECASE,
        )
        return pattern.search(text.strip()) is not None

    async def _try_auto_reply(self, event: Any, text: str) -> bool:
        try:
            rules = await self._get_auto_replies(event.chat_id)
        except Exception:
            logger.debug("Could not load auto-replies for chat %s", event.chat_id, exc_info=True)
            return False

        if not rules:
            return False

        candidate = text.strip()
        folded_candidate = candidate.casefold()
        for rule in rules:
            trigger = rule.trigger.strip()
            matched = (
                folded_candidate == trigger.casefold()
                if rule.mode == "match"
                else self._contains_isolated_phrase(candidate, trigger)
            )
            if not matched:
                continue

            # Check reply condition.
            cond = getattr(rule, "reply_condition", "any") or "any"
            if cond == "nr":
                # Only reply if the message is NOT a reply.
                if getattr(event.message, "reply_to", None) is not None:
                    continue
            elif cond == "sr":
                # Only reply if the message is a reply to me.
                reply_to = getattr(event.message, "reply_to", None)
                if reply_to is None:
                    continue
                # Check if the replied-to message is from the bot's own account.
                reply_to_msg_id = getattr(reply_to, "reply_to_msg_id", None)
                if reply_to_msg_id is None:
                    continue
                try:
                    replied_msg = await self.client.get_messages(event.chat_id, ids=reply_to_msg_id)
                    if replied_msg is None or getattr(replied_msg, "sender_id", None) != self.me.id:
                        continue
                except Exception:
                    logger.debug("Could not fetch replied-to message for -sr check", exc_info=True)
                    continue

            try:
                await event.reply(rule.reply_text)
                logger.debug(
                    "Auto-replied in chat %s using %s %r (condition=%s)",
                    event.chat_id,
                    rule.mode,
                    rule.trigger,
                    cond,
                )
                return True
            except Exception as exc:
                logger.warning(
                    "Could not auto-reply in chat %s for %s %r: %s",
                    event.chat_id,
                    rule.mode,
                    rule.trigger,
                    exc,
                )
                return False

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

    async def _maybe_welcome(self, event: Any) -> None:
        """Greet users who join or are added, when this chat has welcome on."""
        if not self.active:
            return
        if not (getattr(event, "user_joined", False) or getattr(event, "user_added", False)):
            return

        chat_id = getattr(event, "chat_id", None)
        if chat_id is None:
            return

        welcome = await self._enabled_welcome(chat_id)
        if welcome is None:
            return

        try:
            users = await event.get_users()
        except Exception:
            logger.debug("Could not resolve joining users in chat %s", chat_id, exc_info=True)
            users = []
        if not users:
            return

        await self._welcome_users(welcome, chat_id, users, reply_event=event)

    async def _maybe_welcome_join_request(self, update: Any) -> None:
        """Greet users whose join request was approved by this account.

        Telethon's ``ChatAction`` event does not recognise the
        ``MessageActionChatJoinedByRequest`` service message, so approvals
        arrive here through a raw update instead.
        """
        if not self.active:
            return

        message = getattr(update, "message", None)
        if not isinstance(message, types.MessageService):
            return
        if not isinstance(message.action, types.MessageActionChatJoinedByRequest):
            return
        if message.from_id is None:
            return

        chat_id = utils.get_peer_id(message.peer_id)
        welcome = await self._enabled_welcome(chat_id)
        if welcome is None:
            return

        user_id = utils.get_peer_id(message.from_id)

        # Check if the join request was approved by this account.
        if not await self._is_approved_by_me(chat_id, user_id):
            logger.info(
                "Welcome: skipping join request for user %s in chat %s (not approved by me)",
                user_id,
                chat_id,
            )
            return

        user = await self._resolve_user(update, chat_id, user_id, message.id)
        if user is None:
            logger.warning(
                "Welcome: could not resolve approved user %s in chat %s", user_id, chat_id
            )
            return

        logger.info("Welcome: join request approved by me for user %s in chat %s", user_id, chat_id)
        await self._welcome_users(
            welcome, chat_id, [user], reply_to_msg_id=getattr(message, "id", None)
        )

    async def _is_approved_by_me(self, chat_id: int, user_id: int) -> bool:
        """Check if a join request was approved by the logged-in account."""
        my_id = getattr(self.me, "id", None)
        if my_id is None:
            return False

        real_id, peer_type = utils.resolve_id(chat_id)
        if peer_type is not types.PeerChannel:
            return False

        try:
            result = await self.client(
                functions.channels.GetAdminLogRequest(
                    channel=real_id,
                    q="",
                    max_id=0,
                    min_id=0,
                    limit=10,
                    events_filter=types.ChannelAdminLogEventsFilter(invite=True),
                )
            )
            for event in getattr(result, "events", []) or []:
                if event.user_id == user_id and isinstance(
                    event.action, types.ChannelAdminLogEventActionParticipantJoinByRequest
                ):
                    return bool(event.action.approved_by == my_id)
        except Exception:
            logger.debug(
                "Could not check join request approver in admin log for chat %s",
                chat_id,
                exc_info=True,
            )
            return False

        return False

    async def _resolve_user(
        self, update: Any, chat_id: int, user_id: int, msg_id: int | None
    ) -> Any:
        """Resolve a user id into a full User, trying the cheapest source first.

        A user who just joined is usually *not* in the session's entity cache
        yet, so ``get_entity(int)`` alone tends to fail with "could not find
        the input entity". The raw update however carries the user object in
        ``_entities``, and the service message can be re-fetched with entities
        as a last resort.
        """
        # 1. Users delivered alongside the raw update.
        entities = getattr(update, "_entities", None) or {}
        user = entities.get(user_id)
        if isinstance(user, types.User):
            return user

        # 2. The session's entity cache.
        try:
            return await self.client.get_entity(user_id)
        except Exception:
            logger.debug("get_entity(%s) failed", user_id, exc_info=True)

        # 3. Re-fetch the service message; the response includes its sender.
        if msg_id is not None:
            try:
                msg = await self.client.get_messages(chat_id, ids=msg_id)
                if msg is not None:
                    sender = await msg.get_sender()
                    if sender is not None:
                        return sender
            except Exception:
                logger.debug("Refetching message %s failed", msg_id, exc_info=True)
        return None

    async def _welcome_watcher(self) -> None:
        """Poll the admin log of welcome-enabled channels for fresh joins.

        Realtime detection relies on the join service message, which a
        cleaner bot can delete (or the group can hide) before we act on it.
        User accounts do not receive ``UpdateChannelParticipant`` — that
        update is bot-only — but an *admin* account can read the channel's
        admin log, which records every join and join-request approval. The
        dedup guard keeps this from double-greeting users the realtime path
        already handled.
        """
        while True:
            try:
                await asyncio.sleep(_WELCOME_POLL_INTERVAL)
                if not self.active:
                    continue
                await self._poll_welcome_joins()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Welcome watcher pass failed", exc_info=True)

    async def _poll_welcome_joins(self) -> None:
        try:
            welcomes = await self.db.list_welcomes()
        except Exception:
            logger.debug("Could not list welcomes", exc_info=True)
            return

        for welcome in welcomes:
            if not welcome.enabled:
                self._welcome_log_ids.pop(welcome.chat_id, None)
                continue
            real_id, peer_type = utils.resolve_id(welcome.chat_id)
            if peer_type is not types.PeerChannel:
                continue  # legacy small groups have no admin log
            await self._poll_one_admin_log(welcome, real_id)

    async def _poll_one_admin_log(self, welcome: Any, channel_id: int) -> None:
        chat_id = welcome.chat_id
        last_seen = self._welcome_log_ids.get(chat_id)
        try:
            result = await self.client(
                functions.channels.GetAdminLogRequest(
                    channel=channel_id,
                    q="",
                    max_id=0,
                    min_id=last_seen or 0,
                    limit=20 if last_seen else 1,
                    events_filter=types.ChannelAdminLogEventsFilter(join=True, invite=True),
                )
            )
        except FloodWaitError as exc:
            logger.debug("Admin log flood wait %ss for chat %s", exc.seconds, chat_id)
            return
        except Exception as exc:
            if chat_id not in self._welcome_poll_warned:
                self._welcome_poll_warned.add(chat_id)
                logger.warning(
                    "Welcome: cannot read the admin log of chat %s (%s: %s); "
                    "join detection there relies on service messages only",
                    chat_id,
                    type(exc).__name__,
                    exc,
                )
            return

        self._welcome_poll_warned.discard(chat_id)
        events_list = list(getattr(result, "events", []) or [])
        if events_list:
            self._welcome_log_ids[chat_id] = max(e.id for e in events_list)
        elif last_seen is None:
            self._welcome_log_ids[chat_id] = 0

        if last_seen is None:
            return  # first pass only records the position; no back-greeting

        users_by_id = {u.id: u for u in getattr(result, "users", []) or []}
        my_id = getattr(self.me, "id", None)
        join_actions = (
            types.ChannelAdminLogEventActionParticipantJoin,
            types.ChannelAdminLogEventActionParticipantJoinByInvite,
            types.ChannelAdminLogEventActionParticipantJoinByRequest,
        )
        joined: list[Any] = []
        for log_event in sorted(events_list, key=lambda e: e.id):
            if not isinstance(log_event.action, join_actions):
                continue
            if (
                isinstance(log_event.action, types.ChannelAdminLogEventActionParticipantJoinByRequest)
                and (my_id is None or log_event.action.approved_by != my_id)
            ):
                logger.debug(
                    "Welcome: skipping join request for %s; approved by %s (not me %s)",
                    log_event.user_id,
                    getattr(log_event.action, "approved_by", None),
                    my_id,
                )
                continue
            user = users_by_id.get(log_event.user_id)
            if user is None:
                try:
                    user = await self.client.get_entity(log_event.user_id)
                except Exception:
                    logger.debug(
                        "Could not resolve joiner %s from admin log", log_event.user_id,
                        exc_info=True,
                    )
                    continue
            joined.append(user)

        if joined:
            logger.info(
                "Welcome: %d join(s) found in the admin log of chat %s",
                len(joined),
                chat_id,
            )
            await self._welcome_users(welcome, chat_id, joined)

    async def _enabled_welcome(self, chat_id: int) -> Any:
        """The chat's welcome row when it exists and is switched on, else None."""
        try:
            welcome = await self.db.get_welcome(chat_id)
        except Exception:
            logger.debug("Could not load welcome for chat %s", chat_id, exc_info=True)
            return None
        if welcome is None or not welcome.enabled:
            return None
        return welcome

    def _already_welcomed(self, chat_id: int, user_id: int | None) -> bool:
        """Dedup guard: the same join can surface through more than one update."""
        if user_id is None:
            return False
        now = time.monotonic()
        self._recent_welcomes = {
            key: at
            for key, at in self._recent_welcomes.items()
            if now - at < _WELCOME_DEDUP_TTL
        }
        key = (chat_id, user_id)
        if key in self._recent_welcomes:
            return True
        self._recent_welcomes[key] = now
        return False

    async def _welcome_users(
        self,
        welcome: Any,
        chat_id: int,
        users: list[Any],
        *,
        reply_event: Any = None,
        reply_to_msg_id: int | None = None,
    ) -> None:
        """Render and deliver the welcome to each arriving user."""
        from .plugins.welcome import render_welcome

        my_id = getattr(self.me, "id", None)
        for user in users:
            if getattr(user, "bot", False):
                continue
            user_id = getattr(user, "id", None)
            if my_id is not None and user_id == my_id:
                continue  # don't welcome ourselves when we (re)join
            if self._already_welcomed(chat_id, user_id):
                continue

            text = render_welcome(welcome.message, user)
            try:
                # Prefer replying to the join service message; fall back to a
                # plain message when that is not possible.
                try:
                    if reply_event is not None:
                        await reply_event.reply(text)
                    else:
                        await self.client.send_message(
                            chat_id, text, reply_to=reply_to_msg_id
                        )
                except Exception:
                    if reply_event is None and reply_to_msg_id is None:
                        raise
                    await self.client.send_message(chat_id, text)
                logger.debug("Welcomed user %s in chat %s", user_id, chat_id)
            except FloodWaitError as exc:
                logger.warning("Rate limited while welcoming; pausing %ss", exc.seconds)
                return
            except Exception as exc:
                logger.warning("Could not welcome user in chat %s: %s", chat_id, exc)
                return

    def invalidate_auto_reply_cache(self, chat_id: int | None = None) -> None:
        if chat_id is None:
            self._auto_reply_cache.clear()
        else:
            self._auto_reply_cache.pop(chat_id, None)

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
