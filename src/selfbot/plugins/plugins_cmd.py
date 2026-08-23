"""Commands to manage external plugins."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from ..errors import UsageError, ValidationError
from ..registry import Context, command
from ..utils.text import truncate

logger = logging.getLogger(__name__)

CATEGORY = "System"


def _manager(ctx: Context) -> Any:
    manager = getattr(ctx.bot, "plugins", None)
    if manager is None:
        from ..services.plugins import PluginManager

        manager = PluginManager(ctx.bot, ctx.config.plugins_path)
        ctx.bot.plugins = manager
    return manager


@command(
    "plugin",
    category=CATEGORY,
    sudo_only=True,
    usage=(
        "plugin <list|load|reload|unload|enable|disable|install|path> "
        "[name|path|url]"
    ),
    examples=(
        "plugin list",
        "plugin load /tmp/myplugin.py",
        "plugin reload myplugin",
        "plugin install https://github.com/you/repo --trust",
    ),
)
async def cmd_plugin(ctx: Context) -> None:
    """List, load and manage external plugins from DATA_DIR/plugins."""
    if not ctx.args:
        prefix = ctx.config.command_prefix
        await ctx.reply(
            "🧩 **Plugins**\n\n"
            f"`{prefix}plugin list` — installed plugins\n"
            f"`{prefix}plugin load <path>` — add a .py file/package\n"
            f"`{prefix}plugin reload <name>` — re-import one\n"
            f"`{prefix}plugin enable|disable <name>`\n"
            f"`{prefix}plugin unload <name>` — remove its commands\n"
            f"`{prefix}plugin install <git-url|pypi-spec> --trust`\n"
            f"`{prefix}plugin path` — show the plugins directory"
        )
        return

    manager = _manager(ctx)
    action = ctx.args[0].lower()

    if action in {"list", "ls"}:
        plugins = manager.list()
        if not plugins:
            await ctx.reply(
                "ℹ️ No external plugins installed. Drop a `.py` file into "
                f"`{ctx.config.plugins_path}` or use `plugin load <path>`."
            )
            return
        lines = [f"🧩 **Plugins ({len(plugins)})**\n"]
        for p in plugins:
            icon = "✅" if p.loaded else ("⏸" if not p.enabled else "❌")
            meta = f" v{p.version}" if p.version else ""
            lines.append(f"{icon} **{p.name}**{meta} · `{len(p.commands)}` commands")
            if p.description:
                lines.append(f"   {truncate(p.description, 120)}")
            if p.error:
                lines.append(f"   ❌ {truncate(p.error, 200)}")
            for command_name in p.commands[:8]:
                lines.append(f"   • `{command_name}`")
        await ctx.reply("\n".join(lines))

    elif action == "load":
        if len(ctx.args) < 2:
            raise UsageError("Usage: `plugin load <path-to-.py-or-dir>`")
        target = Path(" ".join(ctx.args[1:]).strip())
        try:
            info = await manager.load_path(target)
        except FileNotFoundError:
            raise ValidationError(f"File not found: `{target}`") from None
        if info.error:
            await ctx.reply(f"⚠️ Loaded **{info.name}** with error: `{info.error}`")
        else:
            await ctx.reply(
                f"✅ Loaded **{info.name}** ({len(info.commands)} commands: "
                f"{', '.join(f'`{c}`' for c in info.commands) or 'none'})."
            )

    elif action == "reload":
        if len(ctx.args) < 2:
            raise UsageError("Usage: `plugin reload <name>`")
        name = ctx.args[1]
        try:
            info = await manager.reload(name)
        except KeyError:
            raise ValidationError(f"No plugin named `{name}`.") from None
        await ctx.reply(f"🔄 Reloaded **{info.name}** ({len(info.commands)} commands).")

    elif action in {"unload", "disable"}:
        if len(ctx.args) < 2:
            raise UsageError(f"Usage: `plugin {action} <name>`")
        name = ctx.args[1]
        try:
            await manager.unload(name)
        except KeyError:
            raise ValidationError(f"No plugin named `{name}`.") from None
        await ctx.reply(
            f"{'⏸ Disabled' if action == 'disable' else '📤 Unloaded'} **{name}**."
        )

    elif action == "enable":
        if len(ctx.args) < 2:
            raise UsageError("Usage: `plugin enable <name>`")
        name = ctx.args[1]
        try:
            info = await manager.enable(name)
        except KeyError:
            raise ValidationError(f"No plugin named `{name}`.") from None
        await ctx.reply(f"✅ Enabled **{info.name}**.")

    elif action == "path":
        await ctx.reply(f"📁 Plugins directory: `{ctx.config.plugins_path}`")

    elif action == "install":
        await _install(ctx, manager)

    else:
        raise ValidationError(
            f"Unknown action `{action}`. "
            "Try list/load/reload/unload/enable/disable/install/path."
        )


async def _install(ctx: Context, manager: Any) -> None:
    args = ctx.args[1:]
    if not args:
        raise UsageError(
            "Usage: `plugin install <git-url|pypi-spec> --trust`"
        )
    if "-trust" not in args and "--trust" not in args:
        raise ValidationError(
            "⚠️ External plugins run with **full access to your Telegram "
            "account and this server**. Re-run with `-trust` once you have "
            "reviewed the code."
        )
    args = [a for a in args if a not in {"-trust", "--trust"}]
    spec = " ".join(args).strip()
    if not spec:
        raise UsageError("Nothing to install.")

    await ctx.reply(f"📥 Installing `{truncate(spec, 120)}`…")

    plugins_dir = Path(ctx.config.plugins_path)
    await asyncio.to_thread(plugins_dir.mkdir, parents=True, exist_ok=True)

    if spec.startswith(("http://", "https://", "git@", "git://")):
        target = plugins_dir / _repo_dirname(spec)
        if await asyncio.to_thread(target.exists):
            raise ValidationError(f"`{target}` already exists.")
        cmd = ("git", "clone", "--depth", "1", spec, str(target))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            raise ValidationError("Installation timed out after 120s.") from None
        if proc.returncode != 0:
            raise ValidationError(
                f"git clone failed: {stderr.decode(errors='replace')[:300]}"
            )
        info = await manager.load_path(target)
        await ctx.reply(f"✅ Installed **{info.name}** from git.")
        return

    # Treat anything else as a pip-installable spec.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "--", spec,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        raise ValidationError("pip install timed out after 180s.") from None
    if proc.returncode != 0:
        raise ValidationError(
            f"pip install failed: {stderr.decode(errors='replace')[:300]}"
        )
    await ctx.reply(
        f"✅ Installed `{spec}` via pip. If it ships commands, add a loader "
        f"file to `{plugins_dir}` or use `plugin load <path>`."
    )


def _repo_dirname(url: str) -> str:
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return "".join(c for c in name if c.isalnum() or c in "-_") or "plugin"
