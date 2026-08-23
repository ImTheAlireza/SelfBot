"""AI commands: ``gpt``, provider management, model selection and status.

The HTTP/parsing logic lives in :mod:`selfbot.services.ai`; this module is the
thin command layer that turns Telegram invocations into manager calls. The
historical private names (``_extract_answer``, ``_format_rapidapi_error`` and
the provider constants) are re-exported so existing tests and any third-party
imports keep working unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..errors import FeatureDisabledError, UsageError, ValidationError
from ..registry import Context, command
from ..services.ai import (
    ANYAPI_DEFAULT_BASE_URL,
    ANYAPI_DEFAULT_MODEL,
    BACKUP_RAPIDAPI_CHAT_URL,
    BACKUP_RAPIDAPI_GENERE,
    BACKUP_RAPIDAPI_HOST,
    BLUESMINDS_DEFAULT_BASE_URL,
    RAPIDAPI_CHAT_URL,
    RAPIDAPI_HOST,
    RAPIDAPI_MODEL,
    SYSTEM_PROMPT,
    AIManager,
)
from ..services.ai import (
    extract_answer as _extract_answer,
)
from ..services.ai import (
    format_rapidapi_error as _format_rapidapi_error,
)
from ..utils.text import truncate

logger = logging.getLogger(__name__)

CATEGORY = "AI"

__all__ = [
    "ANYAPI_DEFAULT_BASE_URL",
    "ANYAPI_DEFAULT_MODEL",
    "BACKUP_RAPIDAPI_CHAT_URL",
    "BACKUP_RAPIDAPI_GENERE",
    "BACKUP_RAPIDAPI_HOST",
    "BLUESMINDS_DEFAULT_BASE_URL",
    "RAPIDAPI_CHAT_URL",
    "RAPIDAPI_HOST",
    "RAPIDAPI_MODEL",
    "SYSTEM_PROMPT",
    "_extract_answer",
    "_format_rapidapi_error",
]


def get_manager(ctx: Context) -> AIManager:
    """Return the bot's AIManager, creating one lazily (used by tests)."""
    bot = ctx.bot
    manager = getattr(bot, "ai", None)
    if manager is None:
        manager = AIManager(bot)
        bot.ai = manager
    return manager


async def _delete_status(message: Any) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete the GPT status message", exc_info=True)


# --------------------------------------------------------------------------
# gpt
# --------------------------------------------------------------------------


@command(
    "gpt",
    category=CATEGORY,
    min_args=1,
    usage="gpt <prompt>",
    examples=("gpt What is the meaning of life?",),
)
async def cmd_gpt(ctx: Context) -> None:
    """Ask the active AI model. Shows which provider/model answered."""
    prompt = ctx.raw_args.strip()
    if not prompt:
        raise UsageError(f"Usage: `{ctx.config.command_prefix}gpt <prompt>`")

    manager = get_manager(ctx)
    providers = await manager.providers(enabled_only=True)
    if not providers:
        raise FeatureDisabledError(
            "No AI provider is configured. Add one with "
            "`ai add <name> <base_url> <api_key> [model]`."
        )

    # Reply-to context: when the command is a reply, feed the quoted message
    # in as context so the answer directly addresses it.
    if ctx.event.is_reply:
        replied = await ctx.get_reply_message()
        quoted = (getattr(replied, "raw_text", "") or "").strip()
        if quoted:
            instruction = prompt
            prompt = (
                "Message being replied to:\n"
                f"\"\"\"\n{truncate(quoted, 6000)}\n\"\"\"\n\n"
                f"Instruction: {instruction}"
            )

    memory_on = await _memory_enabled(ctx, manager)
    status = await ctx.reply("🤖 Thinking…")
    try:
        answer = await manager.chat(
            prompt, chat_id=ctx.chat_id, history=memory_on
        )
        used_provider = getattr(manager, "_last_provider", "") or ""
        used_model = getattr(manager, "_last_model", "") or ""
    finally:
        await _delete_status(status)

    footer = ""
    if used_provider:
        shown_model = used_model or "(default)"
        footer = f"\n\n_— via {used_provider} · `{shown_model}`_"
    await ctx.reply(answer + footer)


async def _memory_enabled(ctx: Context, manager: AIManager) -> bool:
    """Per-chat memory toggle, defaulting on."""
    if ctx.config.ai.memory_turns <= 0:
        return False
    saved = await ctx.db.get_setting(f"ai.memory.{ctx.chat_id}", True)
    return bool(saved)


@command(
    "gptmemory",
    category=CATEGORY,
    sudo_only=False,
    usage="gptmemory <on|off|clear|turns|status>",
    examples=("gptmemory off", "gptmemory turns 6", "gptmemory clear"),
)
async def cmd_gptmemory(ctx: Context) -> None:
    """Control per-chat AI conversation memory."""
    if not ctx.args:
        raise UsageError(
            "Usage: `gptmemory <on|off|clear|turns <n>|status>`"
        )

    manager = get_manager(ctx)
    action = ctx.args[0].lower()

    if action == "on":
        await ctx.db.set_setting(f"ai.memory.{ctx.chat_id}", True)
        await ctx.reply("✅ AI memory is **on** for this chat.")
    elif action == "off":
        await ctx.db.set_setting(f"ai.memory.{ctx.chat_id}", False)
        await ctx.reply("⏸ AI memory is **off** for this chat.")
    elif action == "clear":
        removed = await ctx.db.clear_ai_messages(ctx.chat_id)
        await ctx.reply(f"🗑 Cleared {removed} remembered message(s) for this chat.")
    elif action == "turns":
        if len(ctx.args) < 2 or not ctx.args[1].isdigit():
            raise UsageError("Usage: `gptmemory turns <4..50>`")
        turns = int(ctx.args[1])
        if not 1 <= turns <= 50:
            raise ValidationError("Turns must be between 1 and 50.")
        await ctx.db.set_setting("ai.memory_turns", turns)
        await ctx.reply(f"✅ Memory window set to {turns} turns.")
    elif action == "status":
        enabled = await _memory_enabled(ctx, manager)
        turns = await ctx.db.get_setting("ai.memory_turns", ctx.config.ai.memory_turns)
        stored = await ctx.db.count_ai_messages(ctx.chat_id)
        await ctx.reply(
            f"🧠 **AI memory**\n"
            f"State: {'🟢 on' if enabled else '🔴 off'}\n"
            f"Window: {turns} turns\n"
            f"Stored messages in this chat: {stored}"
        )
    else:
        raise ValidationError(
            f"Unknown action `{action}`. Try on/off/clear/turns/status."
        )


@command(
    "gptedit",
    category=CATEGORY,
    sudo_only=True,
    requires_reply=True,
    usage="gptedit [instruction]",
    examples=("gptedit make it more formal", "gptedit translate to English"),
)
async def cmd_gptedit(ctx: Context) -> None:
    """Rewrite one of your own messages with AI, editing it in place."""
    replied = await ctx.get_reply_message()
    if replied is None:
        raise ValidationError("Reply to a message to rewrite it.")

    my_id = getattr(ctx.bot.me, "id", None)
    if my_id is not None and getattr(replied, "sender_id", None) != my_id:
        raise ValidationError(
            "You can only rewrite **your own** messages with `gptedit`."
        )

    original = (getattr(replied, "raw_text", "") or "").strip()
    if not original:
        raise ValidationError("The replied message has no text to rewrite.")

    instruction = ctx.raw_args.strip() or "Improve the wording while keeping the meaning."
    prompt = (
        "Rewrite the following message. Return ONLY the rewritten message, "
        "with no explanation, no quotes, and no preamble.\n\n"
        f"Instruction: {instruction}\n\n"
        f"Message:\n\"\"\"\n{truncate(original, 6000)}\n\"\"\""
    )

    status = await ctx.reply("🤖 Rewriting…")
    manager = get_manager(ctx)
    try:
        rewritten = await manager.chat(prompt, history=False)
    finally:
        await _delete_status(status)

    # Edit the original message; the command message itself is removed so the
    # chat looks as though the answer was authored directly.
    try:
        await ctx.bot.edit(replied, rewritten)
    except Exception as exc:
        await ctx.reply(f"❌ Could not edit the message: `{exc}`")
        return

    try:
        await ctx.event.delete()
    except Exception:
        logger.debug("Could not delete the gptedit command message", exc_info=True)


# --------------------------------------------------------------------------
# summarize
# --------------------------------------------------------------------------


@command(
    "summarize",
    category=CATEGORY,
    usage="summarize [n] [--lang en|fa] [--brief|--detailed]",
    examples=("summarize", "summarize 50 --brief", "summarize --lang fa"),
)
async def cmd_summarize(ctx: Context) -> None:
    """Summarize a replied message, document, or the last N messages."""
    args = list(ctx.args)
    brief = "--brief" in args
    detailed = "--detailed" in args
    args = [a for a in args if a not in {"--brief", "--detailed"}]

    lang = None
    if "--lang" in args:
        idx = args.index("--lang")
        if idx + 1 >= len(args):
            raise UsageError("Usage: `summarize ... --lang en|fa`")
        lang = args.pop(idx + 1).lower()
        args.pop(idx)
        if lang not in {"en", "fa", "english", "persian", "farsi"}:
            raise ValidationError("Language must be `en` or `fa`.")

    count = 0
    if args and args[0].isdigit():
        count = int(args.pop(0))
        if not 1 <= count <= 500:
            raise ValidationError("Count must be between 1 and 500.")
    if args:
        raise UsageError(
            "Usage: `summarize [n] [--lang en|fa] [--brief|--detailed]`"
        )

    manager = get_manager(ctx)
    providers = await manager.providers(enabled_only=True)
    if not providers:
        from ..errors import FeatureDisabledError

        raise FeatureDisabledError(
            "No AI provider is configured. Add one with "
            "`provider add <name> <base_url> <api_key> [model]`."
        )

    text, label = await _collect_summary_text(ctx, count)
    if not text.strip():
        raise ValidationError("Nothing to summarize.")

    budget = 30000
    if len(text) > budget:
        text = text[:budget] + "\n…[truncated]"

    style = (
        "Write a detailed summary in paragraphs, covering the key points."
        if detailed
        else "Write a concise summary as a short bullet list."
        if brief
        else "Write a concise summary with the key points in a few bullets."
    )
    if lang in {"fa", "persian", "farsi"}:
        style += " Respond in Persian (Farsi)."
    elif lang in {"en", "english"}:
        style += " Respond in English."

    prompt = (
        f"You are a summarization assistant. {style}\n\n"
        f"Content ({label}):\n\"\"\"\n{text}\n\"\"\""
    )

    status = await ctx.reply("📝 Summarizing…")
    try:
        summary = await manager.chat(prompt, history=False)
    finally:
        await _delete_status(status)
    await ctx.reply(f"📝 **Summary** ({label})\n\n{summary}")


async def _collect_summary_text(ctx: Context, count: int) -> tuple[str, str]:
    """Return (text, label) from a reply/document or the last ``count`` messages."""
    replied = await ctx.get_reply_message() if ctx.event.is_reply else None

    if count > 0:
        return await _conversation_text(ctx, count)

    if replied is None:
        raise UsageError(
            "Reply to a message or document, or use `summarize <n>`."
        )

    # 1. A text-bearing message.
    raw = (getattr(replied, "raw_text", "") or "").strip()
    if raw and not (replied.photo or replied.document):
        return raw, "replied message"

    # 2. A document — download and extract.
    if replied.document or replied.photo:
        from pathlib import Path

        from ..utils.files import temp_workspace

        with temp_workspace(parent=ctx.config.downloads_dir) as workspace:
            path = Path(await replied.download_media(file=str(workspace)))
            extracted = _extract_document_text(path)
        if extracted:
            return extracted, path.name

    if raw:
        return raw, "replied message"

    raise ValidationError("Could not extract any text from that message.")


async def _conversation_text(ctx: Context, count: int) -> tuple[str, str]:
    lines: list[str] = []
    seen = 0
    async for message in ctx.client.iter_messages(ctx.chat_id, limit=count):
        text = (getattr(message, "raw_text", "") or "").strip()
        if not text:
            continue
        sender = await message.get_sender()
        name = (
            getattr(sender, "first_name", None)
            or getattr(sender, "username", None)
            or str(getattr(message, "sender_id", "?"))
        )
        lines.append(f"{name}: {text}")
        seen += 1
    lines.reverse()  # chronological order
    return "\n".join(lines), f"last {seen} messages"


def _extract_document_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".csv", ".log", ".json", ".xml", ".html", ".py", ""}:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    if suffix == ".pdf":
        try:
            import pypdf

            reader = pypdf.PdfReader(str(path))
            return "\n".join(
                (page.extract_text() or "") for page in reader.pages
            ).strip()
        except Exception as exc:
            logger.debug("PDF extraction failed: %s", exc)
            return ""
    # Fall back to a best-effort text decode.
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------
# ai — one command for everything provider/model related
# --------------------------------------------------------------------------


@command(
    "ai",
    category=CATEGORY,
    sudo_only=True,
    usage=(
        "ai [status|list|add|remove|default|enable|disable|test|model] ..."
    ),
    examples=(
        "ai",
        "ai status",
        "ai add openai https://api.openai.com/v1 sk-xxx gpt-4o-mini",
        "ai default bluesminds",
        "ai model luna",
        "ai model list",
    ),
)
async def cmd_ai(ctx: Context) -> None:
    """Manage AI providers and the active model in one place."""
    manager = get_manager(ctx)
    action = (ctx.args[0].lower() if ctx.args else "status")
    args = ctx.args[1:]

    if action in {"status", "list", "ls", ""}:
        await _ai_status(ctx, manager, verbose=(action in {"list", "ls"}))
    elif action == "add":
        await _ai_add(ctx, manager, args)
    elif action == "remove":
        if not args:
            raise UsageError("Usage: `ai remove <name>`")
        if await manager.remove_provider(args[0]):
            await ctx.reply(f"✅ Removed provider `{args[0]}`.")
        else:
            await ctx.reply(f"ℹ️ No provider named `{args[0]}`.")
    elif action == "default":
        if not args:
            raise UsageError("Usage: `ai default <name>`")
        try:
            await manager.set_default(args[0])
        except KeyError:
            raise ValidationError(f"No provider named `{args[0]}`.") from None
        model, provider = await manager.current_model()
        await ctx.reply(
            f"✅ **{args[0]}** is now the default.\n"
            f"🧠 Active model: `{model}` via `{provider}`."
        )
    elif action in {"enable", "disable"}:
        await _ai_toggle(ctx, manager, args, enabled=(action == "enable"))
    elif action == "test":
        if not args:
            raise UsageError("Usage: `ai test <name>`")
        await ctx.reply(f"🔌 Testing `{args[0]}`…")
        ok, detail = await manager.test_provider(args[0])
        await ctx.reply(f"{'✅' if ok else '❌'} **{args[0]}** — {detail}")
    elif action == "model":
        await _ai_model(ctx, manager, args)
    else:
        raise ValidationError(
            f"Unknown action `{action}`. Try: status, add, remove, default, "
            "enable, disable, test, model."
        )


# Aliases for the old separate commands, kept for muscle memory.
@command("aistatus", category=CATEGORY, sudo_only=True, usage="aistatus", hidden=True)
async def cmd_aistatus_alias(ctx: Context) -> None:
    """Deprecated alias for `ai status`."""
    ctx.args = ["status"]
    await cmd_ai(ctx)


@command(
    "provider",
    category=CATEGORY,
    sudo_only=True,
    usage="provider <add|list|default|...>",
    hidden=True,
)
async def cmd_provider_alias(ctx: Context) -> None:
    """Deprecated alias for `ai`."""
    await cmd_ai(ctx)


async def _ai_status(ctx: Context, manager: AIManager, *, verbose: bool) -> None:
    statuses = await manager.status()
    if not statuses:
        prefix = ctx.config.command_prefix
        await ctx.reply(
            "🧠 **No AI providers configured.**\n\n"
            f"Add one:\n`{prefix}ai add <name> <base_url> <api_key> [model]`\n\n"
            "Example:\n`ai add openai https://api.openai.com/v1 sk-xxx gpt-4o-mini`"
        )
        return

    model, default_name = await manager.current_model()
    available = sum(1 for s in statuses if s.available)
    lines = [
        "🧠 **AI status**\n",
        f"Active: `{model}` via **{default_name}** "
        f"· {available}/{len(statuses)} up\n",
    ]
    for s in statuses:
        p = s.provider
        if not p.enabled:
            icon = "⏸"
        elif s.cooldown_remaining:
            icon = "❄️"
        elif p.last_error:
            icon = "⚠️"
        else:
            icon = "✅"
        default_marker = " ◂ default" if p.is_default else ""
        lines.append(f"{icon} **{p.name}**{default_marker}")
        if verbose:
            lines.append(f"  `{p.base_url}`")
        shown_model = p.model or "(provider default)"
        stats = f"  model: `{shown_model}`"
        if verbose:
            stats += f" · key: `{p.redacted_key}` · {p.success_count}ok/{p.failure_count}fail"
        lines.append(stats)
        if s.cooldown_remaining:
            lines.append(f"  ❄️ cooldown {s.cooldown_remaining}s")
        if p.last_error and verbose:
            lines.append(f"  ⚠️ {truncate(p.last_error, 120)}")
    prefix = ctx.config.command_prefix
    lines.append(
        f"\n`{prefix}ai model <name>` switch · `{prefix}ai model list` browse · "
        f"`{prefix}ai add ...` add"
    )
    await ctx.reply("\n".join(lines))


async def _ai_model(ctx: Context, manager: AIManager, args: list[str]) -> None:
    """`ai model` — show/set/list the active model."""
    sub = args[0].lower() if args else ""

    if sub in {"", "current"}:
        model, provider = await manager.current_model()
        await ctx.reply(f"🧠 Active model: `{model}` via **{provider}**.")
        return

    if sub == "list":
        status = await ctx.reply("🔍 Discovering models…")
        models = await manager.list_models()
        await _delete_status(status)
        if not models:
            await ctx.reply(
                "ℹ️ Couldn't discover models. You can still set one directly, "
                "e.g. `ai model gpt-4o-mini`."
            )
            return
        current, _ = await manager.current_model()
        lines = [f"🧠 **Models** ({len(models)}) — active: `{current}`\n"]
        for entry in models[:60]:
            marker = " ◂ active" if entry["current"] else ""
            providers = ", ".join(entry["providers"])
            lines.append(f"• `{entry['id']}` _({providers})_{marker}")
        if len(models) > 60:
            lines.append(f"\n_…and {len(models) - 60} more_")
        lines.append("\nSet it with `ai model <id>` or `ai model <provider>/<id>`.")
        await ctx.reply("\n".join(lines))
        return

    # Everything else is treated as the model to set.
    raw = " ".join(args).strip()
    if "/" in raw:
        # provider/model form
        provider_name, _, model_name = raw.partition("/")
        provider_name = provider_name.strip().lower()
        model_name = model_name.strip()
        if not await ctx.db.get_provider(provider_name):
            raise ValidationError(f"No provider named `{provider_name}`.")
        await manager.set_provider_model(provider_name, model_name)
        set_provider = provider_name
        set_model = model_name
    else:
        try:
            set_model, set_provider = await manager.set_active_model(raw)
        except KeyError as exc:
            raise ValidationError(f"No provider named `{exc.args[0]}`.") from None

    await ctx.reply(
        f"✅ Active model set to `{set_model}` via **{set_provider}**.\n"
        "The next `gpt` call will use it."
    )


async def _ai_add(ctx: Context, manager: AIManager, args: list[str]) -> None:
    if len(args) < 3:
        raise UsageError(
            "Usage: `ai add <name> <base_url> <api_key> [model]`"
        )
    name = args[0].lower()
    base_url = args[1]
    api_key = args[2]
    model = " ".join(args[3:]).strip()

    if not name.replace("_", "").replace("-", "").isalnum():
        raise ValidationError("Provider name may only contain letters, numbers, '-' and '_'.")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValidationError("Base URL must start with http:// or https://")
    if len(api_key) < 4:
        raise ValidationError("That API key looks too short.")

    existing = await ctx.db.get_provider(name)
    make_default = existing is None and await ctx.db.count_providers() == 0

    if existing is not None:
        await ctx.db.update_provider(
            name, base_url=base_url, api_key=api_key,
            model=model or existing.model,
        )
        manager.invalidate()
        await ctx.reply(f"✅ Updated provider `{name}`.")
        return

    await manager.add_openai_provider(
        name, base_url, api_key, model=model, is_default=make_default
    )
    suffix = " (set as default)" if make_default else ""
    await ctx.reply(
        f"✅ Added provider `{name}`{suffix}.\n"
        f"Set its model with `ai model {name}/<model>` or `ai model <model>` "
        "to use it on the default provider."
    )


async def _ai_toggle(
    ctx: Context, manager: AIManager, args: list[str], *, enabled: bool
) -> None:
    if not args:
        state = "enable" if enabled else "disable"
        raise UsageError(f"Usage: `ai {state} <name>`")
    name = args[0]
    if not await ctx.db.get_provider(name):
        raise ValidationError(f"No provider named `{name}`.")
    await ctx.db.update_provider(name, enabled=enabled)
    manager.invalidate()
    verb = "enabled" if enabled else "disabled"
    await ctx.reply(f"✅ {verb.capitalize()} `{name}`.")


# --------------------------------------------------------------------------
# model (short alias for `ai model`)
# --------------------------------------------------------------------------


@command(
    "model",
    category=CATEGORY,
    sudo_only=True,
    usage="model [list|<model>|current]",
    examples=("model", "model list", "model luna", "model bluesminds/luna"),
)
async def cmd_model(ctx: Context) -> None:
    """Show or set the active AI model (shortcut for `ai model`)."""
    manager = get_manager(ctx)
    if not ctx.args:
        m, provider = await manager.current_model()
        await ctx.reply(f"🧠 Active model: `{m}` via **{provider}**.")
        return
    # Route through the ai model handler, but strip "model" from args.
    ctx.args = ["model", *ctx.args]
    await cmd_ai(ctx)


# --------------------------------------------------------------------------
# gptmodel — deprecated alias kept for backward compatibility
# --------------------------------------------------------------------------


@command(
    "gptmodel",
    category=CATEGORY,
    sudo_only=True,
    usage="gptmodel [list|set|current]",
    hidden=True,
)
async def cmd_gptmodel_alias(ctx: Context) -> None:
    """Deprecated: use `model` or `ai model`."""
    manager = get_manager(ctx)
    if not ctx.args:
        m, provider = await manager.current_model()
        await ctx.reply(
            f"🧠 Active model: `{m}` via **{provider}**.\n"
            "_(This command is now `model`.)_"
        )
        return
    sub = ctx.args[0].lower()
    if sub == "list":
        ctx.args = ["model", "list"]
        await cmd_ai(ctx)
    elif sub == "current":
        m, provider = await manager.current_model()
        await ctx.reply(f"🧠 Active model: `{m}` via **{provider}**.")
    elif sub == "set":
        ctx.args = ["model", *ctx.args[1:]]
        await cmd_ai(ctx)
    elif sub == "clear":
        await ctx.reply(
            "ℹ️ There's no global override anymore — just set the model you "
            "want with `model <name>`."
        )
    else:
        raise ValidationError("Try `model list`, `model <name>`, or `model current`.")


async def _provider_add(ctx: Context, manager: AIManager) -> None:
    if len(ctx.args) < 4:
        raise UsageError(
            "Usage: `provider add <name> <base_url> <api_key> [model]`"
        )
    name = ctx.args[1].lower()
    base_url = ctx.args[2]
    api_key = ctx.args[3]
    model = " ".join(ctx.args[4:]).strip()

    if not name.replace("_", "").replace("-", "").isalnum():
        raise ValidationError("Provider name may only contain letters, numbers, '-' and '_'.")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValidationError("Base URL must start with http:// or https://")
    if len(api_key) < 4:
        raise ValidationError("That API key looks too short.")

    existing = await ctx.db.get_provider(name)
    make_default = existing is None and await ctx.db.count_providers() == 0

    if existing is not None:
        await ctx.db.update_provider(
            name, base_url=base_url, api_key=api_key, model=model or existing.model
        )
        manager.invalidate()
        await ctx.reply(f"✅ Updated provider `{name}`.")
        return

    await manager.add_openai_provider(
        name, base_url, api_key, model=model, is_default=make_default
    )
    suffix = " (set as default)" if make_default else ""
    await ctx.reply(
        f"✅ Added provider `{name}` at `{base_url}`{suffix}.\n"
        "Run `provider test <name>` to verify the key, or `aistatus`."
    )


async def _provider_list(ctx: Context, manager: AIManager) -> None:
    statuses = await manager.status()
    if not statuses:
        await ctx.reply("ℹ️ No providers configured. Use `provider add`.")
        return
    lines = ["🧠 **Providers**\n"]
    for s in statuses:
        p = s.provider
        icon = "✅" if s.available else ("⏸" if not p.enabled else "❄️")
        default = " _(default)_" if p.is_default else ""
        lines.append(
            f"{icon} `{p.name}`{default} — `{p.model or 'default'}` "
            f"· {p.redacted_key}"
        )
    await ctx.reply("\n".join(lines))


async def _provider_toggle(
    ctx: Context, manager: AIManager, enabled: bool
) -> None:
    if len(ctx.args) < 2:
        state = "enable" if enabled else "disable"
        raise UsageError(f"Usage: `provider {state} <name>`")
    name = ctx.args[1]
    existing = await ctx.db.get_provider(name)
    if existing is None:
        raise ValidationError(f"No provider named `{name}`.")
    await ctx.db.update_provider(name, enabled=enabled)
    manager.invalidate()
    verb = "enabled" if enabled else "disabled"
    await ctx.reply(f"✅ {verb.capitalize()} provider `{name}`.")
