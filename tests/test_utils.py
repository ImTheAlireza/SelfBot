"""Utility helpers: text formatting, durations and config loading."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from selfbot.config import ConfigError, load_config
from selfbot.plugins.timers import parse_duration
from selfbot.utils.files import cleanup_old_files, temp_workspace, unique_path
from selfbot.utils.text import (
    chunk_text,
    format_bytes,
    format_duration,
    format_duration_long,
    has_rtl,
    progress_bar,
    styled_clock,
    styled_number,
    truncate,
)

# ---------------------------------------------------------------------------
# Durations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90", 90),
        ("0", 0),
        ("15:30", 15 * 60 + 30),
        ("1:15:30", 3600 + 15 * 60 + 30),
        ("2:12:15:30", 2 * 86400 + 12 * 3600 + 15 * 60 + 30),
        ("1h30m", 5400),
        ("2d", 172800),
        ("45s", 45),
        ("1h2m3s", 3723),
    ],
)
def test_parse_duration_accepts_valid_formats(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "1:2:3:4:5", "-5", "1:x", "10 minutes"])
def test_parse_duration_rejects_garbage(text):
    assert parse_duration(text) is None


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (45, "45s"), (90, "1m 30s"), (3661, "1h 01m 01s")],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_format_duration_long_pluralises():
    assert format_duration_long(1) == "1 second"
    assert format_duration_long(2) == "2 seconds"
    assert format_duration_long(3661) == "1 hour, 1 minute, 1 second"


def test_format_duration_clamps_negatives():
    assert format_duration(-10) == "0s"
    assert format_duration_long(-10) == "0 seconds"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, "0 B"), (512, "512 B"), (1024, "1.0 KB"), (1048576, "1.0 MB"), (1073741824, "1.0 GB")],
)
def test_format_bytes(size, expected):
    assert format_bytes(size) == expected


def test_progress_bar_bounds():
    assert progress_bar(0, 10) == "░" * 10
    assert progress_bar(1, 10) == "█" * 10
    assert len(progress_bar(0.5, 10)) == 10
    # Out-of-range input is clamped rather than raising.
    assert progress_bar(-1, 5) == "░" * 5
    assert progress_bar(99, 5) == "█" * 5


def test_styled_number_and_clock():
    assert styled_number(7) == "𝟬𝟳"
    assert styled_number(123, pad=2) == "𝟭𝟮𝟯"
    assert " : " in styled_clock(3661)
    assert styled_clock(30).count(":") == 1  # MM : SS only


def test_truncate():
    assert truncate("short", 10) == "short"
    result = truncate("a" * 100, 10)
    assert len(result) == 10
    assert result.endswith("…")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hello", False),
        ("سلام", True),
        ("mixed سلام text", True),
        ("", False),
        ("123", False),
    ],
)
def test_has_rtl(text, expected):
    assert has_rtl(text) is expected


def test_chunk_text_respects_limit():
    text = "\n".join(f"line {i}" for i in range(500))
    chunks = chunk_text(text, 300)
    assert all(len(c) <= 300 for c in chunks)
    assert chunks


def test_chunk_text_rejects_bad_limit():
    with pytest.raises(ValueError):
        chunk_text("x", 0)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def test_temp_workspace_is_always_removed():
    with temp_workspace() as workspace:
        assert workspace.is_dir()
        (workspace / "scratch.txt").write_text("data")
        captured = workspace
    assert not captured.exists()


def test_temp_workspace_cleans_up_after_error():
    captured = None
    with pytest.raises(RuntimeError), temp_workspace() as workspace:
        captured = workspace
        raise RuntimeError("boom")
    assert captured is not None
    assert not captured.exists()


def test_unique_path_avoids_collisions(tmp_path: Path):
    target = tmp_path / "file.txt"
    assert unique_path(target) == target

    target.write_text("x")
    first = unique_path(target)
    assert first.name == "file_1.txt"

    first.write_text("y")
    assert unique_path(target).name == "file_2.txt"


def test_cleanup_old_files(tmp_path: Path):
    import time

    old = tmp_path / "old.tmp"
    new = tmp_path / "new.tmp"
    old.write_text("x")
    new.write_text("y")
    os.utime(old, (time.time() - 7200, time.time() - 7200))

    removed = cleanup_old_files(tmp_path, max_age_minutes=60)
    assert removed == 1
    assert not old.exists()
    assert new.exists()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_load_config_requires_credentials(monkeypatch, tmp_path):
    for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "SUDO_USER_ID"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ConfigError, match="Missing required configuration"):
        load_config(env_file=tmp_path / "absent.env")


def test_load_config_reads_env_file(monkeypatch, tmp_path):
    for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "SUDO_USER_ID"):
        monkeypatch.delenv(key, raising=False)

    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "TELEGRAM_API_ID=12345\n"
        'TELEGRAM_API_HASH="abcdef"\n'
        "export SUDO_USER_ID=999\n"
        f"DATA_DIR={tmp_path}\n"
    )

    config = load_config(env_file=env)
    assert config.telegram.api_id == 12345
    assert config.telegram.api_hash == "abcdef"
    assert config.sudo_user_id == 999


def test_config_describe_hides_secrets(config):
    import dataclasses

    config = dataclasses.replace(
        config, database_url="mysql+aiomysql://user:supersecret@host/db"
    )
    described = config.describe()
    assert "supersecret" not in described
    assert "***" in described


# ---------------------------------------------------------------------------
# `--login` bootstrap
#
# SUDO_USER_ID is required to run the bot, but a new user does not know their
# own ID yet. `--login` resolves that chicken-and-egg by discovering it.
# ---------------------------------------------------------------------------


def test_login_config_does_not_require_sudo_id(monkeypatch, tmp_path):
    for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "SUDO_USER_ID"):
        monkeypatch.delenv(key, raising=False)

    env = tmp_path / ".env"
    env.write_text("TELEGRAM_API_ID=1\nTELEGRAM_API_HASH=abc\nSUDO_USER_ID=\n")

    # Normal load must reject the missing owner ID...
    with pytest.raises(ConfigError, match="SUDO_USER_ID"):
        load_config(env_file=env)

    # ...but the login bootstrap must tolerate it.
    config = load_config(env_file=env, allow_missing_sudo=True)
    assert config.sudo_user_id == 0


def test_persist_sudo_id_fills_empty_value(tmp_path):
    from selfbot.__main__ import _persist_sudo_id

    env = tmp_path / ".env"
    env.write_text("TELEGRAM_API_ID=1\nSUDO_USER_ID=\n")
    assert _persist_sudo_id(env, 12345) is True
    assert "SUDO_USER_ID=12345" in env.read_text()


def test_persist_sudo_id_appends_when_absent(tmp_path):
    from selfbot.__main__ import _persist_sudo_id

    env = tmp_path / ".env"
    env.write_text("TELEGRAM_API_ID=1\n")
    assert _persist_sudo_id(env, 12345) is True
    assert "SUDO_USER_ID=12345" in env.read_text()


def test_persist_sudo_id_never_clobbers_a_different_id(tmp_path):
    """Someone else's ID in .env must survive; the user is told instead."""
    from selfbot.__main__ import _persist_sudo_id

    env = tmp_path / ".env"
    env.write_text("SUDO_USER_ID=111\n")
    assert _persist_sudo_id(env, 999) is False
    assert env.read_text() == "SUDO_USER_ID=111\n"


def test_persist_sudo_id_is_idempotent(tmp_path):
    from selfbot.__main__ import _persist_sudo_id

    env = tmp_path / ".env"
    env.write_text("SUDO_USER_ID=999\n")
    assert _persist_sudo_id(env, 999) is True
    assert env.read_text().count("SUDO_USER_ID") == 1


def test_persist_sudo_id_handles_missing_file(tmp_path):
    from selfbot.__main__ import _persist_sudo_id

    assert _persist_sudo_id(tmp_path / "nope" / ".env", 999) is False
