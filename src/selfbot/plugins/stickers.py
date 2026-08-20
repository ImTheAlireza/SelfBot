"""Text stickers and sticker pack management."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..errors import FeatureDisabledError, UsageError, ValidationError
from ..registry import Context, command
from ..utils.files import temp_workspace
from ..utils.text import shape_rtl, truncate

logger = logging.getLogger(__name__)

CATEGORY = "Stickers"

CANVAS = 512
PADDING = 32


def _font_path() -> Path | None:
    candidates = [
        Path(__file__).resolve().parents[3] / "assets" / "fonts" / "Vazirmatn-Regular.ttf",
        Path(__file__).resolve().parents[3] / "assets" / "fonts" / "Vazirmatn-Bold.ttf",
        Path("assets/fonts/Vazirmatn-Regular.ttf"),
        Path("assets/fonts/Vazirmatn-Bold.ttf"),
        Path("/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Regular.ttf"),
        Path("/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Bold.ttf"),
        Path("/usr/share/fonts/truetype/vazir/Vazir-Bold.ttf"),
        Path("/usr/share/fonts/truetype/vazir/Vazir-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    return next((p for p in candidates if p.is_file()), None)


def _wrap_words(
    text: str,
    draw: Any,
    font: Any,
    max_width: float,
) -> tuple[list[str], bool]:
    """Wrap text to fit within max_width using actual font measurements."""
    lines: list[str] = []
    has_broken_word = False

    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            lines.append("")
            continue

        words = paragraph.split()
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if draw.textlength(shape_rtl(candidate), font=font) <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                if draw.textlength(shape_rtl(word), font=font) <= max_width:
                    current = word
                else:
                    has_broken_word = True
                    chunk = ""
                    for ch in word:
                        if draw.textlength(shape_rtl(chunk + ch), font=font) <= max_width:
                            chunk += ch
                        else:
                            if chunk:
                                lines.append(chunk)
                            chunk = ch
                    current = chunk
        if current:
            lines.append(current)

    return lines, has_broken_word


def render_sticker(text: str, output: Path, watermark: str = "") -> Path:
    """Draw text onto a 512×512 WEBP, auto-fitting the font size."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    font_file = _font_path()
    font_str = str(font_file) if font_file else None
    max_width = CANVAS - 2 * PADDING
    max_height = CANVAS - 2 * PADDING - (44 if watermark else 0)

    chosen_font: Any = None
    chosen_lines: list[str] = []
    min_size, max_size = 18, 160

    if font_str:
        low, high = min_size, max_size
        while low <= high:
            mid = (low + high) // 2
            try:
                font = ImageFont.truetype(font_str, mid)
            except OSError:
                break

            lines, broken_word = _wrap_words(text, draw, font, max_width)
            shaped = [shape_rtl(line) if line else "" for line in lines]
            widest = max((draw.textlength(line, font=font) for line in shaped), default=0)
            line_height = mid * 1.35
            total_height = line_height * len(shaped)

            if not broken_word and widest <= max_width and total_height <= max_height:
                chosen_font = font
                chosen_lines = shaped
                low = mid + 1
            else:
                high = mid - 1

    if chosen_font is None:
        if font_str:
            try:
                chosen_font = ImageFont.truetype(font_str, min_size)
            except OSError:
                logger.warning("Sticker font %r exists but cannot be loaded", font_str)
                chosen_font = None
        if chosen_font is None:
            # Never silently fall back to PIL's tiny built-in font: the result
            # is an unreadable 10px sticker. The Vazirmatn fonts ship in
            # assets/fonts/, so this only happens if they were deleted.
            raise RuntimeError(
                "No usable font for stickers. Restore assets/fonts/ "
                '(Vazirmatn-Regular.ttf) or reinstall with `pip install -e ".[full]"`.'
            )
        lines, _ = _wrap_words(text, draw, chosen_font, max_width)
        chosen_lines = [shape_rtl(line) if line else "" for line in lines]

    size = getattr(chosen_font, "size", 18)
    line_height = size * 1.35
    total_text_height = line_height * len(chosen_lines)
    y = (CANVAS - total_text_height) / 2 - (18 if watermark else 0)

    for line in chosen_lines:
        if line:
            width = draw.textlength(line, font=chosen_font)
            x = (CANVAS - width) / 2
            # Outline keeps the text readable on any chat background.
            stroke_width = max(2, int(size / 16))
            draw.text(
                (x, y),
                line,
                font=chosen_font,
                fill=(255, 255, 255, 255),
                stroke_width=stroke_width,
                stroke_fill=(0, 0, 0, 255),
            )
        y += line_height

    if watermark:
        wm_size = max(16, min(26, int(size * 0.4)))
        try:
            wm_font = (
                ImageFont.truetype(font_str, wm_size)
                if font_str
                else ImageFont.load_default()
            )
        except OSError:
            wm_font = ImageFont.load_default()
        wm_width = draw.textlength(watermark, font=wm_font)
        draw.text(
            ((CANVAS - wm_width) / 2, CANVAS - 44),
            watermark,
            font=wm_font,
            fill=(170, 170, 170, 220),
            stroke_width=2,
            stroke_fill=(0, 0, 0, 200),
        )

    image.save(output, "WEBP", quality=95, lossless=True)
    return output


@command(
    "stick",
    category=CATEGORY,
    min_args=1,
    usage="stick [-save] <text>",
    examples=("stick hello world", "stick -save my caption"),
)
async def cmd_stick(ctx: Context) -> None:
    """Turn text into a sticker, optionally saving it to the open pack."""
    raw = ctx.raw_args.strip()
    save = False
    if raw.lower().startswith("-save"):
        save = True
        raw = raw[5:].strip()

    if not raw:
        raise ValidationError("Give me some text.")
    if len(raw) > 300:
        raise ValidationError("Text is too long for a sticker (max 300 characters).")

    with temp_workspace(parent=ctx.config.downloads_dir) as workspace:
        webp = workspace / "sticker.webp"
        await asyncio.to_thread(
            render_sticker, raw, webp, ctx.config.sticker.watermark
        )

        from telethon.tl.types import DocumentAttributeSticker, InputStickerSetEmpty

        sent = await ctx.client.send_file(
            ctx.chat_id,
            str(webp),
            attributes=[
                DocumentAttributeSticker(alt="🖤", stickerset=InputStickerSetEmpty())
            ],
        )

        # Delete the command message so only the sticker remains.
        try:
            await ctx.event.delete()
        except Exception:
            pass

        if save:
            await _save_to_pack(ctx, workspace, webp, sent)


async def _save_to_pack(ctx: Context, workspace: Path, webp: Path, sent: object) -> None:
    if not ctx.config.sticker.enabled:
        raise FeatureDisabledError(
            "Sticker packs need a helper bot. Set `STICKER_BOT_TOKEN` and "
            "`STICKER_BOT_USERNAME` in your .env."
        )

    pack = ctx.bot.active_sticker_pack.get(ctx.sender_id)
    if not pack:
        await ctx.respond("⚠️ No pack open. Use `stickerpack create` or `open` first.")
        return

    from PIL import Image

    png = workspace / "sticker.png"
    await asyncio.to_thread(lambda: Image.open(webp).save(png, "PNG"))

    short_name = _full_pack_name(ctx, pack["name"])
    if pack["mode"] == "create":
        result = await _sticker_api(
            ctx, "createNewStickerSet",
            user_id=ctx.sender_id, name=short_name,
            title=pack["title"], png_sticker=png,
        )
        if result.get("ok"):
            pack["mode"] = "add"
            await ctx.db.add_sticker_pack(pack["name"], pack["title"], ctx.sender_id)
    else:
        result = await _sticker_api(
            ctx, "addStickerToSet",
            user_id=ctx.sender_id, name=short_name, png_sticker=png,
        )

    if result.get("ok"):
        await ctx.respond(f"✅ Saved to `{pack['name']}`.")
    else:
        await ctx.respond(f"❌ Telegram said: {result.get('description', 'unknown error')}")


def _full_pack_name(ctx: Context, name: str) -> str:
    return f"{name}_by_{ctx.config.sticker.bot_username}"


async def _sticker_api(ctx: Context, method: str, **fields: object) -> dict:
    """Call the Bot API with the helper bot's token."""
    import aiohttp

    form = aiohttp.FormData()
    for key, value in fields.items():
        if isinstance(value, Path):
            form.add_field(key, value.read_bytes(), filename=value.name)
        else:
            form.add_field(key, str(value))
    form.add_field("emojis", "🖤")

    token = ctx.config.sticker.bot_token
    try:
        return await ctx.bot.http.post_json(
            f"https://api.telegram.org/bot{token}/{method}", data=form, timeout=60
        )
    except Exception as exc:
        return {"ok": False, "description": str(exc)}


@command(
    "stickerpack",
    category=CATEGORY,
    usage="stickerpack <create|open|list|close|delete> [name] [title]",
    examples=("stickerpack create mypack My Pack", "stickerpack list"),
)
async def cmd_stickerpack(ctx: Context) -> None:
    """Create, open, list or delete your sticker packs."""
    if not ctx.args:
        await ctx.reply(
            "📦 **Sticker packs**\n\n"
            "`stickerpack create <name> <title>` — start a new pack\n"
            "`stickerpack open <name>` — add to an existing pack\n"
            "`stickerpack list` — show your packs\n"
            "`stickerpack close` — close the open pack\n"
            "`stickerpack delete <name>` — remove a pack\n\n"
            "Save stickers with `stick -save <text>`."
        )
        return

    action = ctx.args[0].lower()
    handlers = {
        "create": _pack_create,
        "open": _pack_open,
        "list": _pack_list,
        "close": _pack_close,
        "delete": _pack_delete,
    }
    handler = handlers.get(action)
    if handler is None:
        raise ValidationError(f"Unknown action `{action}`.")
    await handler(ctx)


async def _pack_create(ctx: Context) -> None:
    if len(ctx.args) < 3:
        raise UsageError("Usage: `stickerpack create <short_name> <title>`")

    name = ctx.args[1].lower()
    if not name.replace("_", "").isalnum():
        raise ValidationError("Pack name must be letters, numbers and underscores.")

    title = " ".join(ctx.args[2:])
    if await ctx.db.get_sticker_pack(name):
        raise ValidationError(f"Pack `{name}` already exists — use `open` instead.")

    ctx.bot.active_sticker_pack[ctx.sender_id] = {
        "name": name, "title": title, "mode": "create"
    }
    await ctx.reply(
        f"🆕 Pack **{title}** (`{name}`) is ready.\n"
        f"Send `stick -save <text>` to create it on Telegram."
    )


async def _pack_open(ctx: Context) -> None:
    if len(ctx.args) < 2:
        raise UsageError("Usage: `stickerpack open <name>`")
    name = ctx.args[1].lower()
    pack = await ctx.db.get_sticker_pack(name)
    if pack is None:
        raise ValidationError(f"No pack named `{name}`.")
    ctx.bot.active_sticker_pack[ctx.sender_id] = {
        "name": pack.name, "title": pack.title, "mode": "add"
    }
    await ctx.reply(f"📂 Opened **{pack.title}** — `stick -save <text>` to add.")


async def _pack_list(ctx: Context) -> None:
    packs = await ctx.db.list_sticker_packs(
        None if ctx.is_sudo else ctx.sender_id
    )
    if not packs:
        await ctx.reply("ℹ️ No sticker packs yet.")
        return
    lines = [f"📦 **Sticker packs** ({len(packs)})\n"]
    for pack in packs:
        link = f"https://t.me/addstickers/{_full_pack_name(ctx, pack.name)}"
        lines.append(f"• **{truncate(pack.title, 40)}** (`{pack.name}`)\n  {link}")
    await ctx.reply("\n".join(lines), link_preview=False)


async def _pack_close(ctx: Context) -> None:
    pack = ctx.bot.active_sticker_pack.pop(ctx.sender_id, None)
    if not pack:
        await ctx.reply("ℹ️ No pack is open.")
        return
    link = f"https://t.me/addstickers/{_full_pack_name(ctx, pack['name'])}"
    await ctx.reply(f"🔒 Closed **{pack['title']}**\n{link}", link_preview=False)


@command(
    "setwatermark",
    category=CATEGORY,
    sudo_only=True,
    usage="setwatermark <text>",
    examples=("setwatermark @myname", "setwatermark", "setwatermark off"),
)
async def cmd_setwatermark(ctx: Context) -> None:
    """Set the watermark text shown at the bottom of new stickers.

    This changes the watermark used when creating stickers with `stick`.
    Existing stickers are not affected — only new ones.

    • `setwatermark @myname` — set watermark to `@myname`
    • `setwatermark off` — disable watermark
    • `setwatermark` — show current watermark
    """
    if not ctx.args:
        current = ctx.config.sticker.watermark
        if current:
            await ctx.reply(f"📝 Current watermark: `{current}`")
        else:
            await ctx.reply("ℹ️ No watermark is set. Stickers have no text at the bottom.")
        return

    new_watermark = ctx.raw_args.strip()
    if new_watermark.lower() == "off":
        new_watermark = ""

    # Update the watermark in the live config. Since Config is frozen,
    # we need to replace the sticker sub-config.
    import dataclasses
    ctx.bot.config = dataclasses.replace(
        ctx.bot.config,
        sticker=dataclasses.replace(ctx.bot.config.sticker, watermark=new_watermark),
    )

    if new_watermark:
        await ctx.reply(f"✅ Watermark set to `{new_watermark}`.\nNew stickers will show this at the bottom.")
    else:
        await ctx.reply("✅ Watermark disabled. New stickers will have no text at the bottom.")


async def _pack_delete(ctx: Context) -> None:
    if len(ctx.args) < 2:
        raise UsageError("Usage: `stickerpack delete <name>`")
    name = ctx.args[1].lower()
    if not await ctx.db.get_sticker_pack(name):
        raise ValidationError(f"No pack named `{name}`.")
    if not await ctx.bot.confirm(ctx.event, f"⚠️ Permanently delete pack `{name}`?"):
        await ctx.reply("👍 Cancelled.")
        return

    status = await ctx.reply("🗑 Asking @Stickers…")
    full_name = _full_pack_name(ctx, name)
    try:
        async with ctx.client.conversation("Stickers", timeout=30) as conv:
            await conv.send_message("/delpack")
            await conv.get_response()
            await conv.send_message(full_name)
            response = await conv.get_response()
            if "sure" not in response.text.lower():
                await ctx.bot.edit(status, f"❌ @Stickers replied:\n`{response.text}`")
                return
            await conv.send_message("Yes, I am totally sure.")
            await conv.get_response()
    except asyncio.TimeoutError:
        await ctx.bot.edit(status, "❌ @Stickers did not respond in time.")
        return
    except Exception as exc:
        await ctx.bot.edit(status, f"❌ Could not reach @Stickers: `{exc}`")
        return

    await ctx.db.delete_sticker_pack(name)
    if ctx.bot.active_sticker_pack.get(ctx.sender_id, {}).get("name") == name:
        ctx.bot.active_sticker_pack.pop(ctx.sender_id, None)
    await ctx.bot.edit(status, f"✅ Deleted `{name}`.")
