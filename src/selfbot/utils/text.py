"""Text formatting, sizing and Persian/RTL helpers."""

from __future__ import annotations

import html
import re

__all__ = [
    "TELEGRAM_LIMIT",
    "chunk_text",
    "escape_markdown",
    "format_bytes",
    "format_duration",
    "format_duration_long",
    "has_rtl",
    "progress_bar",
    "styled_clock",
    "styled_number",
    "truncate",
]

TELEGRAM_LIMIT = 4096

_STYLED_DIGITS = {
    "0": "𝟬", "1": "𝟭", "2": "𝟮", "3": "𝟯", "4": "𝟰",
    "5": "𝟱", "6": "𝟲", "7": "𝟳", "8": "𝟴", "9": "𝟵",
}

# Arabic, Arabic Supplement, Extended-A, Presentation Forms, plus Hebrew.
_RTL_PATTERN = re.compile(
    r"[\u0590-\u05FF\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]"
)

_MD_SPECIALS = r"_*[]()~`>#+-=|{}.!"


def has_rtl(text: str) -> bool:
    """True when the text contains right-to-left script."""
    return bool(_RTL_PATTERN.search(text))


def escape_markdown(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    return "".join(f"\\{ch}" if ch in _MD_SPECIALS else ch for ch in text)


def escape_html(text: str) -> str:
    return html.escape(text, quote=False)


def truncate(text: str, limit: int = 100, suffix: str = "…") -> str:
    """Shorten text to ``limit`` characters, appending an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


def chunk_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split text into Telegram-sized chunks on the nicest boundary available.

    Prefers paragraph breaks, then line breaks, then spaces, and only splits
    mid-word when a single token exceeds the limit. The original bot sliced
    blindly every 4096 characters, which routinely cut words and broke
    Markdown entities in half.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    remaining = text

    while len(remaining) > limit:
        window = remaining[:limit]
        split_at = -1
        for separator in ("\n\n", "\n", ". ", " "):
            candidate = window.rfind(separator)
            # Only accept a boundary that keeps chunks reasonably full.
            if candidate > limit * 0.5:
                split_at = candidate + (len(separator) if separator != " " else 1)
                break
        if split_at <= 0:
            split_at = limit
        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip("\n")

    if remaining:
        chunks.append(remaining)
    return chunks


def format_bytes(size: float) -> str:
    """Human-readable byte count."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def format_duration(seconds: float) -> str:
    """Compact duration, e.g. ``1h 05m 09s``."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours:02d}h" if parts else f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes:02d}m" if parts else f"{minutes}m")
    parts.append(f"{secs:02d}s" if parts else f"{secs}s")
    return " ".join(parts)


def format_duration_long(seconds: float) -> str:
    """Verbose duration, e.g. ``1 hour, 5 minutes, 9 seconds``."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    parts: list[str] = []
    for value, label in ((days, "day"), (hours, "hour"), (minutes, "minute")):
        if value:
            parts.append(f"{value} {label}{'s' if value != 1 else ''}")
    if secs or not parts:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return ", ".join(parts)


def styled_number(value: int, pad: int = 2) -> str:
    """Render a number with bold sans-serif digits."""
    return "".join(_STYLED_DIGITS.get(ch, ch) for ch in str(value).zfill(pad))


def styled_clock(seconds: int) -> str:
    """Styled ``DD : HH : MM : SS``, omitting leading zero units."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    if days:
        units = (days, hours, minutes, secs)
    elif hours:
        units = (hours, minutes, secs)
    else:
        units = (minutes, secs)
    return " : ".join(styled_number(u) for u in units)


def progress_bar(
    fraction: float,
    width: int = 16,
    filled: str = "█",
    empty: str = "░",
) -> str:
    """Render a progress bar for a 0..1 fraction."""
    fraction = max(0.0, min(1.0, fraction))
    filled_count = round(width * fraction)
    return filled * filled_count + empty * (width - filled_count)


def shape_rtl(text: str) -> str:
    """Reshape Arabic/Persian glyphs and apply the bidi algorithm.

    Used for image and PDF rendering, where the renderer will not do this for
    us. Returns the input unchanged when the optional dependencies are absent.
    """
    if not has_rtl(text):
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text
