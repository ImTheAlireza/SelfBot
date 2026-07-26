"""Dispatcher behaviour: parsing, prefixes, arity and error handling."""

from __future__ import annotations

import pytest

from conftest import FakeEvent
from selfbot.errors import UsageError, ValidationError
from selfbot.registry import Command, CommandRegistry, Context


@pytest.fixture
def isolated() -> CommandRegistry:
    """An empty registry, so tests do not depend on the real plugins."""
    return CommandRegistry()


# ---------------------------------------------------------------------------
# Argument splitting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        ("a b c", ["a", "b", "c"]),
        ('rename "my file.txt"', ["rename", "my file.txt"]),
        ("  spaced   out  ", ["spaced", "out"]),
        ('unbalanced "quote', ["unbalanced", '"quote']),  # falls back, never raises
        ("emoji 🔥 ok", ["emoji", "🔥", "ok"]),
    ],
)
def test_split_args(raw, expected):
    assert CommandRegistry.split_args(raw) == expected


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_invokes_matching_command(bot, isolated):
    calls: list[Context] = []

    @isolated.command("greet")
    async def greet(ctx: Context) -> None:
        """Say hello."""
        calls.append(ctx)

    event = FakeEvent(raw_text="greet world")
    assert await isolated.dispatch(bot, event, event.raw_text)
    assert len(calls) == 1
    assert calls[0].args == ["world"]
    assert calls[0].raw_args == "world"


@pytest.mark.asyncio
async def test_dispatch_ignores_unknown_command(bot, isolated):
    event = FakeEvent(raw_text="nosuchthing")
    assert await isolated.dispatch(bot, event, event.raw_text) is False


@pytest.mark.asyncio
async def test_dispatch_is_case_insensitive(bot, isolated):
    seen = []

    @isolated.command("ping")
    async def ping(ctx: Context) -> None:
        """Ping."""
        seen.append(True)

    event = FakeEvent(raw_text="PiNg")
    assert await isolated.dispatch(bot, event, event.raw_text)
    assert seen


@pytest.mark.asyncio
async def test_prefix_is_enforced_when_configured(bot, isolated):
    bot.config = type(bot.config)(  # rebuild frozen dataclass with a prefix
        **{**bot.config.__dict__, "command_prefix": "."}
    ) if hasattr(bot.config, "__dict__") else bot.config

    seen = []

    @isolated.command("go")
    async def go(ctx: Context) -> None:
        """Go."""
        seen.append(True)

    import dataclasses

    bot.config = dataclasses.replace(bot.config, command_prefix=".")

    assert await isolated.dispatch(bot, FakeEvent(raw_text="go"), "go") is False
    assert await isolated.dispatch(bot, FakeEvent(raw_text=".go"), ".go") is True
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_min_args_produces_usage_hint(bot, isolated):
    @isolated.command("need", min_args=2, usage="need <a> <b>")
    async def need(ctx: Context) -> None:
        """Needs two."""
        raise AssertionError("should not run")

    event = FakeEvent(raw_text="need one")
    await isolated.dispatch(bot, event, event.raw_text)
    assert any("need <a> <b>" in r for r in event.replies)


@pytest.mark.asyncio
async def test_max_args_is_enforced(bot, isolated):
    @isolated.command("only", max_args=1, usage="only <x>")
    async def only(ctx: Context) -> None:
        """One arg max."""
        raise AssertionError("should not run")

    event = FakeEvent(raw_text="only a b c")
    await isolated.dispatch(bot, event, event.raw_text)
    assert any("Too many arguments" in r for r in event.replies)


@pytest.mark.asyncio
async def test_requires_reply_is_enforced(bot, isolated):
    @isolated.command("needsreply", requires_reply=True)
    async def needsreply(ctx: Context) -> None:
        """Needs a reply."""
        raise AssertionError("should not run")

    event = FakeEvent(raw_text="needsreply", is_reply=False)
    await isolated.dispatch(bot, event, event.raw_text)
    assert any("Reply to a message" in r for r in event.replies)


@pytest.mark.asyncio
async def test_command_error_is_shown_to_user(bot, isolated):
    @isolated.command("boom")
    async def boom(ctx: Context) -> None:
        """Raises."""
        raise ValidationError("that input is wrong")

    event = FakeEvent(raw_text="boom")
    await isolated.dispatch(bot, event, event.raw_text)
    assert any("that input is wrong" in r for r in event.replies)


@pytest.mark.asyncio
async def test_unexpected_exception_is_contained(bot, isolated):
    """A crashing handler must not propagate into the event loop."""

    @isolated.command("crash")
    async def crash(ctx: Context) -> None:
        """Crashes."""
        raise RuntimeError("kaboom")

    event = FakeEvent(raw_text="crash")
    await isolated.dispatch(bot, event, event.raw_text)  # must not raise
    assert any("crashed" in r for r in event.replies)
    assert any("kaboom" in r for r in event.replies)


@pytest.mark.asyncio
async def test_registry_requires_async_handlers(isolated):
    with pytest.raises(TypeError, match="must be async"):

        @isolated.command("sync")
        def sync_handler(ctx: Context) -> None:  # type: ignore[misc]
            ...


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------


def test_context_arg_accessor(make_ctx):
    ctx = make_ctx("test", "alpha beta")
    assert ctx.arg(0) == "alpha"
    assert ctx.arg(1) == "beta"
    assert ctx.arg(5) is None
    assert ctx.arg(5, "fallback") == "fallback"


def test_context_require_args(make_ctx):
    ctx = make_ctx("test", "")
    with pytest.raises(UsageError):
        ctx.require_args(1)


def test_command_help_formatting():
    async def handler(ctx: Context) -> None:  # pragma: no cover
        ...

    cmd = Command(
        name="demo",
        handler=handler,
        help="Do a thing.",
        usage="demo <x>",
        aliases=("d",),
        examples=("demo 1",),
        sudo_only=True,
    )
    text = cmd.format_help()
    assert "demo" in text
    assert "Do a thing." in text
    assert "Owner only" in text
    assert "demo 1" in text
