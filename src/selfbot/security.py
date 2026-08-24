"""Symmetric encryption for secrets stored in the database.

API keys and other credentials live in the database rather than the
environment, but a database file is often backed up, copied or shared. This
module encrypts those values at rest using Fernet (AES-128-CBC + HMAC-SHA256)
backed by a single key file on disk.

The key file is generated on first use under ``DATA_DIR/secret.key`` with
``0600`` permissions. Operators must back it up alongside the database;
without it the encrypted columns are unrecoverable.

Values that were stored as plaintext (e.g. rows seeded from the environment
before this module existed) are decrypted transparently and re-encrypted on
the next write, so existing databases migrate without a manual step.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Any

__all__ = ["SecretBox", "SecretError", "is_encrypted_token"]

logger = logging.getLogger(__name__)

#: Prefix that marks a Fernet token so we can tell it apart from legacy
#: plaintext values. Fernet tokens themselves always start with ``gAAAAA``
#: but we use an explicit marker to avoid any ambiguity.
_MARKER = "enc::"


class SecretError(RuntimeError):
    """Raised when the secret key is missing, unreadable, or corrupt."""


def is_encrypted_token(value: str) -> bool:
    """True when ``value`` looks like one of our encrypted tokens."""
    return isinstance(value, str) and value.startswith(_MARKER)


class SecretBox:
    """Encrypts and decrypts short strings using a persistent key."""

    def __init__(self, key_path: Path | str) -> None:
        self._key_path = Path(key_path)
        self._fernet: Any = None
        self._invalid_token: Any = None

    def _ensure_key(self) -> Any:
        if self._fernet is not None:
            return self._fernet

        from cryptography.fernet import Fernet, InvalidToken

        self._invalid_token = InvalidToken

        path = self._key_path
        if path.is_file():
            key = path.read_bytes().strip()
            try:
                self._fernet = Fernet(key)
            except (ValueError, TypeError) as exc:
                raise SecretError(
                    f"The secret key at {path} is invalid. Restore the "
                    f"original key file or you will not be able to read "
                    f"previously encrypted values."
                ) from exc
            return self._fernet

        # Generate a new key. Restrict permissions before writing anything.
        path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        # Write via a temporary fd so we can chmod before any data lands.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(key)
        except Exception:
            # Best-effort cleanup if the write failed mid-way.
            try:
                path.unlink(missing_ok=True)
            finally:
                raise
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        logger.warning(
            "Generated a new secret key at %s — back this file up with the "
            "database; encrypted values cannot be recovered without it.",
            path,
        )
        self._fernet = Fernet(key)
        return self._fernet

    @property
    def key_path(self) -> Path:
        return self._key_path

    def encrypt(self, plaintext: str) -> str:
        """Encrypt ``plaintext`` and return a marked, URL-safe token."""
        if not isinstance(plaintext, str):
            raise TypeError("encrypt() expects a string")
        if not plaintext:
            return ""
        fernet = self._ensure_key()
        token = fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return f"{_MARKER}{token}"

    def decrypt(self, value: str) -> str:
        """Decrypt a token produced by :meth:`encrypt`.

        Plaintext values (legacy data) are returned unchanged.
        """
        if not value:
            return ""
        if not is_encrypted_token(value):
            return value
        fernet = self._ensure_key()
        token = value[len(_MARKER):].encode("ascii")
        try:
            return fernet.decrypt(token).decode("utf-8")
        except self._invalid_token as exc:
            raise SecretError(
                "Could not decrypt a secret — the key file does not match "
                "the one used to encrypt it."
            ) from exc

    def is_available(self) -> bool:
        """True when the cryptography package is importable."""
        try:
            import cryptography.fernet  # noqa: F401

            return True
        except ImportError:
            return False
