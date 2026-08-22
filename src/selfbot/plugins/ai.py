"""AI commands: ``gpt``, provider management, model selection and status.

The HTTP/parsing logic lives in :mod:`selfbot.services.ai`; this module is the
thin command layer that turns Telegram invocations into manager calls. The
historical private names (``_extract_answer``, ``_format_rapidapi_error`` and
the provider constants) are re-exported so existing tests and any third-party
imports keep working unchanged.
"""

from __future__ import annotations

import logging
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
    """Ask an AI model, trying each configured provider until one answers."""
    prompt = ctx.raw_args.strip()
    if not prompt:
        raise UsageError(f"Usage: `{ctx.config.command_prefix}gpt <prompt>`")

    manager = get_manager(ctx)
    providers = await manager.providers(enabled_only=True)
    if not providers:
        raise FeatureDisabledError(
            "No AI provider is configured. Add one with "
            "`provider add <name> <base_url> <api_key> [model]`, or set "
            "ANYAPI_KEY / BLUESMINDS_API_KEY / RAPIDAPI_KEY in your `.env`."
        )

    status = await ctx.reply("🤖 Thinking…")
    try:
        answer = await manager.chat(prompt, history=False)
    finally:
        await _delete_status(status)

    await ctx.reply(answer)


# --------------------------------------------------------------------------
# aistatus
# --------------------------------------------------------------------------


@command("aistatus", category=CATEGORY, sudo_only=True, usage="aistatus [test <name>]")
async def cmd_aistatus(ctx: Context) -> None:
    """Show configured AI providers, their health and any active cooldown."""
    manager = get_manager(ctx)

    if ctx.args and ctx.args[0].lower() == "test":
        if len(ctx.args) < 2:
            raise UsageError("Usage: `aistatus test <provider>`")
        name = ctx.args[1]
        await ctx.reply(f"🔌 Testing `{name}`…")
        ok, detail = await manager.test_provider(name)
        icon = "✅" if ok else "❌"
        await ctx.reply(f"{icon} **{name}** — {detail}")
        return

    statuses = await manager.status()
    if not statuses:
        await ctx.reply("ℹ️ No AI providers configured. Use `provider add` to add one.")
        return

    default_name = None
    for s in statuses:
        if s.provider.is_default:
            default_name = s.provider.name

    lines = ["🧠 **AI providers**\n"]
    available = sum(1 for s in statuses if s.available)
    lines.append(
        f"{available}/{len(statuses)} available"
        + (f" · default: `{default_name}`" if default_name else "")
        + "\n"
    )
    for s in statuses:
        p = s.provider
        if not p.enabled:
            icon = "⏸"
        elif s.cooldown_remaining:
            icon = "❄️"
        elif p.last_error and not p.is_default:
            icon = "⚠️"
        else:
            icon = "✅"
        title = f"{icon} **{p.name}**"
        if p.is_default:
            title += " _(default)_"
        lines.append(title)
        model = p.model or "(provider default)"
        lines.append(f"  `{p.base_url}`")
        lines.append(
            f"  model: `{model}` · key: `{p.redacted_key}` · "
            f"{p.success_count} ok / {p.failure_count} fail"
        )
        if s.cooldown_remaining:
            lines.append(f"  ❄️ cooldown: {s.cooldown_remaining}s")
        if p.last_error:
            lines.append(f"  ⚠️ {truncate(p.last_error, 120)}")
        lines.append("")

    await ctx.reply("\n".join(lines).rstrip())


# --------------------------------------------------------------------------
# gptmodel
# --------------------------------------------------------------------------


@command(
    "gptmodel",
    category=CATEGORY,
    sudo_only=True,
    usage="gptmodel <list|set|clear|current> [model]",
    examples=("gptmodel list", "gptmodel set openai/gpt-4o-mini", "gptmodel current"),
)
async def cmd_gptmodel(ctx: Context) -> None:
    """List available models or choose the default model used by ``gpt``."""
    if not ctx.args:
        raise UsageError(
            "Usage: `gptmodel <list|set|clear|current> [model]`\n"
            "• `list` — discover models from enabled providers\n"
            "• `set <model>` — set the global default model\n"
            "• `set <provider> <model>` — set one provider's model\n"
            "• `clear` — remove the global override\n"
            "• `current` — show what the next `gpt` call uses"
        )

    manager = get_manager(ctx)
    action = ctx.args[0].lower()

    if action == "list":
        status = await ctx.reply("🔍 Discovering models…")
        models = await manager.list_models()
        await _delete_status(status)
        if not models:
            await ctx.reply(
                "ℹ️ No models discovered. Check `aistatus`; providers may be "
                "unreachable or you can set a model directly with "
                "`gptmodel set <model>`."
            )
            return
        current = await manager.current_model()
        lines = [f"🧠 **Models** ({len(models)}) — current: `{current}`\n"]
        for entry in models[:60]:
            marker = " ◂ current" if entry["current"] else ""
            providers = ", ".join(entry["providers"])
            lines.append(f"• `{entry['id']}` _({providers})_{marker}")
        if len(models) > 60:
            lines.append(f"\n_…and {len(models) - 60} more_")
        await ctx.reply("\n".join(lines))

    elif action == "set":
        if len(ctx.args) < 2:
            raise UsageError("Usage: `gptmodel set <model>` or `gptmodel set <provider> <model>`")

        # Two-argument form: gptmodel set <provider> <model>
        if len(ctx.args) >= 3 and "/" not in ctx.args[1]:
            provider_name = ctx.args[1]
            model = " ".join(ctx.args[2:]).strip()
            existing = await ctx.db.get_provider(provider_name)
            if existing is None:
                raise ValidationError(f"No provider named `{provider_name}`.")
            await manager.set_provider_model(provider_name, model)
            await ctx.reply(f"✅ Set `{provider_name}` model to `{model}`.")
            return

        model = " ".join(ctx.args[1:]).strip()
        if len(model) > 200:
            raise ValidationError("Model name is too long.")
        await manager.set_global_model(model)
        await ctx.reply(f"✅ Default model set to `{model}`.")

    elif action == "clear":
        await manager.clear_global_model()
        await ctx.reply("✅ Cleared the global model override.")

    elif action == "current":
        current = await manager.current_model()
        default = await ctx.db.get_default_provider()
        provider_name = default.name if default else "(config)"
        await ctx.reply(
            f"🧠 Next `gpt` call uses `{current}` via **{provider_name}**."
        )

    else:
        raise ValidationError(f"Unknown action `{action}`. Try list/set/clear/current.")


# --------------------------------------------------------------------------
# provider
# --------------------------------------------------------------------------


@command(
    "provider",
    category=CATEGORY,
    sudo_only=True,
    usage=(
        "provider <add|remove|list|default|enable|disable|test> "
        "[name] [base_url] [api_key] [model]"
    ),
    examples=(
        "provider add openai https://api.openai.com/v1 sk-xxx gpt-4o-mini",
        "provider list",
        "provider default openai",
        "provider test openai",
    ),
)
async def cmd_provider(ctx: Context) -> None:
    """Add, list and manage AI providers stored in the database."""
    if not ctx.args:
        prefix = ctx.config.command_prefix
        await ctx.reply(
            "🧠 **AI providers**\n\n"
            f"`{prefix}provider add <name> <base_url> <api_key> [model]`\n"
            f"`{prefix}provider list`\n"
            f"`{prefix}provider default <name>`\n"
            f"`{prefix}provider enable|disable <name>`\n"
            f"`{prefix}provider test <name>`\n"
            f"`{prefix}provider remove <name>`"
        )
        return

    manager = get_manager(ctx)
    action = ctx.args[0].lower()

    if action == "add":
        await _provider_add(ctx, manager)
    elif action in {"list", "ls"}:
        await _provider_list(ctx, manager)
    elif action == "remove":
        if len(ctx.args) < 2:
            raise UsageError("Usage: `provider remove <name>`")
        name = ctx.args[1]
        if await manager.remove_provider(name):
            await ctx.reply(f"✅ Removed provider `{name}`.")
        else:
            await ctx.reply(f"ℹ️ No provider named `{name}`.")
    elif action == "default":
        if len(ctx.args) < 2:
            raise UsageError("Usage: `provider default <name>`")
        name = ctx.args[1]
        try:
            await manager.set_default(name)
        except KeyError:
            raise ValidationError(f"No provider named `{name}`.") from None
        await ctx.reply(f"✅ `{name}` is now the default provider.")
    elif action == "enable":
        await _provider_toggle(ctx, manager, True)
    elif action == "disable":
        await _provider_toggle(ctx, manager, False)
    elif action == "test":
        if len(ctx.args) < 2:
            raise UsageError("Usage: `provider test <name>`")
        name = ctx.args[1]
        await ctx.reply(f"🔌 Testing `{name}`…")
        ok, detail = await manager.test_provider(name)
        icon = "✅" if ok else "❌"
        await ctx.reply(f"{icon} **{name}** — {detail}")
    else:
        raise ValidationError(
            f"Unknown action `{action}`. "
            "Try add/list/default/enable/disable/test/remove."
        )


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
