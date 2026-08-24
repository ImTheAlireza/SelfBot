"""Every Telegram command flag uses exactly one leading dash."""

from __future__ import annotations

import pytest

from conftest import FakeEvent, FakeMessage


@pytest.mark.parametrize(
    ("text", "reply_required"),
    [
        ("summarize 5 --brief", False),
        ("summarize 5 --length detailed", False),
        ("summarize 5 --style meeting", False),
        ('summarize 5 --focus "deadlines"', False),
        ("ai add https://api.example.com/v1 sk-test --model demo", False),
        ("backup --include-secrets", False),
        ("backup --file copy.json", False),
        ("restore --force", True),
        ("del 10 --me", False),
        ("delto --me", True),
        ("plugin install https://example.com/repo.git --trust", False),
        ('setautoreply contain "hello" "hi" --nr', False),
        ("remautoreply --allchats", False),
        ("search invoice --here", False),
        ("stick --save hello", False),
        ("qr hello --size 5", False),
        ("selfwlc off --all", False),
    ],
)
async def test_double_dash_command_flags_are_rejected(
    bot, text: str, reply_required: bool
) -> None:
    event = FakeEvent(
        raw_text=text,
        is_reply=reply_required,
        reply_message=FakeMessage(id=10) if reply_required else None,
    )

    assert await bot.registry.dispatch(bot, event, text)

    output = " ".join(event.replies)
    assert "Flags use one dash" in output
    assert "Use `-" in output
