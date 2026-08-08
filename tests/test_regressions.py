"""Regression tests for the concrete bugs found in the original ``self.py``.

Each test names the defect it locks down so a future refactor cannot quietly
reintroduce it.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from conftest import SUDO_ID, FakeEvent
from selfbot.registry import Command, CommandRegistry, Context
from selfbot.utils.files import safe_extract, sanitize_filename
from selfbot.utils.text import chunk_text

# ---------------------------------------------------------------------------
# BUG 1 — several commands raised TypeError when given arguments.
#
# The old dispatcher always called `handler(event, *args)`, but
# `handle_user_help`, `handle_currency`, `handle_backup`, `handle_db_import`,
# `handle_ziplist_command` and `handle_qr_read_api` were `def f(event)`, while
# `handle_del` and `handle_book_download_by_md5` took exactly one argument.
# ---------------------------------------------------------------------------

ZERO_ARG_COMMANDS = ["help", "currency", "add", "qrread", "status", "ping"]


@pytest.mark.parametrize("name", ZERO_ARG_COMMANDS)
def test_zero_arg_commands_accept_extra_arguments(registry, name):
    """Passing stray arguments must never raise TypeError."""
    command = registry.get(name)
    assert command is not None, f"{name} should be registered"

    # Every handler takes exactly one parameter: the Context.
    import inspect

    params = list(inspect.signature(command.handler).parameters)
    assert params == ["ctx"], (
        f"{name} must accept a single Context, got {params}"
    )


@pytest.mark.asyncio
async def test_dispatch_with_extra_args_does_not_raise_typeerror(bot, registry):
    """`help me some extra words` used to crash; now it just works."""
    event = FakeEvent(raw_text="help totally unknown extra args")
    handled = await registry.dispatch(bot, event, event.raw_text)
    assert handled
    joined = " ".join(event.replies)
    assert "TypeError" not in joined
    assert "crashed" not in joined


@pytest.mark.asyncio
async def test_unknown_subject_reports_cleanly(bot, registry):
    event = FakeEvent(raw_text="help nosuchcommand")
    await registry.dispatch(bot, event, event.raw_text)
    assert any("No such command" in r for r in event.replies)


# ---------------------------------------------------------------------------
# BUG 2 — zip-slip in `unzip` (extractall on untrusted archives).
# ---------------------------------------------------------------------------


def test_safe_extract_blocks_path_traversal(tmp_path: Path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../escaped.txt", "pwned")
        zf.writestr("nested/../../also_escaped.txt", "pwned")
        zf.writestr("safe.txt", "fine")

    target = tmp_path / "out"
    with zipfile.ZipFile(archive) as zf:
        extracted = safe_extract(zf, target)

    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path / "also_escaped.txt").exists()
    assert [p.name for p in extracted] == ["safe.txt"]


def test_safe_extract_blocks_absolute_paths(tmp_path: Path):
    archive = tmp_path / "abs.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("/etc/passwd", "root")
        zf.writestr("ok.txt", "fine")

    with zipfile.ZipFile(archive) as zf:
        extracted = safe_extract(zf, tmp_path / "out")

    assert [p.name for p in extracted] == ["ok.txt"]


def test_safe_extract_rejects_zip_bombs(tmp_path: Path):
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.bin", b"\0" * 20_000)

    with zipfile.ZipFile(archive) as zf, pytest.raises(ValueError, match="expands to"):
        safe_extract(zf, tmp_path / "out", max_total_bytes=1000)


def test_safe_extract_rejects_too_many_files(tmp_path: Path):
    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for index in range(20):
            zf.writestr(f"file{index}.txt", "x")

    with zipfile.ZipFile(archive) as zf, pytest.raises(ValueError, match="too many entries"):
        safe_extract(zf, tmp_path / "out", max_files=5)


# ---------------------------------------------------------------------------
# BUG 3 — path traversal via `rename ../../etc/passwd`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("/absolute/path.txt", "path.txt"),
        ("..\\..\\windows\\system32", "system32"),
        ("normal.txt", "normal.txt"),
        ("", "file"),
        ("...", "file"),
        ("with/slash.png", "slash.png"),
        ("nul\x00byte.txt", "nul_byte.txt"),
    ],
)
def test_sanitize_filename_strips_traversal(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_guards_windows_reserved_names():
    assert sanitize_filename("CON.txt") == "_CON.txt"
    assert sanitize_filename("com1") == "_com1"


def test_sanitize_filename_truncates_but_keeps_extension():
    result = sanitize_filename("a" * 500 + ".txt", max_length=50)
    assert len(result) <= 50
    assert result.endswith(".txt")


# ---------------------------------------------------------------------------
# BUG 4 — `except aiohttp.ClientTimeout` is a TypeError at runtime, because
# ClientTimeout is a dataclass, not an exception.
# ---------------------------------------------------------------------------


def test_no_source_catches_non_exception_types():
    import selfbot

    root = Path(selfbot.__file__).parent
    offenders = [
        path
        for path in root.rglob("*.py")
        if "except aiohttp.ClientTimeout" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"ClientTimeout is not an exception: {offenders}"


# ---------------------------------------------------------------------------
# BUG 5 — blind 4096-character slicing split words and Markdown mid-entity.
# ---------------------------------------------------------------------------


def test_chunk_text_prefers_natural_boundaries():
    paragraph = ("word " * 300).strip()
    chunks = chunk_text(paragraph, 200)
    assert all(len(c) <= 200 for c in chunks)
    # No chunk should start or end mid-word.
    for chunk in chunks:
        assert not chunk.startswith("ord")
        assert chunk == chunk.strip()


def test_chunk_text_handles_unbreakable_input():
    chunks = chunk_text("x" * 1000, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == "x" * 1000


def test_chunk_text_roundtrips_short_input():
    assert chunk_text("hello", 100) == ["hello"]
    assert chunk_text("", 100) == []


# ---------------------------------------------------------------------------
# BUG 6 — sudo checks were bypassable and inconsistently applied.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sudo_only_command_rejects_stranger(bot, registry):
    event = FakeEvent(raw_text="adminlist", out=False, sender_id=999999)
    await registry.dispatch(bot, event, event.raw_text)
    assert any("owner" in r.lower() for r in event.replies)


@pytest.mark.asyncio
async def test_sudo_only_command_allows_owner(bot, registry):
    event = FakeEvent(raw_text="adminlist", out=True, sender_id=SUDO_ID)
    await registry.dispatch(bot, event, event.raw_text)
    assert not any("owner can" in r.lower() for r in event.replies)


# ---------------------------------------------------------------------------
# BUG 7 — duplicate command names silently shadowed each other.
# ---------------------------------------------------------------------------


def test_registry_rejects_duplicate_names():
    reg = CommandRegistry()

    async def handler(ctx: Context) -> None:  # pragma: no cover - never called
        ...

    reg.register(Command(name="dup", handler=handler))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(Command(name="dup", handler=handler))


def test_registry_rejects_duplicate_aliases():
    reg = CommandRegistry()

    async def handler(ctx: Context) -> None:  # pragma: no cover
        ...

    reg.register(Command(name="one", handler=handler, aliases=("shared",)))
    with pytest.raises(ValueError):
        reg.register(Command(name="two", handler=handler, aliases=("shared",)))


def test_no_duplicate_commands_in_real_registry(registry):
    seen: dict[str, str] = {}
    for cmd in registry.all():
        for name in cmd.names:
            assert name not in seen, f"{name} registered twice"
            seen[name] = cmd.name


# ---------------------------------------------------------------------------
# BUG 8 — help text advertised commands that did not exist.
# ---------------------------------------------------------------------------


def test_every_command_has_help_and_usage(registry):
    missing = [
        cmd.name
        for cmd in registry.all()
        if not cmd.help or not cmd.usage_text()
    ]
    assert not missing, f"Commands missing documentation: {missing}"


def test_documented_aliases_resolve(registry):
    for cmd in registry.all():
        for alias in cmd.aliases:
            assert registry.get(alias) is cmd
