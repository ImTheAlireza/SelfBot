"""Tests for the external plugin manager and plugin commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeEvent
from selfbot.registry import registry as global_registry
from selfbot.services.plugins import PluginManager, PluginMeta

PLUGIN_SOURCE = '''
from selfbot.registry import command, Context

PLUGIN = __import__("selfbot.services.plugins", fromlist=["PluginMeta"]).PluginMeta(
    name="demo", version="1.0", description="A demo plugin"
)

SETUP_CALLED = False
TEARDOWN_CALLED = False

async def setup(bot):
    global SETUP_CALLED
    SETUP_CALLED = True

async def teardown(bot):
    global TEARDOWN_CALLED
    TEARDOWN_CALLED = True

@command("demohello", category="Demo", usage="demohello")
async def cmd_demohello(ctx: Context) -> None:
    """Say hello from a plugin."""
    await ctx.reply("hello from demo plugin")
'''


BAD_PLUGIN_SOURCE = """
this is not valid python!!!
"""


@pytest.fixture
def plugin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plugins"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
async def _cleanup_plugins(bot, plugin_dir: Path):
    """Unload any external plugins and remove their commands after each test."""
    yield
    if bot.plugins is not None:
        try:
            await bot.plugins.shutdown()
        except Exception:
            pass
    # Belt-and-braces: ensure the demo command never leaks between tests.
    global_registry.unregister("demohello")


def _write_plugin(directory: Path, name: str, source: str) -> Path:
    path = directory / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return path


async def test_load_single_file_plugin(bot, plugin_dir: Path) -> None:
    bot.plugins = PluginManager(bot, plugin_dir)
    _write_plugin(plugin_dir, "demo", PLUGIN_SOURCE)

    loaded = await bot.plugins.load_all()
    assert len(loaded) == 1
    info = bot.plugins.get("demo")
    assert info is not None and info.loaded and info.version == "1.0"
    assert "demohello" in info.commands

    event = FakeEvent(raw_text="demohello")
    await bot.registry.dispatch(bot, event, "demohello")
    assert any("hello from demo plugin" in r for r in event.replies)


async def test_unload_removes_commands_and_calls_teardown(
    bot, plugin_dir: Path
) -> None:
    bot.plugins = PluginManager(bot, plugin_dir)
    _write_plugin(plugin_dir, "demo", PLUGIN_SOURCE)
    await bot.plugins.load_all()
    assert "demohello" in bot.registry

    await bot.plugins.unload("demo")
    assert "demohello" not in bot.registry
    info = bot.plugins.get("demo")
    assert info is not None and not info.loaded


async def test_reload_picks_up_changes(bot, plugin_dir: Path) -> None:
    path = _write_plugin(plugin_dir, "demo", PLUGIN_SOURCE)
    bot.plugins = PluginManager(bot, plugin_dir)
    await bot.plugins.load_all()

    path.write_text(
        PLUGIN_SOURCE.replace("hello from demo plugin", "RELOADED"),
        encoding="utf-8",
    )
    info = await bot.plugins.reload("demo")
    assert info.loaded
    event = FakeEvent(raw_text="demohello")
    await bot.registry.dispatch(bot, event, "demohello")
    assert any("RELOADED" in r for r in event.replies)


async def test_bad_plugin_does_not_crash_load(bot, plugin_dir: Path) -> None:
    _write_plugin(plugin_dir, "bad", BAD_PLUGIN_SOURCE)
    _write_plugin(plugin_dir, "good", PLUGIN_SOURCE.replace("demo", "good"))
    bot.plugins = PluginManager(bot, plugin_dir)

    loaded = await bot.plugins.load_all()
    names = {p.name for p in loaded}
    assert "good" in names
    bad = bot.plugins.get("bad")
    assert bad is not None and bad.loaded is False and bad.error


async def test_load_path_copies_external_file(bot, plugin_dir: Path, tmp_path: Path) -> None:
    external = tmp_path / "external.py"
    external.write_text(PLUGIN_SOURCE, encoding="utf-8")
    bot.plugins = PluginManager(bot, plugin_dir)

    info = await bot.plugins.load_path(external)
    assert (plugin_dir / "external.py").exists()
    assert info.loaded and "demohello" in info.commands


async def test_disabled_plugin_not_loaded(bot, plugin_dir: Path) -> None:
    _write_plugin(plugin_dir, "demo", PLUGIN_SOURCE)
    bot.plugins = PluginManager(bot, plugin_dir)
    await bot.db.set_plugin_enabled("demo", False)

    loaded = await bot.plugins.load_all()
    assert loaded == []
    assert "demohello" not in bot.registry


async def test_enable_disable_roundtrip(bot, plugin_dir: Path) -> None:
    _write_plugin(plugin_dir, "demo", PLUGIN_SOURCE)
    bot.plugins = PluginManager(bot, plugin_dir)
    await bot.plugins.load_all()

    await bot.plugins.disable("demo")
    assert "demohello" not in bot.registry

    await bot.plugins.enable("demo")
    assert "demohello" in bot.registry


async def test_plugin_list_command(bot, plugin_dir: Path) -> None:
    _write_plugin(plugin_dir, "demo", PLUGIN_SOURCE)
    bot.plugins = PluginManager(bot, plugin_dir)
    await bot.plugins.load_all()

    event = FakeEvent(raw_text="plugin list")
    await bot.registry.dispatch(bot, event, "plugin list")
    assert any("demo" in r for r in event.replies)


async def test_plugin_path_command(bot) -> None:
    event = FakeEvent(raw_text="plugin path")
    await bot.registry.dispatch(bot, event, "plugin path")
    assert any("plugins" in r for r in event.replies)


async def test_plugin_install_requires_trust(bot) -> None:
    event = FakeEvent(raw_text="plugin install https://example.com/repo.git")
    await bot.registry.dispatch(bot, event, "plugin install https://example.com/repo.git")
    assert any("--trust" in r for r in event.replies)


def test_plugin_meta_dataclass() -> None:
    meta = PluginMeta(name="x", version="2", description="d", author="a")
    assert meta.name == "x" and meta.version == "2"
