"""Logging configuration, including a non-blocking Telegram log sink.

The original ``TelegramLogHandler`` called ``loop.create_task`` from
``emit()``, which is invoked synchronously from arbitrary threads. When the
loop was not running the log line was silently dropped, and because the handler
logged its own failures it could recurse. This version pushes onto an
``asyncio.Queue`` drained by a single background task, and never logs from
inside the handler.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any, ClassVar

from .utils.text import chunk_text

__all__ = ["TelegramLogHandler", "setup_logging"]

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Telethon is extremely chatty at DEBUG/INFO.
_NOISY_LOGGERS = ("telethon", "asyncio", "aiosqlite", "aiohttp.access")


class _ColourFormatter(logging.Formatter):
    COLOURS: ClassVar[dict[int, str]] = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        colour = self.COLOURS.get(record.levelno)
        return f"{colour}{message}{self.RESET}" if colour else message


class TelegramLogHandler(logging.Handler):
    """Mirror log records into a Telegram channel without blocking."""

    def __init__(self, chat_id: int, *, max_queue: int = 500) -> None:
        super().__init__()
        self.chat_id = chat_id
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_queue)
        self._task: asyncio.Task[None] | None = None
        self._client: Any = None
        self._dropped = 0

    def attach(self, client: Any) -> None:
        """Bind a Telethon client and start draining the queue."""
        self._client = client
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain(), name="telegram-log-sink")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def emit(self, record: logging.LogRecord) -> None:
        # Never mirror our own failures: that is how you build an infinite loop.
        if record.name.startswith("selfbot.logging_setup"):
            return
        try:
            message = self.format(record)
        except Exception:
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self._dropped += 1

    async def _drain(self) -> None:
        while True:
            try:
                message = await self._queue.get()
                if self._client is None:
                    continue
                for chunk in chunk_text(message, 3900):
                    try:
                        await self._client.send_message(
                            self.chat_id, f"```\n{chunk}\n```", parse_mode="markdown"
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(0.4)  # stay under Telegram's rate limit
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1)


def setup_logging(
    *,
    level: str = "INFO",
    log_file: Path | None = None,
    colour: bool | None = None,
) -> None:
    """Configure root logging with console and optional rotating file output."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # handlers filter; root stays permissive

    for handler in list(root.handlers):
        root.removeHandler(handler)

    if colour is None:
        colour = sys.stderr.isatty()

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(
        (_ColourFormatter if colour else logging.Formatter)(_FORMAT, _DATE_FORMAT)
    )
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_FORMAT, _DATE_FORMAT))
        root.addHandler(file_handler)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
