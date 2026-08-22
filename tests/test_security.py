"""Tests for the at-rest secret encryption helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from selfbot.security import SecretBox, SecretError, is_encrypted_token


def test_round_trip(tmp_path: Path) -> None:
    box = SecretBox(tmp_path / "secret.key")
    token = box.encrypt("sk-live-abcdef-123456")
    assert is_encrypted_token(token)
    assert token != "sk-live-abcdef-123456"
    assert box.decrypt(token) == "sk-live-abcdef-123456"


def test_empty_values_pass_through(tmp_path: Path) -> None:
    box = SecretBox(tmp_path / "secret.key")
    assert box.encrypt("") == ""
    assert box.decrypt("") == ""


def test_legacy_plaintext_is_returned_unchanged(tmp_path: Path) -> None:
    box = SecretBox(tmp_path / "secret.key")
    # A value without the marker is treated as legacy plaintext.
    assert box.decrypt("plaintext-key") == "plaintext-key"


def test_key_file_is_created_with_owner_only_perms(tmp_path: Path) -> None:
    key_path = tmp_path / "nested" / "secret.key"
    SecretBox(key_path).encrypt("anything")
    assert key_path.is_file()
    mode = key_path.stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_wrong_key_cannot_decrypt(tmp_path: Path) -> None:
    box1 = SecretBox(tmp_path / "k1.key")
    token = box1.encrypt("secret")
    box2 = SecretBox(tmp_path / "k2.key")
    box2.encrypt("other")  # force key generation
    with pytest.raises(SecretError):
        box2.decrypt(token)


def test_corrupt_key_file_errors(tmp_path: Path) -> None:
    key = tmp_path / "secret.key"
    key.write_bytes(b"not a valid fernet key")
    box = SecretBox(key)
    with pytest.raises(SecretError):
        box.encrypt("x")


def test_encryption_is_nondeterministic(tmp_path: Path) -> None:
    box = SecretBox(tmp_path / "secret.key")
    assert box.encrypt("same") != box.encrypt("same")
    assert box.decrypt(box.encrypt("same")) == "same"
