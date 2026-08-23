"""Tests for memory, gpt edit, summarize and reply-context gpt."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conftest import FakeEvent, FakeReplyTo


@dataclass
class _Resp:
    payload: Any
    status: int = 200

    async def json(self, *, content_type: Any = None) -> Any:
        return self.payload

    async def text(self) -> str:
        return str(self.payload)


@dataclass
class _Http:
    answer: str = "ok"
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def request(self, method: str, url: str, **kwargs: Any) -> _Resp:
        self.calls.append({"method": method, "url": url, **kwargs})
        return _Resp({"choices": [{"message": {"content": self.answer}}]})


@dataclass
class _Sender:
    first_name: str = "Tester"
    username: str = "tester"
    id: int = 4242


@dataclass
class _Replied:
    raw_text: str
    sender_id: int = 4242
    id: int = 555
    photo: Any = None
    document: Any = None
    edits: list[str] = field(default_factory=list)

    async def get_sender(self) -> _Sender:
        return _Sender()

    async def edit(self, text: str, **_kw: Any) -> _Replied:
        self.edits.append(text)
        return self


# --------------------------------------------------------------------------
# memory
# --------------------------------------------------------------------------


async def test_memory_toggle_and_clear(bot) -> None:
    event = FakeEvent(raw_text="memory off")
    await bot.registry.dispatch(bot, event, event.raw_text)
    assert any("off" in r for r in event.replies)

    event = FakeEvent(raw_text="memory status")
    await bot.registry.dispatch(bot, event, event.raw_text)
    assert any("🔴 off" in r for r in event.replies)

    event = FakeEvent(raw_text="memory clear")
    await bot.registry.dispatch(bot, event, event.raw_text)
    assert any("Cleared" in r for r in event.replies)


async def test_memory_turns_validation(bot) -> None:
    event = FakeEvent(raw_text="memory turns 99")
    await bot.registry.dispatch(bot, event, event.raw_text)
    assert any("between 1 and 50" in r for r in event.replies)


# --------------------------------------------------------------------------
# gpt reply context
# --------------------------------------------------------------------------


async def test_gpt_reply_includes_quoted_message(bot) -> None:
    from selfbot.plugins.ai import get_manager

    manager = get_manager(type("C", (), {"bot": bot})())
    bot.ai = manager
    bot.http = _Http(answer="sure")
    replied = _Replied(raw_text="the quoted message")
    event = FakeEvent(raw_text="gpt translate this", is_reply=True, reply_message=replied)
    event.reply_to = FakeReplyTo(reply_to_msg_id=replied.id)
    await bot.registry.dispatch(bot, event, event.raw_text)

    request = bot.http.calls[0]["json"]["messages"]
    user_content = next(m["content"] for m in request if m["role"] == "user")
    assert "the quoted message" in user_content
    assert "translate this" in user_content


# --------------------------------------------------------------------------
# gpt edit
# --------------------------------------------------------------------------


async def test_gpt_edit_refuses_others_messages(bot) -> None:
    bot.me = type("M", (), {"id": 111})()
    replied = _Replied(raw_text="hello", sender_id=999)
    event = FakeEvent(raw_text="gpt edit", is_reply=True, reply_message=replied)
    event.reply_to = FakeReplyTo(reply_to_msg_id=replied.id)
    await bot.registry.dispatch(bot, event, event.raw_text)
    assert any("your own" in r for r in event.replies)


async def test_gpt_edit_edits_own_message(bot) -> None:
    from selfbot.plugins.ai import get_manager

    bot.me = type("M", (), {"id": 4242})()
    manager = get_manager(type("C", (), {"bot": bot})())
    bot.ai = manager
    bot.http = _Http(answer="Rewritten!")

    replied = _Replied(raw_text="original", sender_id=4242, id=777)
    event = FakeEvent(raw_text="gpt edit make it shorter", is_reply=True, reply_message=replied)
    event.reply_to = FakeReplyTo(reply_to_msg_id=777)
    await bot.registry.dispatch(bot, event, event.raw_text)

    assert replied.edits and replied.edits[0] == "Rewritten!"
    assert event.deleted is True


# --------------------------------------------------------------------------
# summarize
# --------------------------------------------------------------------------


async def test_summarize_replied_text(bot) -> None:
    from selfbot.plugins.ai import get_manager

    manager = get_manager(type("C", (), {"bot": bot})())
    bot.ai = manager
    bot.http = _Http(answer="Summary here")
    replied = _Replied(raw_text="A very long message that needs summarizing.")
    event = FakeEvent(raw_text="summarize", is_reply=True, reply_message=replied)
    event.reply_to = FakeReplyTo(reply_to_msg_id=replied.id)
    await bot.registry.dispatch(bot, event, event.raw_text)
    assert any("Summary here" in r for r in event.replies)
    # It must not persist into conversation memory.
    stored = await bot.db.count_ai_messages(event.chat_id)
    assert stored == 0


async def test_summarize_requires_reply_or_count(bot) -> None:
    event = FakeEvent(raw_text="summarize")
    await bot.registry.dispatch(bot, event, event.raw_text)
    assert any("Reply to" in r for r in event.replies)


async def test_summarize_conversation(bot) -> None:
    from selfbot.plugins.ai import get_manager

    manager = get_manager(type("C", (), {"bot": bot})())
    bot.ai = manager
    bot.http = _Http(answer="Conv summary")

    class IterMessage:
        def __init__(self, text: str) -> None:
            self.raw_text = text

        async def get_sender(self) -> _Sender:
            return _Sender()

    messages = [IterMessage("one"), IterMessage("two"), IterMessage("three")]

    async def iter_messages(chat_id, limit):
        for m in messages[:limit]:
            yield m

    bot.client.iter_messages = iter_messages
    event = FakeEvent(raw_text="summarize 3")
    await bot.registry.dispatch(bot, event, event.raw_text)
    assert any("Conv summary" in r for r in event.replies)
    content = bot.http.calls[0]["json"]["messages"][-1]["content"]
    assert "one" in content and "three" in content


def test_extract_pdf_and_text(tmp_path: Path) -> None:
    from selfbot.plugins.ai import _extract_document_text

    txt = tmp_path / "note.txt"
    txt.write_text("hello world", encoding="utf-8")
    assert _extract_document_text(txt) == "hello world"
