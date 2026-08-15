"""Command plugins.

Importing this package registers every command with the global registry.
Adding a feature means dropping a module in here that uses ``@command`` — no
central list to update, which is what made the old ``command_map`` drift out of
sync with the handlers it pointed at.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

logger = logging.getLogger(__name__)

__all__ = ["load_all"]

#: Modules loaded in this order so `help` categories read sensibly.
PRELUDE = (
    "core",
    "ai",
    "messaging",
    "files",
    "timers",
    "utilities",
    "reactions",
    "welcome",
    "stickers",
)


def load_all() -> list[str]:
    """Import every plugin module. Returns the names successfully loaded."""
    loaded: list[str] = []

    names = list(PRELUDE)
    for module in pkgutil.iter_modules(__path__):
        if not module.name.startswith("_") and module.name not in names:
            names.append(module.name)

    for name in names:
        try:
            importlib.import_module(f"{__name__}.{name}")
            loaded.append(name)
        except ImportError as exc:
            # A missing optional dependency disables one plugin, not the bot.
            logger.warning("Plugin %r unavailable: %s", name, exc)
        except Exception:
            logger.exception("Plugin %r failed to load", name)

    logger.info("Loaded %d plugin(s): %s", len(loaded), ", ".join(loaded))
    return loaded
