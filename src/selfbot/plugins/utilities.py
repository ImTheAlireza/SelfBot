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
# Weather
# ---------------------------------------------------------------------------

WEATHER_ICONS = {
    (0,): "☀️", (1, 2): "🌤", (3,): "☁️", (45, 48): "🌫",
    (51, 53, 55, 56, 57): "🌦", (61, 63, 65, 66, 67): "🌧",
    (71, 73, 75, 77): "❄️", (80, 81, 82): "🌦",
    (85, 86): "🌨", (95, 96, 99): "⛈",
}

WEATHER_TEXT = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle",
    55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain",
    67: "Freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
    77: "Snow grains", 80: "Light showers", 81: "Showers",
    82: "Violent showers", 85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with hail",
}


def _weather_icon(code: int) -> str:
    return next((icon for codes, icon in WEATHER_ICONS.items() if code in codes), "🌡")


async def _geocode(ctx: Context, city: str) -> tuple[float, float, str]:
    """Resolve a place name via Open-Meteo's free geocoder (no API key)."""
    data = await ctx.bot.http.get_json(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "en", "format": "json"},
        timeout=15,
    )
    results = (data or {}).get("results")
    if not results:
        raise ValidationError(f"Could not find a place called `{truncate(city, 60)}`.")
    top = results[0]
    label = ", ".join(filter(None, [top.get("name"), top.get("country")]))
    return float(top["latitude"]), float(top["longitude"]), label


@command(
    "weather",
    category=CATEGORY,
    min_args=1,
    aliases=("dw",),
    usage="weather <city>",
    examples=("weather Dronten", "weather Tehran"),
)
async def cmd_weather(ctx: Context) -> None:
    """Seven-day forecast for a city."""
    city = ctx.raw_args.strip()
    status = await ctx.reply("🌍 Looking up…")

    lat, lon, label = await _geocode(ctx, city)
    data = await ctx.bot.http.get_json(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto", "forecast_days": 7,
        },
        timeout=20,
    )

    daily = (data or {}).get("daily") or {}
    if not daily.get("time"):
        raise ValidationError("No forecast data available for that location.")

    lines = [f"🌤 **7-day forecast — {label}**\n"]
    for index, date in enumerate(daily["time"]):
        code = daily["weather_code"][index]
        lines.append(
            f"**{date}** {_weather_icon(code)} {WEATHER_TEXT.get(code, '—')}\n"
            f"  🔺 {daily['temperature_2m_max'][index]}°C  "
            f"🔻 {daily['temperature_2m_min'][index]}°C  "
            f"💧 {daily['precipitation_probability_max'][index] or 0}%"
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
    """Next 24 hours of weather for a city."""
    city = ctx.raw_args.strip()
    status = await ctx.reply("🌍 Looking up…")

    lat, lon, label = await _geocode(ctx, city)
    data = await ctx.bot.http.get_json(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m,weather_code,precipitation_probability",
            "timezone": "auto", "forecast_days": 2,
        },
        timeout=20,
    )

    hourly = (data or {}).get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        raise ValidationError("No hourly data available for that location.")

    from datetime import datetime

    now = datetime.now().isoformat()
    start = next((i for i, t in enumerate(times) if t >= now), 0)

    lines = [f"🕐 **Next 24 hours — {label}**\n"]
    for index in range(start, min(start + 24, len(times))):
        code = hourly["weather_code"][index]
        clock = times[index][11:16]
        lines.append(
            f"`{clock}` {_weather_icon(code)} "
            f"{hourly['temperature_2m'][index]}°C · "
            f"💧{hourly['precipitation_probability'][index] or 0}%"
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
