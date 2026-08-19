"""File tools: zip, unzip, batch queue, rename, audio metadata, PDF split."""

from __future__ import annotations

import asyncio
import logging
import zipfile
from pathlib import Path

from ..errors import UsageError, ValidationError
from ..registry import Context, command
from ..utils.files import (
    guess_extension,
    safe_extract,
    sanitize_filename,
    temp_workspace,
    unique_path,
)
from ..utils.text import format_bytes, truncate

logger = logging.getLogger(__name__)

CATEGORY = "Files"


def _check_size(ctx: Context, message: object) -> None:
    """Reject files above MAX_FILE_SIZE_MB before downloading them."""
    size = getattr(getattr(message, "file", None), "size", None)
    if size and size > ctx.config.max_file_size_bytes:
        raise ValidationError(
            f"File is {format_bytes(size)}, over the "
            f"{ctx.config.max_file_size_mb} MiB limit."
        )


@command(
    "zip",
    category=CATEGORY,
    requires_reply=True,
    aliases=("zipfile",),
    usage="zip [password]",
    examples=("zip", "zip hunter2"),
)
async def cmd_zip(ctx: Context) -> None:
    """Compress the replied-to file, optionally AES-encrypted."""
    password = ctx.arg(0)
    replied = await ctx.get_reply_message()
    _check_size(ctx, replied)

    status = await ctx.reply("📦 Preparing…")

    with temp_workspace(parent=ctx.config.downloads_dir) as workspace:
        # Prefer real media; only fall back to text when there is no file at all.
        # The original checked `.text` first, so a captioned photo zipped the
        # caption and silently discarded the image.
        if replied.media and getattr(replied, "file", None):
            await ctx.bot.edit(status, "⬇️ Downloading…")
            source = Path(await replied.download_media(file=str(workspace)))
            name = sanitize_filename(replied.file.name or source.name)
        elif replied.raw_text:
            name = "message.txt"
            source = workspace / name
            source.write_text(replied.raw_text, encoding="utf-8")
        else:
            raise ValidationError("Nothing to zip in that message.")

        await ctx.bot.edit(status, "🗜 Compressing…")
        archive = workspace / f"{Path(name).stem}.zip"

        def build() -> None:
            if password:
                import pyzipper

                with pyzipper.AESZipFile(
                    archive, "w",
                    compression=pyzipper.ZIP_DEFLATED,
                    encryption=pyzipper.WZ_AES,
                ) as zf:
                    zf.setpassword(password.encode())
                    zf.write(source, name)
            else:
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.write(source, name)

        await asyncio.to_thread(build)

        await ctx.bot.edit(status, "⬆️ Uploading…")
        await ctx.client.send_file(
            ctx.chat_id,
            str(archive),
            caption=(
                f"📦 `{archive.name}` · {format_bytes(archive.stat().st_size)}"
                + (f"\n🔒 Password: `{password}`" if password else "")
            ),
            reply_to=ctx.event.id,
        )
        await status.delete()


@command(
    "unzip",
    category=CATEGORY,
    requires_reply=True,
    usage="unzip [password]",
)
async def cmd_unzip(ctx: Context) -> None:
    """Extract a replied-to ZIP archive and send back its contents."""
    password = ctx.arg(0)
    replied = await ctx.get_reply_message()

    if not replied.document:
        raise ValidationError("Reply to a `.zip` file.")
    filename = getattr(replied.file, "name", "") or ""
    if not filename.lower().endswith(".zip"):
        raise ValidationError("That does not look like a ZIP file.")
    _check_size(ctx, replied)

    status = await ctx.reply("⬇️ Downloading…")

    with temp_workspace(parent=ctx.config.downloads_dir) as workspace:
        archive_path = Path(await replied.download_media(file=str(workspace)))
        target = workspace / "extracted"

        await ctx.bot.edit(status, "🗜 Extracting…")

        def extract() -> list[Path]:
            import pyzipper

            opener = pyzipper.AESZipFile if password else zipfile.ZipFile
            with opener(archive_path) as zf:  # type: ignore[operator]
                if password:
                    zf.setpassword(password.encode())
                # safe_extract blocks zip-slip, symlink escapes and zip bombs.
                return safe_extract(
                    zf, target, max_total_bytes=ctx.config.max_file_size_bytes
                )

        try:
            files = await asyncio.to_thread(extract)
        except RuntimeError as exc:
            if "password" in str(exc).lower():
                raise ValidationError(
                    "This archive is encrypted. Use `unzip <password>`."
                ) from exc
            raise
        except zipfile.BadZipFile as exc:
            raise ValidationError("That file is not a valid ZIP archive.") from exc

        if not files:
            await ctx.bot.edit(status, "ℹ️ The archive is empty.")
            return

        for index, path in enumerate(files, 1):
            await ctx.bot.edit(status, f"⬆️ Sending {index}/{len(files)}…")
            await ctx.client.send_file(
                ctx.chat_id, str(path), caption=f"📄 `{path.name}`"
            )
        await status.delete()


@command("add", category=CATEGORY, requires_reply=True, usage="add")
async def cmd_add(ctx: Context) -> None:
    """Queue the replied-to file for a batch zip."""
    replied = await ctx.get_reply_message()
    if not getattr(replied, "file", None):
        raise ValidationError("Reply to a file.")
    _check_size(ctx, replied)

    queue = ctx.bot.zip_queue.setdefault(ctx.sender_id, [])
    if len(queue) >= 50:
        raise ValidationError("Queue is full (50 files). Run `zipit` first.")
    queue.append(replied)

    name = getattr(replied.file, "name", None) or "unnamed"
    await ctx.reply(f"✅ Queued `{truncate(name, 40)}` — **{len(queue)}** file(s) waiting.")


@command("zipqueue", category=CATEGORY, usage="zipqueue", aliases=("queue",))
async def cmd_zipqueue(ctx: Context) -> None:
    """Show the files waiting to be zipped."""
    queue = ctx.bot.zip_queue.get(ctx.sender_id, [])
    if not queue:
        await ctx.reply("ℹ️ Queue is empty. Reply to files with `add`.")
        return
    lines = [f"📦 **Queue** ({len(queue)})\n"]
    for index, message in enumerate(queue, 1):
        name = getattr(getattr(message, "file", None), "name", None) or "unnamed"
        lines.append(f"{index}. `{truncate(name, 50)}`")
    lines.append("\nRun `zipit [password]` to archive, `zipclear` to discard.")
    await ctx.reply("\n".join(lines))


@command("zipclear", category=CATEGORY, usage="zipclear")
async def cmd_zipclear(ctx: Context) -> None:
    """Discard the batch zip queue."""
    count = len(ctx.bot.zip_queue.pop(ctx.sender_id, []))
    await ctx.reply(f"🗑 Cleared {count} queued file(s).")


@command("zipit", category=CATEGORY, usage="zipit [password]")
async def cmd_zipit(ctx: Context) -> None:
    """Zip every queued file into one archive."""
    password = ctx.arg(0)
    queue = ctx.bot.zip_queue.get(ctx.sender_id, [])
    if not queue:
        raise ValidationError("Queue is empty. Reply to files with `add` first.")

    status = await ctx.reply(f"📦 Archiving {len(queue)} file(s)…")

    with temp_workspace(parent=ctx.config.downloads_dir) as workspace:
        staged: list[Path] = []
        for index, message in enumerate(queue, 1):
            await ctx.bot.edit(status, f"⬇️ Downloading {index}/{len(queue)}…")
            downloaded = await message.download_media(file=str(workspace))
            if downloaded:
                staged.append(Path(downloaded))

        if not staged:
            raise ValidationError("Could not download any queued file.")

        archive = workspace / "archive.zip"
        await ctx.bot.edit(status, "🗜 Compressing…")

        def build() -> None:
            if password:
                import pyzipper

                with pyzipper.AESZipFile(
                    archive, "w",
                    compression=pyzipper.ZIP_DEFLATED,
                    encryption=pyzipper.WZ_AES,
                ) as zf:
                    zf.setpassword(password.encode())
                    for path in staged:
                        zf.write(path, path.name)
            else:
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                    for path in staged:
                        zf.write(path, path.name)

        await asyncio.to_thread(build)

        await ctx.bot.edit(status, "⬆️ Uploading…")
        await ctx.client.send_file(
            ctx.chat_id,
            str(archive),
            caption=(
                f"📦 {len(staged)} file(s) · {format_bytes(archive.stat().st_size)}"
                + (f"\n🔒 Password: `{password}`" if password else "")
            ),
        )
        await status.delete()

    ctx.bot.zip_queue.pop(ctx.sender_id, None)


@command(
    "rename",
    category=CATEGORY,
    requires_reply=True,
    min_args=1,
    usage="rename <new name>",
    examples=('rename "holiday photos"',),
)
async def cmd_rename(ctx: Context) -> None:
    """Re-upload the replied-to file under a new name."""
    replied = await ctx.get_reply_message()
    if not getattr(replied, "file", None):
        raise ValidationError("Reply to a file.")
    _check_size(ctx, replied)

    requested = ctx.raw_args.strip()
    original = getattr(replied.file, "name", "") or "file"
    extension = guess_extension(original)

    # sanitize_filename strips ../ and absolute paths — the original code
    # concatenated user input straight into a path.
    stem = sanitize_filename(Path(requested).stem or "file")
    new_name = stem if Path(requested).suffix else f"{stem}{extension}"
    new_name = sanitize_filename(new_name)

    status = await ctx.reply("⬇️ Downloading…")
    with temp_workspace(parent=ctx.config.downloads_dir) as workspace:
        source = Path(await replied.download_media(file=str(workspace)))
        target = unique_path(workspace / new_name)
        await asyncio.to_thread(source.rename, target)

        await ctx.bot.edit(status, "⬆️ Uploading…")
        await ctx.client.send_file(
            ctx.chat_id,
            str(target),
            force_document=True,
            caption=f"✅ Renamed to `{target.name}`",
        )
        await status.delete()


@command(
    "metadata",
    category=CATEGORY,
    requires_reply=True,
    min_args=1,
    usage="metadata <title> - <artist>",
    examples=("metadata Bohemian Rhapsody - Queen",),
)
async def cmd_metadata(ctx: Context) -> None:
    """Rewrite the title and artist tags of a replied-to audio file."""
    replied = await ctx.get_reply_message()
    if not (replied.audio or replied.voice or replied.document):
        raise ValidationError("Reply to an audio file.")
    _check_size(ctx, replied)

    raw = ctx.raw_args
    if "-" not in raw:
        raise UsageError("Usage: `metadata <title> - <artist>`")
    title, _, artist = raw.partition("-")
    title, artist = title.strip(), artist.strip()
    if not title or not artist:
        raise ValidationError("Both title and artist are required.")

    status = await ctx.reply("⬇️ Downloading…")

    with temp_workspace(parent=ctx.config.downloads_dir) as workspace:
        source = Path(await replied.download_media(file=str(workspace)))
        extension = source.suffix.lower()

        def tag() -> None:
            if extension == ".mp3":
                from mutagen.easyid3 import EasyID3
                from mutagen.mp3 import MP3

                try:
                    mp3 = MP3(source, ID3=EasyID3)
                except Exception:
                    mp3 = MP3(source)
                    mp3.add_tags()
                    mp3 = MP3(source, ID3=EasyID3)
                mp3["title"] = title
                mp3["artist"] = artist
                mp3.save()
            elif extension in {".m4a", ".mp4"}:
                from mutagen.mp4 import MP4

                mp4 = MP4(source)
                mp4["\xa9nam"] = title
                mp4["\xa9ART"] = artist
                mp4.save()
            elif extension == ".flac":
                from mutagen.flac import FLAC

                flac = FLAC(source)
                flac["title"] = title
                flac["artist"] = artist
                flac.save()
            elif extension in {".ogg", ".oga"}:
                from mutagen.oggvorbis import OggVorbis

                ogg = OggVorbis(source)
                ogg["title"] = title
                ogg["artist"] = artist
                ogg.save()
            else:
                raise ValidationError(
                    f"Unsupported format `{extension or '?'}`. Use MP3, M4A, FLAC or OGG."
                )

        await ctx.bot.edit(status, "🎵 Writing tags…")
        await asyncio.to_thread(tag)

        renamed = unique_path(workspace / sanitize_filename(f"{title}{extension}"))
        await asyncio.to_thread(source.rename, renamed)

        from telethon.tl.types import DocumentAttributeAudio

        await ctx.bot.edit(status, "⬆️ Uploading…")
        await ctx.client.send_file(
            ctx.chat_id,
            str(renamed),
            caption=f"🎵 **{title}**\n👤 {artist}",
            attributes=[DocumentAttributeAudio(duration=0, title=title, performer=artist)],
        )
        await status.delete()


@command(
    "split",
    category=CATEGORY,
    requires_reply=True,
    min_args=1,
    usage="split <start>-<end>",
    examples=("split 2-5",),
)
async def cmd_split(ctx: Context) -> None:
    """Extract a page range from a replied-to PDF."""
    import re

    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", ctx.args[0])
    if not match:
        raise UsageError("Usage: `split <start>-<end>`, e.g. `split 2-5`")

    start, end = int(match.group(1)), int(match.group(2))
    if start < 1 or end < start:
        raise ValidationError("Invalid range: start must be ≥ 1 and ≤ end.")

    replied = await ctx.get_reply_message()
    mime = getattr(getattr(replied, "document", None), "mime_type", "")
    if mime != "application/pdf":
        raise ValidationError("Reply to a PDF file.")
    _check_size(ctx, replied)

    status = await ctx.reply("⬇️ Downloading…")

    with temp_workspace(parent=ctx.config.downloads_dir) as workspace:
        source = Path(await replied.download_media(file=str(workspace)))
        output = workspace / f"pages_{start}-{end}.pdf"

        def split() -> int:
            try:
                from pypdf import PdfReader, PdfWriter
            except ImportError:
                from PyPDF2 import PdfReader, PdfWriter  # type: ignore[no-redef]

            reader = PdfReader(str(source))
            total = len(reader.pages)
            if end > total:
                raise ValidationError(
                    f"This PDF has {total} page(s); {start}-{end} is out of range."
                )
            writer = PdfWriter()
            for index in range(start - 1, end):
                writer.add_page(reader.pages[index])
            with open(output, "wb") as sink:
                writer.write(sink)
            return total

        await ctx.bot.edit(status, "✂️ Splitting…")
        total = await asyncio.to_thread(split)

        await ctx.bot.edit(status, "⬆️ Uploading…")
        await ctx.client.send_file(
            ctx.chat_id,
            str(output),
            caption=f"✂️ Pages **{start}–{end}** of {total}",
        )
        await status.delete()
