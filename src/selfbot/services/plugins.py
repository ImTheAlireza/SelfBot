"""External plugin discovery and lifecycle management.

Bundled commands live in :mod:`selfbot.plugins` and are auto-imported. This
manager adds *external* plugins — single ``.py`` files or packages dropped into
``DATA_DIR/plugins`` — that register commands the same way (via the global
``@command`` decorator) without requiring any change to the core project.

A plugin may optionally expose:

* ``PLUGIN = PluginMeta(...)`` — name/version/description metadata,
* ``async def setup(bot)`` — awaited once after import (spawn tasks here),
* ``async def teardown(bot)`` — awaited on unload/reload/shutdown.

Plugins execute with the same privileges as the bot (full account access), so
installing third-party code is explicitly gated behind
``plugin install -trust``.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["PluginInfo", "PluginManager", "PluginMeta"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PluginMeta:
    """Optional metadata a plugin module exposes as ``PLUGIN``."""

    name: str = ""
    version: str = ""
    description: str = ""
    author: str = ""


@dataclass(slots=True)
class PluginInfo:
    """A loaded (or failed) external plugin."""

    name: str
    source: Path
    module_name: str
    version: str = ""
    description: str = ""
    author: str = ""
    enabled: bool = True
    loaded: bool = False
    error: str | None = None
    commands: list[str] = field(default_factory=list)


class PluginManager:
    """Discovers, loads, unloads and reloads external plugin modules."""

    def __init__(self, bot: Any, plugins_dir: Path) -> None:
        self.bot = bot
        self.dir = Path(plugins_dir)
        self._plugins: dict[str, PluginInfo] = {}

    # -- discovery --------------------------------------------------------

    def discover(self) -> list[Path]:
        """Return candidate plugin files/dirs, sorted for stable ordering."""
        if not self.dir.is_dir():
            return []
        candidates: list[Path] = []
        for entry in sorted(self.dir.iterdir()):
            if entry.name.startswith(("_", ".")):
                continue
            if (entry.suffix == ".py" and entry.is_file()) or (entry.is_dir() and (entry / "__init__.py").is_file()):
                candidates.append(entry)
        return candidates

    # -- load / unload ----------------------------------------------------

    async def load_all(self) -> list[PluginInfo]:
        """Load every enabled plugin in the plugins directory."""
        self.dir.mkdir(parents=True, exist_ok=True)
        # Make the plugins dir importable so packages can import siblings.
        if str(self.dir) not in sys.path:
            sys.path.insert(0, str(self.dir))

        loaded: list[PluginInfo] = []
        for path in self.discover():
            info = await self._info_for(path)
            try:
                state = await self.bot.db.get_plugin_state(info.name)
            except Exception:
                state = None
            # DB state defaults to enabled when unknown.
            enabled = True if state is None else state.get("enabled", True)
            if not enabled:
                info.enabled = False
                self._plugins[info.name] = info
                continue
            await self._load(info)
            if info.loaded:
                loaded.append(info)
        return loaded

    async def load_path(self, path: Path) -> PluginInfo:
        """Load a single plugin by file path (used by ``plugin load``)."""
        path = Path(path)
        path = await asyncio.to_thread(lambda: path.expanduser().resolve())
        if not await asyncio.to_thread(path.exists):
            raise FileNotFoundError(path)
        # Copy into the managed directory when it lives elsewhere.
        if self.dir not in path.parents:
            import shutil

            await asyncio.to_thread(self.dir.mkdir, parents=True, exist_ok=True)
            target = self.dir / path.name

            def _copy() -> None:
                if path.is_dir():
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.copytree(path, target)
                else:
                    shutil.copy2(path, target)

            await asyncio.to_thread(_copy)
            path = target

        if str(self.dir) not in sys.path:
            sys.path.insert(0, str(self.dir))

        info = await self._info_for(path)
        await self._load(info)
        try:
            await self.bot.db.set_plugin_state(
                info.name, f"local:{path}", version=info.version, enabled=True
            )
        except Exception:
            logger.debug("Could not persist plugin state for %s", info.name)
        return info

    async def unload(self, name: str) -> PluginInfo:
        info = self._plugins.get(name)
        if info is None:
            raise KeyError(name)

        # Tear down the plugin if it asks for it.
        module = sys.modules.get(info.module_name)
        if module is not None:
            teardown = getattr(module, "teardown", None)
            if callable(teardown):
                try:
                    await teardown(self.bot)
                except Exception:
                    logger.exception("Plugin %s teardown failed", name)

        # Remove every command this module registered.
        registry = self.bot.registry
        for command_name in list(info.commands):
            registry.unregister(command_name)
        info.commands.clear()
        info.loaded = False

        sys.modules.pop(info.module_name, None)
        try:
            await self.bot.db.set_plugin_enabled(name, False)
        except Exception:
            pass
        return info

    async def reload(self, name: str) -> PluginInfo:
        info = self._plugins.get(name)
        if info is None:
            raise KeyError(name)
        await self.unload(name)
        # Re-discover the path (it may have changed on disk).
        path = info.source
        fresh = await self._info_for(path)
        await self._load(fresh)
        return fresh

    async def enable(self, name: str) -> PluginInfo:
        info = self._plugins.get(name)
        if info is None:
            raise KeyError(name)
        if not info.loaded:
            await self._load(info)
        await self.bot.db.set_plugin_enabled(name, True)
        info.enabled = True
        return info

    async def disable(self, name: str) -> PluginInfo:
        if name not in self._plugins:
            raise KeyError(name)
        await self.unload(name)
        info = self._plugins[name]
        info.enabled = False
        return info

    async def shutdown(self) -> None:
        for name in list(self._plugins):
            info = self._plugins[name]
            if info.loaded:
                try:
                    await self.unload(name)
                except Exception:
                    logger.debug("Error unloading plugin %s", name, exc_info=True)

    # -- introspection ----------------------------------------------------

    def list(self) -> list[PluginInfo]:
        return sorted(self._plugins.values(), key=lambda p: p.name)

    def get(self, name: str) -> PluginInfo | None:
        return self._plugins.get(name)

    # -- internals --------------------------------------------------------

    async def _info_for(self, path: Path) -> PluginInfo:
        # Single-file plugins load via an explicit spec; package directories
        # import by their directory name (the plugins dir is on sys.path).
        is_dir = await asyncio.to_thread(path.is_dir)
        module_name = path.name if is_dir else f"selfbot_ext_{path.stem}"
        return PluginInfo(name=path.stem, source=path, module_name=module_name)

    def _snapshot_commands(self) -> set[str]:
        return set(self.bot.registry._commands)  # type: ignore[attr-defined]

    async def _load(self, info: PluginInfo) -> None:
        before = self._snapshot_commands()
        module = await self._import_module(info)
        if module is None:
            return

        meta = getattr(module, "PLUGIN", None)
        if isinstance(meta, PluginMeta):
            if meta.name:
                info.name = meta.name
            info.version = meta.version
            info.description = meta.description
            info.author = meta.author

        after = self._snapshot_commands()
        info.commands = sorted(after - before)
        info.loaded = True
        info.error = None
        self._plugins[info.name] = info

        setup = getattr(module, "setup", None)
        if callable(setup):
            try:
                await setup(self.bot)
            except Exception:
                logger.exception("Plugin %s setup failed", info.name)
                info.error = "setup hook raised"

    async def _import_module(self, info: PluginInfo) -> Any:
        path = info.source
        try:
            if path.is_dir():
                module = importlib.import_module(info.module_name)
            else:
                spec = importlib.util.spec_from_file_location(
                    info.module_name, path
                )
                if spec is None or spec.loader is None:
                    raise ImportError(f"could not load spec for {path}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[info.module_name] = module
                spec.loader.exec_module(module)
            return module
        except Exception as exc:
            logger.warning("Could not load plugin %s: %s", path.name, exc)
            info.error = f"{type(exc).__name__}: {exc}"
            info.loaded = False
            self._plugins[info.name] = info
            sys.modules.pop(info.module_name, None)
            return None
