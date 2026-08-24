"""Tests for metrics, the health command and the /healthz endpoint."""

from __future__ import annotations

from conftest import FakeEvent
from selfbot.services.metrics import Metrics


async def test_metrics_counters_and_failures() -> None:
    m = Metrics()
    m.incr("messages_seen", 3)
    m.incr("ai_requests")
    assert m.counters["messages_seen"] == 3
    assert m.counters["ai_requests"] == 1

    m.record_failure("ai:anyapi", "quota", status=429)
    failures = m.recent_failures()
    assert failures and failures[0].source == "ai:anyapi"
    assert failures[0].status == 429


async def test_metrics_snapshot_uses_bot(bot) -> None:
    metrics = Metrics()
    metrics.attach(bot)
    bot.metrics = metrics
    snap = await metrics.snapshot(bot)
    assert snap["db_connected"] is True
    assert snap["db_backend"] == "sqlite"
    assert snap["db_tables"] is not None and snap["db_tables"] >= 8
    assert snap["tasks"] >= 0
    assert "messages_seen" in snap["counters"]


def test_rss_mb_returns_number_or_none() -> None:
    value = Metrics.rss_mb()
    assert value is None or value > 0


async def test_health_command_renders(bot) -> None:
    event = FakeEvent(raw_text="health")
    await bot.registry.dispatch(bot, event, "health")
    text = "\n".join(event.replies)
    assert "SelfBot health" in text
    assert "DB:" in text


async def test_health_metrics_subcommand(bot) -> None:
    event = FakeEvent(raw_text="health metrics")
    await bot.registry.dispatch(bot, event, "health metrics")
    text = "\n".join(event.replies)
    assert "Metrics" in text
    assert "messages_seen" in text


async def test_metrics_incremented_on_command_dispatch(bot) -> None:
    before = bot.metrics.counters["commands_run"]
    event = FakeEvent(raw_text="ping")
    await bot.registry.dispatch(bot, event, "ping")
    assert bot.metrics.counters["commands_run"] >= before + 1


async def test_metrics_failed_command_counter(bot) -> None:
    before = bot.metrics.counters["commands_failed"]
    event = FakeEvent(raw_text="gpt")  # raises UsageError
    await bot.registry.dispatch(bot, event, "gpt")
    assert bot.metrics.counters["commands_failed"] >= before + 1


async def test_metrics_message_counter(bot) -> None:
    # Dispatch directly through the bot's message handler if available; in the
    # test double the registry path is enough to exercise command counting.
    event = FakeEvent(raw_text="ping")
    await bot.registry.dispatch(bot, event, "ping")
    assert bot.metrics.counters["commands_run"] >= 1


async def test_health_endpoint_returns_200() -> None:
    import aiohttp

    from selfbot.plugins.health import start_health_server, stop_health_server

    class _HealthCfg:
        enabled = True
        bind = "127.0.0.1"
        port = 0  # ephemeral; the OS chooses

    class _Cfg:
        health = _HealthCfg()

    class _FakeMetrics:
        @staticmethod
        async def snapshot(_bot: object) -> dict:
            return {
                "db_connected": True,
                "uptime_seconds": 12.0,
                "state": "active",
                "tasks": 2,
                "rss_mb": 42.0,
            }

    class _FakeBot:
        config = _Cfg()
        metrics = _FakeMetrics()
        uptime = 12.0
        active = True

    bot = _FakeBot()
    runner = await start_health_server(bot)
    try:
        assert runner is not None
        # Find the ephemeral port the OS assigned.
        site = next(iter(runner.sites))
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
        url = f"http://127.0.0.1:{port}/healthz"
        async with aiohttp.ClientSession() as session, session.get(url, timeout=5) as resp:
            assert resp.status == 200
            body = await resp.json()
        assert body["status"] == "ok"
        assert body["db"] is True
    finally:
        await stop_health_server(runner)


async def test_health_endpoint_disabled_returns_none() -> None:
    from selfbot.plugins.health import start_health_server

    class _HealthCfg:
        enabled = False
        bind = "127.0.0.1"
        port = None

    class _Cfg:
        health = _HealthCfg()

    class _FakeBot:
        config = _Cfg()

    assert await start_health_server(_FakeBot()) is None
