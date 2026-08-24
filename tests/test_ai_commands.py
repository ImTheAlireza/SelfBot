"""Tests for memory, gpt edit, summarize and reply-context gpt."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
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


def _png_bytes() -> bytes:
    from PIL import Image

    output = BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, "PNG")
    return output.getvalue()


@dataclass
class _Replied:
    raw_text: str
    sender_id: int = 4242
    id: int = 555
    photo: Any = None
    document: Any = None
    sticker: Any = None
    file: Any = None
    media_bytes: bytes = b""
    edits: list[str] = field(default_factory=list)

    async def get_sender(self) -> _Sender:
        return _Sender()

    async def download_media(self, *, file: Any) -> Any:
        if file is bytes:
            return self.media_bytes
        path = Path(file) / "media.bin"
        path.write_bytes(self.media_bytes)
        return str(path)

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


async def test_gpt_reply_sends_image_to_vision_model(bot) -> None:
    from selfbot.plugins.ai import get_manager

    await bot.db.add_provider(
        "vision", "https://vision.example/v1", "sk-test", model="vision-model", is_default=True
    )
    manager = get_manager(type("C", (), {"bot": bot})())
    bot.ai = manager
    bot.http = _Http(answer="I see a red square")
    replied = _Replied(raw_text="", photo=object(), media_bytes=_png_bytes())
    event = FakeEvent(
        raw_text="gpt describe this image",
        is_reply=True,
        reply_message=replied,
    )

    await bot.registry.dispatch(bot, event, event.raw_text)

    content = bot.http.calls[0]["json"]["messages"][-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "describe this image"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert any("I see a red square" in reply for reply in event.replies)


def test_ai_response_formats_thinking_and_italic_footer() -> None:
    from selfbot.plugins.ai import _format_ai_response

    rendered = _format_ai_response(
        "&lt;think&gt;Check the **details**.&lt;/think&gt;\nHello!",
        provider="jasper",
        requested_model="ox-alpha",
        reported_model="x-preview-f-free",
    )

    assert "<b>💭 Thinking</b>" in rendered
    assert "<blockquote expandable>" in rendered
    assert "Check the <strong>details</strong>." in rendered
    assert "Hello!" in rendered
    assert "<i>— via jasper · requested ox-alpha" in rendered
    assert "API reported x-preview-f-free" in rendered
    assert rendered.endswith("</i>")

    from telethon.extensions import html

    _plain, entities = html.parse(rendered)
    blockquote = next(entity for entity in entities if type(entity).__name__ == "MessageEntityBlockquote")
    assert blockquote.collapsed is True
    assert any(type(entity).__name__ == "MessageEntityItalic" for entity in entities)


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


async def test_summarize_sends_static_sticker_to_vision_model(bot) -> None:
    from selfbot.plugins.ai import get_manager

    await bot.db.add_provider(
        "vision", "https://vision.example/v1", "sk-test", model="vision-model", is_default=True
    )
    manager = get_manager(type("C", (), {"bot": bot})())
    bot.ai = manager
    bot.http = _Http(answer="A red sticker")
    replied = _Replied(
        raw_text="",
        document=object(),
        sticker=object(),
        media_bytes=_png_bytes(),
    )
    event = FakeEvent(
        raw_text="summarize",
        is_reply=True,
        reply_message=replied,
    )

    await bot.registry.dispatch(bot, event, event.raw_text)

    content = bot.http.calls[0]["json"]["messages"][-1]["content"]
    assert isinstance(content, list)
    assert any(part.get("type") == "image_url" for part in content)
    text_part = next(part["text"] for part in content if part.get("type") == "text")
    assert "static sticker" in text_part
    assert any("A red sticker" in reply for reply in event.replies)


async def test_summarize_requires_reply_or_count(bot) -> None:
    event = FakeEvent(raw_text="summarize")
    await bot.registry.dispatch(bot, event, event.raw_text)
    assert any("Reply to" in r for r in event.replies)


async def test_summarize_accepts_single_dash_flags(bot) -> None:
    from selfbot.plugins.ai import get_manager

    await bot.db.add_provider(
        "summary", "https://summary.example/v1", "sk-test", model="summary-model", is_default=True
    )
    manager = get_manager(type("C", (), {"bot": bot})())
    bot.ai = manager
    bot.http = _Http(answer="خلاصه")
    replied = _Replied(raw_text="A message to summarize.")
    event = FakeEvent(
        raw_text="summarize -lang fa -brief",
        is_reply=True,
        reply_message=replied,
    )

    await bot.registry.dispatch(bot, event, event.raw_text)

    assert any("خلاصه" in reply for reply in event.replies)
    request = bot.http.calls[0]["json"]
    prompt = request["messages"][-1]["content"]
    assert "Respond in Persian" in prompt
    assert "very short" in prompt
    assert "clear bullet points" in prompt
    assert request["max_tokens"] == 600


async def test_summarize_actions_style_and_focus(bot) -> None:
    from selfbot.plugins.ai import get_manager

    await bot.db.add_provider(
        "summary", "https://summary.example/v1", "sk-test", model="summary-model", is_default=True
    )
    manager = get_manager(type("C", (), {"bot": bot})())
    bot.ai = manager
    bot.http = _Http(answer="Action summary")
    replied = _Replied(raw_text="Alice owns deployment by Friday.")
    event = FakeEvent(
        raw_text='summarize -style actions -focus "owners and deadlines" -length detailed',
        is_reply=True,
        reply_message=replied,
    )

    await bot.registry.dispatch(bot, event, event.raw_text)

    request = bot.http.calls[0]["json"]
    prompt = request["messages"][-1]["content"]
    assert "action items, owners, deadlines" in prompt
    assert "owners and deadlines" in prompt
    assert request["max_tokens"] == 1800


async def test_summarize_long_source_uses_map_reduce_without_truncating(bot) -> None:
    from selfbot.plugins.ai import get_manager

    await bot.db.add_provider(
        "summary", "https://summary.example/v1", "sk-test", model="summary-model", is_default=True
    )
    manager = get_manager(type("C", (), {"bot": bot})())
    bot.ai = manager
    bot.http = _Http(answer="section notes")
    source = ("A" * 25_000) + " FINAL-MARKER"
    replied = _Replied(raw_text=source)
    event = FakeEvent(
        raw_text="summarize -length medium",
        is_reply=True,
        reply_message=replied,
    )

    await bot.registry.dispatch(bot, event, event.raw_text)

    # 3 source chunks plus one synthesis request.
    assert len(bot.http.calls) == 4
    chunk_prompts = [
        call["json"]["messages"][-1]["content"]
        for call in bot.http.calls[:-1]
    ]
    assert any("FINAL-MARKER" in prompt for prompt in chunk_prompts)
    final_prompt = bot.http.calls[-1]["json"]["messages"][-1]["content"]
    assert "Section 1 notes" in final_prompt
    assert bot.http.calls[-1]["json"]["max_tokens"] == 1100


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


def test_extract_text_html_docx_and_reject_binary(tmp_path: Path) -> None:
    import zipfile

    from selfbot.plugins.ai import _extract_document_text

    txt = tmp_path / "note.txt"
    txt.write_text("hello world", encoding="utf-8")
    assert _extract_document_text(txt) == "hello world"

    html = tmp_path / "page.html"
    html.write_text("<h1>Title</h1><p>Useful text</p>", encoding="utf-8")
    assert _extract_document_text(html) == "Title\nUseful text"

    docx = tmp_path / "notes.docx"
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>First paragraph</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
    assert _extract_document_text(docx) == "First paragraph\nSecond paragraph"

    binary = tmp_path / "archive.zip"
    binary.write_bytes(b"\x00\xffnot text")
    assert _extract_document_text(binary) == ""
