"""Command registry and dispatcher.

The original bot kept a hand-maintained ``command_map`` dict and always invoked
handlers as ``handler(event, *args)`` — which raised ``TypeError`` for the nine
handlers declared as ``def handler(event)``. Here, commands declare their own
argument policy and the dispatcher validates *before* calling, so a mistake
produces a usage hint instead of a stack trace.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .errors import CommandError, PermissionDeniedError, UsageError

logger = logging.getLogger(__name__)

__all__ = ["Command", "CommandRegistry", "Context", "command", "registry"]


@dataclass(slots=True)
class Context:
    """Everything a handler needs, passed as a single object.

    Handlers receive ``(ctx)`` rather than a bare Telethon event so they can
    reach the bot, config, database and parsed arguments without touching
    module-level globals.
    """

    event: Any
    bot: Any
    args: list[str]
    raw_args: str
    command: str

    @property
    def client(self) -> Any:
        return self.bot.client

    @property
    def config(self) -> Any:
        return self.bot.config

    @property
    def db(self) -> Any:
        return self.bot.db

    @property
    def sender_id(self) -> int:
        return self.event.sender_id

    @property
    def chat_id(self) -> int:
        return self.event.chat_id

    @property
    def is_sudo(self) -> bool:
        return self.bot.is_sudo(self.event)

    async def reply(self, text: str, **kwargs: Any) -> Any:
        """Reply, transparently splitting messages over Telegram's limit."""
        return await self.bot.reply(self.event, text, **kwargs)

    async def respond(self, text: str, **kwargs: Any) -> Any:
        return await self.event.respond(text, **kwargs)

    async def get_reply_message(self) -> Any:
        return await self.event.get_reply_message()

    def require_args(self, count: int = 1, usage: str | None = None) -> None:
        if len(self.args) < count:
            raise UsageError(usage or f"Usage: `{self.command} <arguments>`")

    def arg(self, index: int, default: str | None = None) -> str | None:
        return self.args[index] if index < len(self.args) else default


Handler = Callable[[Context], Awaitable[Any]]


@dataclass(slots=True)
class Command:
    """A registered command."""

    name: str
    handler: Handler
    help: str = ""
    usage: str = ""
    category: str = "General"
    aliases: tuple[str, ...] = ()
    sudo_only: bool = False
    requires_reply: bool = False
    min_args: int = 0
    max_args: int | None = None
    examples: tuple[str, ...] = ()
    hidden: bool = False

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def usage_text(self, prefix: str = "") -> str:
        return self.usage or f"{prefix}{self.name}"

    def format_help(self, prefix: str = "") -> str:
        lines = [f"**{prefix}{self.name}** — {self.help or 'No description.'}"]
        if self.aliases:
            lines.append("Aliases: " + ", ".join(f"`{prefix}{a}`" for a in self.aliases))
        lines.append(f"Usage: `{self.usage_text(prefix)}`")
        if self.examples:
            lines.append("Examples:\n" + "\n".join(f"  `{e}`" for e in self.examples))
        if self.sudo_only:
            lines.append("_Owner only._")
        return "\n".join(lines)


class CommandRegistry:
    """Holds commands and routes an incoming message to one of them."""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._by_name: dict[str, Command] = {}

    # -- registration ------------------------------------------------------

    def register(self, command: Command) -> Command:
        for name in command.names:
            key = name.lower()
            if key in self._commands:
                raise ValueError(
                    f"Command {name!r} already registered by "
                    f"{self._commands[key].handler.__qualname__}"
                )
            self._commands[key] = command
        self._by_name[command.name] = command
        return command

    def command(self, name: str, **kwargs: Any) -> Callable[[Handler], Handler]:
        """Decorator form of :meth:`register`."""

        def decorator(func: Handler) -> Handler:
            if not inspect.iscoroutinefunction(func):
                raise TypeError(f"Handler for {name!r} must be async")
            doc = inspect.getdoc(func) or ""
            kwargs.setdefault("help", doc.split("\n", 1)[0])
            self.register(Command(name=name, handler=func, **kwargs))
            return func

        return decorator

    # -- lookup ------------------------------------------------------------

    def get(self, name: str) -> Command | None:
        return self._commands.get(name.lower())

    def all(self) -> list[Command]:
        return sorted(self._by_name.values(), key=lambda c: (c.category, c.name))

    def by_category(self) -> dict[str, list[Command]]:
        grouped: dict[str, list[Command]] = {}
        for cmd in self.all():
            if not cmd.hidden:
                grouped.setdefault(cmd.category, []).append(cmd)
        return grouped

    def __len__(self) -> int:
        return len(self._by_name)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.lower() in self._commands

    # -- dispatch ----------------------------------------------------------

    @staticmethod
    def split_args(raw: str) -> list[str]:
        """Split arguments, honouring quotes but never raising.

        ``shlex`` gives quote support (``rename "my file"``); if the user leaves
        a quote dangling we fall back to a plain split instead of erroring.
        """
        if not raw:
            return []
        try:
            return shlex.split(raw)
        except ValueError:
            return raw.split()

    async def dispatch(self, bot: Any, event: Any, text: str) -> bool:
        """Parse and run a command. Returns True when one was handled."""
        prefix = bot.config.command_prefix
        stripped = text.strip()

        if prefix:
            if not stripped.startswith(prefix):
                return False
            stripped = stripped[len(prefix):].lstrip()

        if not stripped:
            return False

        head, _, rest = stripped.partition(" ")
        command = self.get(head)
        if command is None:
            return False

        raw_args = rest.strip()
        args = self.split_args(raw_args)
        ctx = Context(
            event=event,
            bot=bot,
            args=args,
            raw_args=raw_args,
            command=head.lower(),
        )

        metrics = getattr(bot, "metrics", None)
        try:
            await self._invoke(command, ctx)
            if metrics is not None:
                metrics.incr("commands_run")
        except CommandError as exc:
            if metrics is not None:
                metrics.incr("commands_failed")
            await _safe_reply(ctx, exc.user_message())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if metrics is not None:
                metrics.incr("commands_failed")
            logger.exception("Unhandled error in command %s", command.name)
            await _safe_reply(
                ctx,
                f"💥 `{command.name}` crashed: `{type(exc).__name__}: {exc}`",
            )
        return True

    async def _invoke(self, command: Command, ctx: Context) -> None:
        prefix = ctx.bot.config.command_prefix

        if command.sudo_only and not ctx.is_sudo:
            raise PermissionDeniedError("Only the bot owner can use this command.")

        if command.requires_reply and not ctx.event.is_reply:
            raise UsageError(
                f"Reply to a message to use this.\nUsage: `{command.usage_text(prefix)}`"
            )

        if len(ctx.args) < command.min_args:
            raise UsageError(f"Usage: `{command.usage_text(prefix)}`")

        if command.max_args is not None and len(ctx.args) > command.max_args:
            raise UsageError(
                f"Too many arguments.\nUsage: `{command.usage_text(prefix)}`"
            )

        await command.handler(ctx)


async def _safe_reply(ctx: Context, text: str) -> None:
    try:
        await ctx.reply(text)
    except Exception:
        logger.exception("Failed to deliver error message to user")


#: The global registry that plugin modules populate at import time.
registry = CommandRegistry()
command = registry.command
