"""Typed, validated configuration loaded from the environment.

Core runtime settings are loaded here. Missing optional configuration degrades
a feature instead of crashing the bot.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

__all__ = ["Config", "load_config"]


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _env_int(key: str, default: int | None = None) -> int | None:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _env_float(key: str, default: float) -> float:
    raw = _env(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    api_id: int
    api_hash: str
    phone: str
    session_name: str

    @property
    def redacted(self) -> dict[str, str]:
        return {
            "api_id": str(self.api_id),
            "api_hash": f"{self.api_hash[:4]}…{self.api_hash[-2:]}",
            "phone": f"…{self.phone[-4:]}" if self.phone else "(interactive)",
            "session": self.session_name,
        }


@dataclass(frozen=True, slots=True)
class StickerConfig:
    bot_token: str
    bot_username: str
    watermark: str

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.bot_username)


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    config_path: str
    process_name: str
    log_file: str
    executable: str

    @property
    def enabled(self) -> bool:
        # Only the process name is essential. supervisorctl locates its own
        # config when -c is omitted, and the executable is auto-discovered.
        return bool(self.process_name)


@dataclass(frozen=True, slots=True)
class SpamConfig:
    delay: float
    limit: int
    cooldown: float


@dataclass(frozen=True, slots=True)
class Config:
    telegram: TelegramConfig
    sticker: StickerConfig
    supervisor: SupervisorConfig
    spam: SpamConfig

    sudo_user_id: int
    database_url: str
    data_dir: Path
    log_level: str
    log_channel_id: int | None
    log_channel_level: str
    command_prefix: str
    quick_reply_prefix: str
    startup_notify: str
    max_file_size_mb: int
    temp_ttl_minutes: int

    # Populated lazily so tests can point them somewhere temporary.
    _dirs_created: bool = field(default=False, compare=False)

    @property
    def session_path(self) -> Path:
        return self.data_dir / self.telegram.session_name

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    def ensure_dirs(self) -> None:
        """Create the data directories. Safe to call repeatedly."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

    def describe(self) -> str:
        """Human-readable, secret-free summary for startup logs."""
        lines = [
            f"session      : {self.session_path}",
            f"database     : {_redact_url(self.database_url)}",
            f"sudo user    : {self.sudo_user_id}",
            f"prefix       : {self.command_prefix or '(none)'}",
            f"stickers     : {'enabled' if self.sticker.enabled else 'disabled'}",
            f"supervisor   : {'enabled' if self.supervisor.enabled else 'disabled'}",
            f"log channel  : {self.log_channel_id or '(none)'}",
        ]
        return "\n".join(lines)


def _redact_url(url: str) -> str:
    """Strip credentials out of a database URL for safe logging."""
    if "://" not in url:
        return url
    scheme, _, rest = url.partition("://")
    if "@" not in rest:
        return url
    creds, _, host = rest.partition("@")
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def load_config(
    *,
    env_file: str | os.PathLike[str] | None = ".env",
    allow_missing_sudo: bool = False,
) -> Config:
    """Build a :class:`Config` from the environment.

    A ``.env`` file, when present, is loaded first but never overrides
    variables already set in the real environment.

    Set ``allow_missing_sudo`` for the ``--login`` flow, where the owner's user
    ID is not known yet and is discovered by signing in.
    """
    if env_file is not None:
        _load_dotenv(Path(env_file))

    api_id = _env_int("TELEGRAM_API_ID")
    api_hash = _env("TELEGRAM_API_HASH")
    sudo_user_id = _env_int("SUDO_USER_ID")

    required: tuple[tuple[str, object], ...] = (
        ("TELEGRAM_API_ID", api_id),
        ("TELEGRAM_API_HASH", api_hash),
        ("SUDO_USER_ID", sudo_user_id),
    )
    if allow_missing_sudo:
        # `--login` runs before the user knows their own ID, so it is
        # discovered rather than required. Placeholder is replaced after sign-in.
        required = required[:2]
        sudo_user_id = sudo_user_id or 0

    missing = [name for name, value in required if not value]
    if missing:
        hint = "Copy .env.example to .env and fill it in."
        if missing == ["SUDO_USER_ID"]:
            # The only value the user cannot look up themselves — offer the fix.
            hint = (
                "Run `python -m selfbot --login` to sign in; it will detect "
                "your user ID and write SUDO_USER_ID into .env for you."
            )
        raise ConfigError(
            "Missing required configuration: " + ", ".join(missing) + ".\n" + hint
        )

    data_dir = Path(_env("DATA_DIR", "./data")).expanduser().resolve()
    database_url = _env(
        "DATABASE_URL", f"sqlite+aiosqlite:///{data_dir / 'selfbot.db'}"
    )

    sticker = StickerConfig(
        bot_token=_env("STICKER_BOT_TOKEN"),
        bot_username=_env("STICKER_BOT_USERNAME").lstrip("@"),
        watermark=_env("STICKER_WATERMARK"),
    )

    supervisor = SupervisorConfig(
        config_path=_env("SUPERVISOR_CONFIG"),
        process_name=_env("SUPERVISOR_PROCESS", "selfbot"),
        log_file=_env("SUPERVISOR_LOG_FILE"),
        executable=_env("SUPERVISOR_CTL"),
    )

    spam = SpamConfig(
        delay=max(0.0, _env_float("SPAM_DELAY", 1.5)),
        limit=max(1, _env_int("SPAM_LIMIT", 50) or 50),
        cooldown=max(0.0, _env_float("SPAM_COOLDOWN", 4)),
    )

    return Config(
        telegram=TelegramConfig(
            api_id=api_id,  # type: ignore[arg-type]
            api_hash=api_hash,
            phone=_env("TELEGRAM_PHONE"),
            session_name=_env("TELEGRAM_SESSION", "selfbot"),
        ),
        sticker=sticker,
        supervisor=supervisor,
        spam=spam,
        sudo_user_id=sudo_user_id,  # type: ignore[arg-type]
        database_url=database_url,
        data_dir=data_dir,
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        log_channel_id=_env_int("LOG_CHANNEL_ID"),
        log_channel_level=_env("LOG_CHANNEL_LEVEL", "WARNING").upper(),
        command_prefix=_env("COMMAND_PREFIX"),
        quick_reply_prefix=_env("QUICK_REPLY_PREFIX", "-") or "-",
        startup_notify=_env("STARTUP_NOTIFY", "me") or "me",
        max_file_size_mb=_env_int("MAX_FILE_SIZE_MB", 512) or 512,
        temp_ttl_minutes=_env_int("TEMP_TTL_MINUTES", 60) or 60,
    )


def _load_dotenv(path: Path) -> None:
    """Minimal .env parser — no dependency, no surprises.

    Supports ``KEY=value``, ``export KEY=value``, ``#`` comments and quoted
    values. Existing environment variables always win.
    """
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        os.environ.setdefault(key, value)
