"""Utilities: QR codes, text-to-PDF, weather, dictionary, currency, TTS."""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

from ..errors import FeatureDisabledError, UsageError, ValidationError
from ..registry import Context, command
from ..utils.files import temp_workspace
from ..utils.text import has_rtl, shape_rtl, truncate

logger = logging.getLogger(__name__)

CATEGORY = "Utilities"

COLOURS = {
    "black": "#000000", "white": "#FFFFFF", "red": "#FF0000",
    "blue": "#0000FF", "green": "#00A000", "yellow": "#FFD700",
    "purple": "#800080", "orange": "#FF8C00", "pink": "#FFC0CB",
    "cyan": "#00CED1", "grey": "#808080", "gray": "#808080",
}


# ---------------------------------------------------------------------------
# QR codes
# ---------------------------------------------------------------------------


@command(
    "qr",
    category=CATEGORY,
    min_args=1,
    usage="qr <text> [--size N] [--fg colour] [--bg colour]",
    examples=("qr https://example.com", "qr hello --size 14 --fg blue --bg white"),
)
async def cmd_qr(ctx: Context) -> None:
    """Generate a QR code, optionally sized and coloured.

    Merges the old `qr` and `qradv` commands into one flag-driven interface.
    """
    import qrcode

    args = list(ctx.args)
    size, fg, bg = 10, "black", "white"

    # Pull out flags, leaving the payload behind.
    remaining: list[str] = []
    index = 0
    while index < len(args):
        token = args[index].lower()
        if token in {"--size", "-s"} and index + 1 < len(args):
            if not args[index + 1].isdigit():
                raise ValidationError("`--size` needs a number.")
            size = int(args[index + 1])
            index += 2
        elif token in {"--fg", "--foreground"} and index + 1 < len(args):
            fg = args[index + 1].lower()
            index += 2
        elif token in {"--bg", "--background"} and index + 1 < len(args):
            bg = args[index + 1].lower()
            index += 2
        else:
            remaining.append(args[index])
            index += 1

    payload = " ".join(remaining).strip()
    if not payload:
        raise ValidationError("Nothing to encode.")
    if not 1 <= size <= 40:
        raise ValidationError("Size must be between 1 and 40.")
    for name, value in (("--fg", fg), ("--bg", bg)):
        if value not in COLOURS:
            raise ValidationError(
                f"Unknown colour for {name}: `{value}`.\n"
                f"Available: {', '.join(sorted(set(COLOURS)))}"
            )

    status = await ctx.reply("🔲 Generating…")

    with temp_workspace(parent=ctx.config.downloads_dir) as workspace:
        output = workspace / "qr.png"

        def build() -> None:
            qr = qrcode.QRCode(
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=size,
                border=4,
            )
            qr.add_data(payload)
            qr.make(fit=True)
            qr.make_image(
                fill_color=COLOURS[fg], back_color=COLOURS[bg]
            ).save(output)

        await asyncio.to_thread(build)
        await ctx.client.send_file(
            ctx.chat_id,
            str(output),
            caption=f"🔲 `{truncate(payload, 150)}`",
            reply_to=ctx.event.id,
        )
    await status.delete()


@command("qrread", category=CATEGORY, requires_reply=True, usage="qrread")
async def cmd_qrread(ctx: Context) -> None:
    """Decode a QR code from a replied-to image."""
    replied = await ctx.get_reply_message()
    if not (replied.photo or replied.document):
        raise ValidationError("Reply to an image.")

    status = await ctx.reply("🔍 Reading…")

    with temp_workspace(parent=ctx.config.downloads_dir) as workspace:
        image_path = Path(await replied.download_media(file=str(workspace)))

        # Local decode first; fall back to the public API when unavailable.
        decoded = await asyncio.to_thread(_decode_locally, image_path)
        if decoded is None:
            decoded = await _decode_remotely(ctx, image_path)

    if not decoded:
        await ctx.bot.edit(status, "❌ No QR code found in that image.")
        return

    await ctx.bot.edit(
        status, f"✅ **Decoded**\n\n`{truncate(decoded, 3500)}`"
    )


def _decode_locally(path: Path) -> str | None:
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
    except ImportError:
        return None
    try:
        results = decode(Image.open(path))
        return results[0].data.decode("utf-8", errors="replace") if results else ""
    except Exception:
        return None


async def _decode_remotely(ctx: Context, path: Path) -> str:
    import aiohttp

    payload = await asyncio.to_thread(path.read_bytes)
    form = aiohttp.FormData()
    form.add_field("file", payload, filename=path.name)
    data = await ctx.bot.http.post_json(
        "https://api.qrserver.com/v1/read-qr-code/", data=form, timeout=30
    )
    try:
        return data[0]["symbol"][0]["data"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


# ---------------------------------------------------------------------------
# Text to PDF
# ---------------------------------------------------------------------------


@command(
    "topdf",
    category=CATEGORY,
    requires_reply=True,
    usage="topdf [en|fa] [font size]",
    examples=("topdf", "topdf fa 14"),
)
async def cmd_topdf(ctx: Context) -> None:
    """Convert a replied-to text message into a PDF."""
    replied = await ctx.get_reply_message()
    text = (replied.raw_text or "").strip()
    if not text:
        raise ValidationError("The replied message has no text.")

    language: str | None = None
    font_size = 12
    for arg in ctx.args:
        lowered = arg.lower()
        if lowered in {"en", "english"}:
            language = "en"
        elif lowered in {"fa", "persian", "farsi"}:
            language = "fa"
        elif lowered.isdigit():
            font_size = int(lowered)
        else:
            raise UsageError("Usage: `topdf [en|fa] [font size]`")

    if not 6 <= font_size <= 72:
        raise ValidationError("Font size must be between 6 and 72.")
    if language is None:
        language = "fa" if has_rtl(text[:200]) else "en"

    status = await ctx.reply("📄 Building PDF…")

    with temp_workspace(parent=ctx.config.downloads_dir) as workspace:
        output = workspace / "document.pdf"
        font_used = await asyncio.to_thread(
            _build_pdf, text, output, language, font_size
        )
        await ctx.bot.edit(status, "⬆️ Uploading…")
        await ctx.client.send_file(
            ctx.chat_id,
            str(output),
            caption=(
                f"📄 **PDF ready**\n"
                f"Language: {'Persian' if language == 'fa' else 'English'}\n"
                f"Font: {font_used} · {font_size}pt\n"
                f"Length: {len(text):,} characters"
            ),
            reply_to=ctx.event.id,
        )
    await status.delete()


def _find_font() -> Path | None:
    """Locate a Unicode-capable TTF for Persian rendering."""
    candidates = [
        Path(__file__).resolve().parents[3] / "assets" / "fonts" / "Vazirmatn-Regular.ttf",
        Path("assets/fonts/Vazirmatn-Regular.ttf"),
        Path("fonts/Vazirmatn-Regular.ttf"),
        Path("/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    return next((p for p in candidates if p.is_file()), None)


def _build_pdf(text: str, output: Path, language: str, font_size: int) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    font_name = "Helvetica"
    if language == "fa":
        font_path = _find_font()
        if font_path:
            try:
                pdfmetrics.registerFont(TTFont("Custom", str(font_path)))
                font_name = "Custom"
            except Exception:
                logger.warning("Could not register %s", font_path)

    page = canvas.Canvas(str(output), pagesize=A4)
    width, height = A4
    margin, leading = 50, font_size + 8
    usable = width - 2 * margin
    y = height - margin
    page.setFont(font_name, font_size)

    rtl = language == "fa"
    for paragraph in text.split("\n"):
        paragraph = paragraph.rstrip()
        if not paragraph:
            y -= leading / 2
            if y < margin:
                page.showPage()
                page.setFont(font_name, font_size)
                y = height - margin
            continue

        rendered = shape_rtl(paragraph) if rtl else paragraph
        for line in _wrap(rendered, page, font_name, font_size, usable):
            if y < margin:
                page.showPage()
                page.setFont(font_name, font_size)
                y = height - margin
            if rtl:
                page.drawRightString(width - margin, y, line)
            else:
                page.drawString(margin, y, line)
            y -= leading

    page.save()
    return font_name


def _wrap(text: str, page: object, font: str, size: int, max_width: float) -> list[str]:
    """Greedy word wrap, breaking overlong words character by character."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if page.stringWidth(candidate, font, size) <= max_width:  # type: ignore[attr-defined]
            current = candidate
            continue
        if current:
            lines.append(current)
        if page.stringWidth(word, font, size) <= max_width:  # type: ignore[attr-defined]
            current = word
        else:
            chunk = ""
            for char in word:
                if page.stringWidth(chunk + char, font, size) <= max_width:  # type: ignore[attr-defined]
                    chunk += char
                else:
                    lines.append(chunk)
                    chunk = char
            current = chunk
    if current:
        lines.append(current)
    return lines or [""]


# ---------------------------------------------------------------------------
# Weather (wttr.in — wraps AccuWeather and other accurate sources)
# ---------------------------------------------------------------------------

WTTR_ICONS = {
    "113": "☀️", "116": "🌤", "119": "☁️", "122": "☁️",
    "143": "🌫", "176": "🌦", "179": "🌧", "182": "🌨",
    "185": "🌨", "200": "🌦", "227": "❄️", "230": "❄️",
    "248": "🌫", "260": "🌫", "263": "🌦", "266": "🌧",
    "281": "🌧", "284": "🌧", "293": "🌧", "296": "🌧",
    "299": "🌧", "302": "🌧", "305": "🌧", "308": "🌧",
    "311": "🌧", "314": "🌧", "317": "❄️", "320": "❄️",
    "323": "❄️", "326": "❄️", "329": "❄️", "332": "❄️",
    "335": "❄️", "338": "❄️", "350": "🌧", "353": "🌦",
    "356": "🌧", "359": "🌧", "362": "❄️", "365": "❄️",
    "368": "❄️", "371": "❄️", "374": "🌧", "377": "❄️",
    "386": "⛈", "389": "⛈", "392": "⛈", "395": "⛈",
}


def _weather_icon(code: str | int) -> str:
    """Map a wttr.in weather code to an emoji icon."""
    return WTTR_ICONS.get(str(code), "🌡")


async def _wttr_fetch(ctx: Context, city: str) -> dict:
    """Fetch weather data from wttr.in (wraps AccuWeather and other sources)."""
    data = await ctx.bot.http.get_json(
        f"https://wttr.in/{city}",
        params={"format": "j1"},
        timeout=20,
    )
    if not data:
        raise ValidationError(f"Could not fetch weather for `{truncate(city, 60)}`.")
    area = data.get("nearest_area", [{}])
    if not area:
        raise ValidationError(f"Could not find a place called `{truncate(city, 60)}`.")
    return data


@command(
    "weather",
    category=CATEGORY,
    min_args=1,
    aliases=("dw",),
    usage="weather <city>",
    examples=("weather Dronten", "weather Tehran"),
)
async def cmd_weather(ctx: Context) -> None:
    """Three-day forecast for a city using wttr.in (AccuWeather-based)."""
    city = ctx.raw_args.strip()
    status = await ctx.reply("🌍 Looking up…")

    try:
        data = await _wttr_fetch(ctx, city)
    except ValidationError:
        raise
    except Exception as exc:
        await ctx.bot.edit(status, f"❌ Could not fetch weather: `{exc}`")
        return

    area = data["nearest_area"][0]
    label = ", ".join(filter(None, [
        area.get("areaName", [{}])[0].get("value", ""),
        area.get("country", [{}])[0].get("value", ""),
    ]))

    current = data.get("current_condition", [{}])[0]
    weather_desc = (current.get("weatherDesc", [{}])[0].get("value", "—"))
    feels = current.get("FeelsLikeC", "?")
    temp = current.get("temp_C", "?")
    humidity = current.get("humidity", "?")
    wind = current.get("windspeedKmph", "?")
    wind_dir = current.get("winddir16Point", "")
    vis = current.get("visibility", "?")
    uv = current.get("uvIndex", "?")
    precip = current.get("precipMM", "0")
    icon = _weather_icon(current.get("weatherCode", "113"))

    lines = [
        f"{icon} **Weather — {label}**\n",
        f"**Now:** {weather_desc} · {temp}°C (feels {feels}°C)",
        f"💧 Humidity: {humidity}% · 💨 Wind: {wind} km/h {wind_dir}",
        f"👁 Visibility: {vis} km · ☀️ UV: {uv} · 🌧 Precip: {precip} mm\n",
        "**3-day forecast**\n",
    ]

    for day in data.get("weather", []):
        date = day.get("date", "?")
        max_t = day.get("maxtempC", "?")
        min_t = day.get("mintempC", "?")
        avg_t = day.get("avgtempC", "?")
        sun_h = day.get("sunHour", "?")
        uv_day = day.get("uvIndex", "?")
        hourly = day.get("hourly", [])
        if hourly:
            mid = hourly[4]  # noon entry
            day_desc = mid.get("weatherDesc", [{}])[0].get("value", "—")
            day_icon = _weather_icon(mid.get("weatherCode", "113"))
            precip_pct = mid.get("chanceofrain", "0")
        else:
            day_desc = "—"
            day_icon = "🌡"
            precip_pct = "0"

        lines.append(
            f"**{date}** {day_icon} {day_desc}\n"
            f"  🔺 {max_t}°C  🔻 {min_t}°C  ⛅ {avg_t}°C  "
            f"💧 {precip_pct}%  ☀️ {sun_h}h  UV {uv_day}"
        )

    await status.delete()
    await ctx.reply("\n".join(lines))


@command(
    "hourly",
    category=CATEGORY,
    min_args=1,
    aliases=("hw",),
    usage="hourly <city>",
)
async def cmd_hourly(ctx: Context) -> None:
    """Next 72 hours of weather for a city using wttr.in (AccuWeather-based)."""
    city = ctx.raw_args.strip()
    status = await ctx.reply("🌍 Looking up…")

    try:
        data = await _wttr_fetch(ctx, city)
    except ValidationError:
        raise
    except Exception as exc:
        await ctx.bot.edit(status, f"❌ Could not fetch weather: `{exc}`")
        return

    area = data["nearest_area"][0]
    label = ", ".join(filter(None, [
        area.get("areaName", [{}])[0].get("value", ""),
        area.get("country", [{}])[0].get("value", ""),
    ]))

    lines = [f"🕐 **Next 72 hours — {label}**\n"]

    # wttr.in returns 3 days × 8 hourly slots (00, 03, 06, …, 21) = 24 entries
    # covering 72 hours. We show all of them, aligned to Tehran's timezone.
    all_hourly: list[dict] = []
    for day in data.get("weather", []):
        for hour in day.get("hourly", []):
            all_hourly.append(hour)

    from datetime import datetime, timezone, timedelta

    # Use Tehran's timezone (UTC+3:30) to align the "now" hour.
    tehran_tz = timezone(timedelta(hours=3, minutes=30))
    now_tehran = datetime.now(tehran_tz)
    now_hour_tehran = now_tehran.hour

    for entry in all_hourly:
        hour_utc = int(entry.get("time", "0")) // 100
        # Convert the UTC hour to Tehran time for display.
        hour_tehran = (hour_utc + 3) % 24  # +3 hours offset (simplified)
        # Show +30 for the minutes display when the half-hour shifts
        # the "current" block forward. wttr.in gives 3-hour blocks so
        # the half-hour doesn't change the slot alignment significantly.

        clock = f"{hour_tehran:02d}:00"
        temp = entry.get("tempC", "?")
        desc = entry.get("weatherDesc", [{}])[0].get("value", "—")
        icon = _weather_icon(entry.get("weatherCode", "113"))
        rain_pct = entry.get("chanceofrain", "0")
        wind = entry.get("windspeedKmph", "?")
        feels = entry.get("FeelsLikeC", "?")

        # Mark the current time slot.
        is_now = hour_tehran == now_hour_tehran
        marker = " **▸**" if is_now else ""

        lines.append(
            f"`{clock}`{marker} {icon} {desc} · {temp}°C (feels {feels}°C) · "
            f"💧{rain_pct}% · 💨{wind} km/h"
        )

    await status.delete()
    await ctx.reply("\n".join(lines))


# ---------------------------------------------------------------------------
# Dictionary
# ---------------------------------------------------------------------------


@command(
    "dic",
    category=CATEGORY,
    min_args=1,
    aliases=("define",),
    usage="dic <word>",
    examples=("dic serendipity",),
)
async def cmd_dic(ctx: Context) -> None:
    """Look up an English word: definitions, examples and pronunciation."""
    word = ctx.raw_args.strip().lower()
    if not all(ch.isalpha() or ch in "- '" for ch in word):
        raise ValidationError("Letters, hyphens and apostrophes only.")

    status = await ctx.reply(f"📖 Looking up *{word}*…")

    try:
        data = await ctx.bot.http.get_json(
            f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}", timeout=15
        )
    except Exception:
        await ctx.bot.edit(status, f"❌ No dictionary entry for `{word}`.")
        return

    if not isinstance(data, list) or not data:
        await ctx.bot.edit(status, f"❌ No dictionary entry for `{word}`.")
        return

    entry = data[0]
    phonetic = entry.get("phonetic") or next(
        (p.get("text") for p in entry.get("phonetics", []) if p.get("text")), ""
    )
    audio_url = next(
        (p["audio"] for p in entry.get("phonetics", []) if p.get("audio")), None
    )

    lines = [f"📖 **{entry.get('word', word).title()}**"]
    if phonetic:
        lines.append(f"🔊 `{phonetic}`")
    lines.append("")

    icons = {
        "noun": "🟦", "verb": "🟥", "adjective": "🟩",
        "adverb": "🟨", "pronoun": "🟪", "preposition": "🟧",
    }

    for meaning in entry.get("meanings", [])[:4]:
        pos = meaning.get("partOfSpeech", "—")
        lines.append(f"{icons.get(pos, '▫️')} **{pos.title()}**")
        for number, definition in enumerate(meaning.get("definitions", [])[:3], 1):
            lines.append(f"{number}. {definition.get('definition', '')}")
            if definition.get("example"):
                lines.append(f"   _“{definition['example']}”_")
        synonyms = meaning.get("synonyms", [])[:5]
        if synonyms:
            lines.append(f"   💡 {', '.join(synonyms)}")
        lines.append("")

    await status.delete()
    await ctx.reply("\n".join(lines).strip())

    if audio_url:
        try:
            audio = await ctx.bot.http.get_bytes(audio_url, timeout=20)
            with temp_workspace(parent=ctx.config.downloads_dir) as workspace:
                path = workspace / f"{word}.mp3"
                path.write_bytes(audio)
                await ctx.client.send_file(ctx.chat_id, str(path), voice_note=True)
        except Exception:
            logger.debug("Pronunciation download failed", exc_info=True)


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------


@command("currency", category=CATEGORY, usage="currency", aliases=("rates",))
async def cmd_currency(ctx: Context) -> None:
    """Live IRR exchange rates, gold and coin prices from tgju.org."""
    status = await ctx.reply("💱 Fetching rates…")

    try:
        html = await ctx.bot.http.get_text("https://www.tgju.org/", timeout=20)
    except Exception as exc:
        await ctx.bot.edit(status, f"❌ Could not reach tgju.org: `{exc}`")
        return

    prices = await asyncio.to_thread(_scrape_tgju, html)
    if not prices:
        await ctx.bot.edit(
            status, "⚠️ Could not parse tgju.org — the site layout may have changed."
        )
        return

    groups = (
        ("💵 **Currencies**", [
            ("USD", "price_dollar_rl"), ("EUR", "price_eur"),
            ("GBP", "price_gbp"), ("AED", "price_aed"),
        ]),
        ("🥇 **Gold**", [("18K gram", "geram18"), ("24K gram", "geram24")]),
        ("🪙 **Coins**", [
            ("Bahar Azadi", "sekeb"), ("Emami", "sekee"),
            ("Half", "nim"), ("Quarter", "rob"),
        ]),
    )

    lines: list[str] = []
    for heading, items in groups:
        rows = [
            f"  {label}: **{prices[slug]:,}** T"
            for label, slug in items
            if slug in prices
        ]
        if rows:
            lines.append(heading)
            lines.extend(rows)
            lines.append("")

    if not lines:
        await ctx.bot.edit(status, "⚠️ No price data found.")
        return

    await ctx.bot.edit(status, "💱 **Live prices** (Toman)\n\n" + "\n".join(lines).strip())


def _scrape_tgju(html: str) -> dict[str, int]:
    """Extract rates, converting rial to toman."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, int] = {}
    for row in soup.find_all("tr", attrs={"data-market-nameslug": True}):
        slug = row.get("data-market-nameslug")
        raw = row.get("data-price")
        if not slug or not raw:
            continue
        try:
            out[slug] = int(str(raw).replace(",", "")) // 10
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# Text to speech
# ---------------------------------------------------------------------------


@command("tts", category=CATEGORY, requires_reply=True, usage="tts")
async def cmd_tts(ctx: Context) -> None:
    """Read a replied-to message aloud as a voice note."""
    if ctx.config.tts_provider == "none" or not ctx.config.rapidapi_key:
        raise FeatureDisabledError(
            "Text-to-speech is not configured. Set `TTS_PROVIDER=rapidapi` "
            "and `RAPIDAPI_KEY` in your .env."
        )

    replied = await ctx.get_reply_message()
    text = (replied.raw_text or "").strip()
    if not text:
        raise ValidationError("The replied message has no text.")
    if len(text) > 5000:
        raise ValidationError("Text is too long (max 5000 characters).")

    status = await ctx.reply("🎙 Synthesising…")

    data = await ctx.bot.http.post_json(
        "https://joj-text-to-speech.p.rapidapi.com/",
        json={
            "input": {"text": text},
            "voice": {
                "languageCode": "en-US",
                "name": "en-US-Journey-F",
                "ssmlGender": "FEMALE",
            },
            "audioConfig": {"audioEncoding": "MP3", "pitch": 0, "speakingRate": 1.0},
        },
        headers={
            "x-rapidapi-key": ctx.config.rapidapi_key,
            "x-rapidapi-host": "joj-text-to-speech.p.rapidapi.com",
            "Content-Type": "application/json",
        },
        timeout=60,
    )

    audio_b64 = (data or {}).get("audioContent")
    if not audio_b64:
        raise ValidationError("The TTS service returned no audio.")

    with temp_workspace(parent=ctx.config.downloads_dir) as workspace:
        path = workspace / "speech.mp3"
        path.write_bytes(base64.b64decode(audio_b64))
        await ctx.client.send_file(
            ctx.chat_id, str(path), voice_note=True, reply_to=replied.id
        )
    await status.delete()
