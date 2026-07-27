"""Exception hierarchy for the self-bot.

Handlers raise :class:`CommandError` (or a subclass) to report a problem to the
user. The dispatcher turns those into a friendly reply. Anything else is a real
bug and gets logged with a traceback.
"""

from __future__ import annotations

__all__ = [
    "CommandError",
    "ConfigError",
    "FeatureDisabledError",
    "PermissionDeniedError",
    "ProviderError",
    "SelfBotError",
    "UsageError",
    "ValidationError",
]


class SelfBotError(Exception):
    """Base class for every error raised by this project."""


class ConfigError(SelfBotError):
    """Configuration is missing or invalid. Fatal at startup."""


class CommandError(SelfBotError):
    """A command failed in a way worth telling the user about.

    The message is sent back verbatim, so keep it short and actionable.
    """

    emoji = "❌"

    def user_message(self) -> str:
        return f"{self.emoji} {self}"


class UsageError(CommandError):
    """The user called a command incorrectly."""

    emoji = "💡"


class PermissionDeniedError(CommandError):
    """The caller is not allowed to run this command."""

    emoji = "🔒"


class FeatureDisabledError(CommandError):
    """The feature needs configuration that is not present."""

    emoji = "⚙️"


class ProviderError(CommandError):
    """An upstream API failed."""

    emoji = "🌐"


class ValidationError(CommandError):
    """User-supplied input failed validation."""

    emoji = "⚠️"
