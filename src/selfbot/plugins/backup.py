"""Export and import settings, quick replies, timers and admin data."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import UsageError, ValidationError
from ..registry import Context, command
from ..utils.files import temp_workspace

logger = logging.getLogger(__name__)

CATEGORY = "Admin"

BACKUP_VERSION = 1
BACKUP_SECTIONS = (
    "users",
    "channel_reactions",
    "quick_replies",
    "auto_replies",
    "welcomes",
    "timers",
    "sticker_packs",
    "app_settings",
    "plugin_state",
    "ai_providers",
)


@command(
    "backup",
    category=CATEGORY,
    sudo_only=True,
    usage="backup [--include-secrets] [--file <name>]",
)
async def cmd_backup(ctx: Context) -> None:
    """Export bot data as a JSON document (provider keys redacted by default)."""
    include_secrets = "--include-secrets" in ctx.args
    file_flag = "--file" in ctx.args
    filename = None
    if file_flag:
        index = ctx.args.index("--file")
        if index + 1 >= len(ctx.args):
            raise UsageError("Usage: `backup --file <name>`")
        filename = ctx.args[index + 1]

    data = await _build_dump(ctx, include_secrets=include_secrets)
    serialized = json.dumps(data, indent=2, default=str, ensure_ascii=False)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if include_secrets and ctx.bot.secrets is not None:
        # Encrypt the whole document with the operator's secret key.
        token = ctx.bot.secrets.encrypt(serialized)
        out_name = filename or f"selfbot-backup-{stamp}.enc"
        payload: bytes = token.encode("ascii")
        caption = (
            f"🔐 **Encrypted backup** · v{BACKUP_VERSION}\n"
            "This file needs your `secret.key` to restore."
        )
    else:
        out_name = filename or f"selfbot-backup-{stamp}.json"
        payload = serialized.encode("utf-8")
        caption = (
            f"💾 **Backup** · v{BACKUP_VERSION}\n"
            f"Sections: {', '.join(BACKUP_SECTIONS)}\n"
            "Provider API keys were redacted. Use `--include-secrets` to "
            "include them (the file is encrypted)."
        )

    with temp_workspace(parent=ctx.config.downloads_dir) as workspace:
        out_path = workspace / out_name
        out_path.write_bytes(payload)
        await ctx.client.send_file(
            ctx.chat_id,
            str(out_path),
            caption=caption,
            reply_to=ctx.event.id,
        )


@command(
    "restore",
    category=CATEGORY,
    sudo_only=True,
    requires_reply=True,
    usage="restore [--force]",
)
async def cmd_restore(ctx: Context) -> None:
    """Restore data from a backup document (reply to the file)."""
    force = "--force" in ctx.args
    replied = await ctx.get_reply_message()
    if not replied or not (replied.document or replied.file):
        raise ValidationError("Reply to a backup `.json`/`.enc` file.")

    name = getattr(getattr(replied, "file", None), "name", "") or "backup"
    if not name.lower().endswith((".json", ".enc")):
        raise ValidationError("Expected a `.json` or `.enc` backup file.")

    with temp_workspace(parent=ctx.config.downloads_dir) as workspace:
        path = Path(await replied.download_media(file=str(workspace)))
        raw = await asyncio.to_thread(path.read_bytes)

    if name.lower().endswith(".enc"):
        if ctx.bot.secrets is None:
            raise ValidationError(
                "This backup is encrypted but encryption is not available "
                "(`cryptography` missing)."
            )
        try:
            text = ctx.bot.secrets.decrypt(raw.decode("ascii"))
        except Exception as exc:
            raise ValidationError(f"Could not decrypt backup: {exc}") from exc
    else:
        text = raw.decode("utf-8", errors="replace")

    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"Not a valid JSON backup: {exc}") from exc

    if not isinstance(data, dict) or data.get("version") != BACKUP_VERSION:
        raise ValidationError(
            f"Unrecognised backup (expected version {BACKUP_VERSION})."
        )

    summary = [f"{section}: {len(data.get(section, []) or [])}" for section in BACKUP_SECTIONS]
    if not force:
        prompt = (
            "⚠️ Restore this backup? Existing rows will be updated, missing "
            "rows inserted. This cannot be undone.\n\n"
            + "\n".join(f"• {line}" for line in summary)
        )
        if not await ctx.bot.confirm(ctx.event, prompt):
            await ctx.reply("👍 Restore cancelled.")
            return

    counts = await _import_dump(ctx, data)
    report = "\n".join(f"• {section}: {counts[section]}" for section in BACKUP_SECTIONS)
    await ctx.reply(f"✅ **Restore complete**\n{report}")


# --------------------------------------------------------------------------
# Export / import helpers
# --------------------------------------------------------------------------


async def _build_dump(ctx: Context, *, include_secrets: bool) -> dict[str, Any]:
    db = ctx.db
    dump = await db.export_rows()

    if not include_secrets:
        for provider in dump.get("ai_providers", []):
            key = provider.get("api_key") or ""
            provider["api_key"] = f"redacted:{key[-4:]}" if key else ""

    return {
        "version": BACKUP_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "selfbot",
        **dump,
    }


async def _import_dump(ctx: Context, data: dict[str, Any]) -> dict[str, int]:
    db = ctx.db
    counts: dict[str, int] = dict.fromkeys(BACKUP_SECTIONS, 0)

    # Users
    for row in data.get("users", []) or []:
        try:
            await db.add_admin(int(row["id"]), row.get("username"))
            counts["users"] += 1
        except Exception:
            logger.debug("Could not import user %s", row, exc_info=True)

    # Channel reactions
    for row in data.get("channel_reactions", []) or []:
        try:
            await db.set_reaction(row["channel"], row["emoji"])
            counts["channel_reactions"] += 1
        except Exception:
            logger.debug("Could not import reaction %s", row, exc_info=True)

    # Quick replies
    for row in data.get("quick_replies", []) or []:
        try:
            await db.set_quick_reply(
                int(row["user_id"]), row["alias"], row["message"]
            )
            counts["quick_replies"] += 1
        except Exception:
            logger.debug("Could not import quick reply %s", row, exc_info=True)

    # Auto replies
    for row in data.get("auto_replies", []) or []:
        try:
            await db.set_auto_reply(
                int(row["chat_id"]),
                row["mode"],
                row["trigger_text"],
                row["reply_text"],
                reply_condition=row.get("reply_condition", "any"),
            )
            counts["auto_replies"] += 1
        except Exception:
            logger.debug("Could not import auto reply %s", row, exc_info=True)

    # Welcomes
    for row in data.get("welcomes", []) or []:
        try:
            await db.set_welcome_message(int(row["chat_id"]), row["message"])
            await db.set_welcome_enabled(int(row["chat_id"]), bool(row.get("enabled")))
            counts["welcomes"] += 1
        except Exception:
            logger.debug("Could not import welcome %s", row, exc_info=True)

    # Timers: only import active ones; do not clobber existing hashes.
    from ..db import Timer
    from ..db import utcnow as _utcnow

    for row in data.get("timers", []) or []:
        try:
            if await db.get_timer(row["hash"]) is not None:
                continue
            end = row.get("end_time")
            end_dt = _parse_dt(end) or _utcnow()
            await db.create_timer(
                Timer(
                    hash=row["hash"],
                    user_id=int(row["user_id"]),
                    chat_id=int(row["chat_id"]),
                    title=row.get("title", "restored"),
                    duration_seconds=int(row.get("duration_seconds", 0)),
                    end_time=end_dt,
                    message_id=row.get("message_id"),
                    is_active=True,
                )
            )
            counts["timers"] += 1
        except Exception:
            logger.debug("Could not import timer %s", row, exc_info=True)

    # Sticker packs
    for row in data.get("sticker_packs", []) or []:
        try:
            await db.add_sticker_pack(
                row["name"], row["title"], int(row["owner_id"])
            )
            counts["sticker_packs"] += 1
        except Exception:
            logger.debug("Could not import sticker pack %s", row, exc_info=True)

    # App settings
    for row in data.get("app_settings", []) or []:
        try:
            key, value = row["key"], row["value"]
            if isinstance(value, str):
                try:
                    import json

                    value = json.loads(value)
                except (ValueError, TypeError):
                    pass
            await db.set_setting(key, value)
            counts["app_settings"] += 1
        except Exception:
            logger.debug("Could not import setting %s", row, exc_info=True)

    # Plugin state
    for row in data.get("plugin_state", []) or []:
        try:
            await db.set_plugin_state(
                row["name"],
                row["source"],
                version=row.get("version"),
                enabled=bool(row.get("enabled", True)),
            )
            counts["plugin_state"] += 1
        except Exception:
            logger.debug("Could not import plugin state %s", row, exc_info=True)

    # AI providers: skip redacted stubs so we never overwrite real keys.
    for row in data.get("ai_providers", []) or []:
        api_key = row.get("api_key", "") or ""
        if api_key.startswith("redacted:"):
            continue
        try:
            existing = await db.get_provider(row["name"])
            if existing is None:
                await db.add_provider(
                    row["name"],
                    row["base_url"],
                    api_key,
                    model=row.get("model", ""),
                    kind=row.get("kind", "openai"),
                    is_default=bool(row.get("is_default")),
                )
            else:
                await db.update_provider(
                    row["name"],
                    base_url=row["base_url"],
                    api_key=api_key,
                    model=row.get("model", existing.model),
                )
            counts["ai_providers"] += 1
        except Exception:
            logger.debug("Could not import provider %s", row, exc_info=True)

    # Clear caches so new providers/settings show up immediately.
    if getattr(ctx.bot, "ai", None) is not None:
        ctx.bot.ai.invalidate()
    ctx.bot.invalidate_auto_reply_cache(None)
    ctx.bot.invalidate_reaction_cache()

    return counts


def _parse_dt(value: Any) -> Any:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
