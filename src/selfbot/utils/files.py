"""Filesystem safety helpers.

Two real vulnerabilities in the original bot are fixed here:

* **Zip-slip** — ``zipf.extractall()`` was called on attacker-supplied archives,
  so an entry named ``../../.ssh/authorized_keys`` would escape the extraction
  directory. :func:`safe_extract` validates every member first.
* **Path traversal** — ``rename ../../etc/passwd`` built a destination path by
  concatenating user input. :func:`sanitize_filename` strips directory
  components and reserved names.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import unicodedata
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "cleanup_old_files",
    "guess_extension",
    "is_within",
    "safe_extract",
    "sanitize_filename",
    "temp_workspace",
    "unique_path",
]

# Characters illegal on Windows plus path separators and control codes.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MAX_NAME_LENGTH = 200


def sanitize_filename(name: str, *, default: str = "file", max_length: int = MAX_NAME_LENGTH) -> str:
    """Reduce arbitrary user input to a safe, single path component.

    >>> sanitize_filename("../../etc/passwd")
    'passwd'
    >>> sanitize_filename("")
    'file'
    >>> sanitize_filename("CON.txt")
    '_CON.txt'
    """
    name = unicodedata.normalize("NFC", name or "")
    # Take the last component: kills ../.. and absolute paths alike.
    name = name.replace("\\", "/").split("/")[-1]
    name = _ILLEGAL.sub("_", name).strip(" .")

    if not name:
        return default

    stem, dot, ext = name.rpartition(".")
    check = (stem if dot else name).upper()
    if check in _RESERVED_WINDOWS:
        name = f"_{name}"

    if len(name) > max_length:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            keep = max_length - len(ext) - 1
            name = f"{stem[:keep]}.{ext}"
        else:
            name = name[:max_length]

    return name or default


def is_within(base: Path, target: Path) -> bool:
    """True when ``target`` resolves inside ``base``."""
    try:
        base_resolved = base.resolve()
        target_resolved = target.resolve()
    except OSError:
        return False
    return base_resolved == target_resolved or base_resolved in target_resolved.parents


def safe_extract(
    archive: zipfile.ZipFile,
    destination: Path,
    *,
    max_total_bytes: int | None = None,
    max_files: int = 1000,
) -> list[Path]:
    """Extract a ZIP archive, rejecting unsafe members.

    Guards against zip-slip (``../`` escapes, absolute paths), symlink escapes,
    zip bombs (via ``max_total_bytes``) and file-count exhaustion.
    """
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    members = archive.infolist()
    if len(members) > max_files:
        raise ValueError(f"Archive contains too many entries ({len(members)} > {max_files})")

    declared_total = sum(m.file_size for m in members)
    if max_total_bytes is not None and declared_total > max_total_bytes:
        raise ValueError(
            f"Archive expands to {declared_total} bytes, over the "
            f"{max_total_bytes} byte limit"
        )

    extracted: list[Path] = []
    for member in members:
        name = member.filename
        if not name or name.endswith("/"):
            continue

        # Reject absolute paths, drive letters and parent traversal outright.
        normalized = name.replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[a-zA-Z]:", normalized):
            logger.warning("Skipping absolute path in archive: %s", name)
            continue
        if any(part == ".." for part in normalized.split("/")):
            logger.warning("Skipping traversal entry in archive: %s", name)
            continue

        target = (destination / normalized).resolve()
        if not is_within(destination, target):
            logger.warning("Skipping escaping entry in archive: %s", name)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, open(target, "wb") as sink:
            shutil.copyfileobj(source, sink)
        extracted.append(target)

    return extracted


def unique_path(path: Path) -> Path:
    """Return a path that does not exist yet, adding ``_1``, ``_2``… if needed."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for index in range(1, 1000):
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    return parent / f"{stem}_{os.getpid()}{suffix}"


@contextmanager
def temp_workspace(prefix: str = "selfbot-", parent: Path | None = None) -> Iterator[Path]:
    """A temporary directory that is always cleaned up.

    The original code scattered ``os.remove`` calls through ``finally`` blocks
    and leaked files whenever an early ``return`` fired first.
    """
    if parent is not None:
        # Be defensive: the data dir may not exist yet on a first run.
        parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def cleanup_old_files(directory: Path, max_age_minutes: int = 60) -> int:
    """Delete files older than ``max_age_minutes``. Returns the count removed."""
    if not directory.is_dir():
        return 0

    import time

    cutoff = time.time() - max_age_minutes * 60
    removed = 0
    for entry in directory.iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError as exc:
            logger.debug("Could not remove %s: %s", entry, exc)
    return removed


def guess_extension(filename: str | None, fallback: str = ".bin") -> str:
    """Extract a lowercase extension, or ``fallback``."""
    if not filename:
        return fallback
    suffix = Path(filename).suffix.lower()
    return suffix if suffix and len(suffix) <= 12 else fallback
