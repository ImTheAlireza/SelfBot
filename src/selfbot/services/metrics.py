"""Lightweight in-process metrics for the health command and /healthz.

Counters are incremented from hot paths (messages, commands, HTTP, AI) without
any locking overhead beyond the GIL; gauges are computed on read from the live
:class:`~selfbot.bot.SelfBot`. A bounded deque keeps the most recent API
failures so ``health`` can show *what* broke recently, not just a count.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["FailureEvent", "Metrics"]

logger = logging.getLogger(__name__)

_FAILURE_RING = 100


@dataclass(slots=True)
class FailureEvent:
    when: float
    source: str
    status: int | None
    message: str

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.when)


@dataclass
class Metrics:
    """Counters, an API-failure ring buffer and an event-loop lag probe."""

    started_monotonic: float = field(default_factory=time.monotonic)
    started_wall: float = field(default_factory=time.time)
    counters: dict[str, int] = field(
        default_factory=lambda: {
            "messages_seen": 0,
            "commands_run": 0,
            "commands_failed": 0,
            "ai_requests": 0,
            "ai_failures": 0,
            "http_requests": 0,
            "http_failures": 0,
        }
    )
    failures: deque[FailureEvent] = field(
        default_factory=lambda: deque(maxlen=_FAILURE_RING)
    )
    _lag_samples: deque[float] = field(
        default_factory=lambda: deque(maxlen=60)
    )
    _lag_task: Any = None
    _bot: Any = None

    # -- wiring -----------------------------------------------------------

    def attach(self, bot: Any) -> None:
        self._bot = bot

    def start_loop_probe(self) -> None:
        """Sample event-loop latency every 10s. Safe to call once."""
        if self._lag_task is not None:
            return
        import asyncio

        async def probe() -> None:
            while True:
                try:
                    start = time.perf_counter()
                    await asyncio.sleep(10.0)
                    self._lag_samples.append(
                        (time.perf_counter() - start - 10.0) * 1000.0
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("metrics loop probe failed", exc_info=True)

        self._lag_task = asyncio.ensure_future(probe())
        self._lag_task.set_name("metrics-lag-probe")

    async def stop(self) -> None:
        if self._lag_task is not None:
            self._lag_task.cancel()
            try:
                await self._lag_task
            except Exception:
                pass
            self._lag_task = None

    # -- counters ---------------------------------------------------------

    def incr(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def record_failure(
        self,
        source: str,
        message: str,
        *,
        status: int | None = None,
    ) -> None:
        self.failures.append(
            FailureEvent(
                when=time.monotonic(),
                source=source,
                status=status,
                message=message[:300],
            )
        )

    def recent_failures(self, limit: int = 10) -> list[FailureEvent]:
        items = list(self.failures)
        items.reverse()
        return items[:limit]

    # -- gauges -----------------------------------------------------------

    def event_loop_lag_ms(self) -> float:
        if not self._lag_samples:
            return 0.0
        return max(0.0, sum(self._lag_samples) / len(self._lag_samples))

    @staticmethod
    def rss_mb() -> float | None:
        """Resident set size in MiB, or None when unavailable."""
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Linux reports kilobytes, macOS reports bytes.
            if os.uname().sysname == "Darwin":  # pragma: no cover - mac only
                return usage / (1024 * 1024)
            return usage / 1024.0
        except Exception:
            return None

    def task_count(self, bot: Any | None = None) -> int:
        target = bot or self._bot
        if target is None:
            return 0
        count = len(getattr(target, "_background", set()))
        for attr in ("timer_tasks", "spam_tasks", "challenge_tasks"):
            count += len(getattr(target, attr, {}))
        return count

    async def db_ok(self, bot: Any | None = None) -> bool:
        target = bot or self._bot
        if target is None:
            return False
        try:
            await target.db.fetch_one("SELECT 1 AS ok")
            return True
        except Exception:
            return False

    async def db_table_count(self, bot: Any | None = None) -> int | None:
        target = bot or self._bot
        if target is None:
            return None
        try:
            if target.db.backend == "sqlite":
                rows = await target.db.fetch_all(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            else:
                rows = await target.db.fetch_all(
                    "SELECT table_name AS name FROM information_schema.tables "
                    "WHERE table_schema = DATABASE()"
                )
            return len(rows)
        except Exception:
            return None

    async def snapshot(self, bot: Any | None = None) -> dict[str, Any]:
        target = bot or self._bot
        rss = self.rss_mb()
        return {
            "uptime_seconds": (
                time.monotonic() - self.started_monotonic
                if target is None
                else target.uptime
            ),
            "state": "active" if getattr(target, "active", False) else "paused",
            "tasks": self.task_count(target),
            "rss_mb": round(rss, 1) if rss is not None else None,
            "event_loop_lag_ms": round(self.event_loop_lag_ms(), 1),
            "db_connected": await self.db_ok(target),
            "db_backend": getattr(getattr(target, "db", None), "backend", None),
            "db_tables": await self.db_table_count(target),
            "counters": dict(self.counters),
            "recent_failures": [
                {
                    "source": f.source,
                    "status": f.status,
                    "message": f.message,
                    "age_seconds": round(f.age_seconds, 1),
                }
                for f in self.recent_failures(10)
            ],
        }


# A module-level no-op so call sites can always do ``metrics.incr(...)`` even
# before the bot has constructed one.
class _NoopMetrics:
    def incr(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def record_failure(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __getattr__(self, _name: str) -> Callable[..., None]:
        def _noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        return _noop


NOOP = _NoopMetrics()
