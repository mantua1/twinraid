"""Passwortablage mit Argon2id (Konzept 5.3)."""

import stat
from pathlib import Path

import pytest
from argon2 import Type

from tests.support.passwords import fast_hasher
from twinraid.shared.passwords import (
    PasswordPolicyError,
    PasswordStore,
    VerifyOutcome,
    check_policy,
    default_hasher,
)


def test_production_parameters_follow_rfc_9106() -> None:
    hasher = default_hasher()
    assert hasher.type is Type.ID
    assert hasher.memory_cost == 65536  # KiB = 64 MiB
    assert hasher.time_cost == 3
    assert hasher.parallelism == 4
    assert hasher.hash_len == 32
    assert hasher.salt_len == 16


def test_stored_hash_uses_production_parameters(tmp_path: Path) -> None:
    store = PasswordStore(tmp_path / "pw")
    store.set("ein-sehr-gutes-passwort")
    stored = (tmp_path / "pw").read_text()
    assert stored.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert "ein-sehr-gutes-passwort" not in stored


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("elf-zeichen", "at least 12"),
        ("", "at least 12"),
        ("x" * 1025, "at most 1024"),
        ("zwölf-zeichen\n", "control characters"),
        ("zwölf-zeichen\x00", "control characters"),
    ],
)
def test_policy_rejects(password: str, message: str) -> None:
    with pytest.raises(PasswordPolicyError, match=message):
        check_policy(password)


@pytest.mark.parametrize("password", ["genau-12-zch", "x" * 1024, "Läßt sich gut merken 🙂"])
def test_policy_accepts(password: str) -> None:
    check_policy(password)


def test_set_and_verify(tmp_path: Path) -> None:
    store = PasswordStore(tmp_path / "pw", fast_hasher())
    assert not store.is_set()
    assert store.verify("irgendein-passwort") is VerifyOutcome.NOT_SET
    store.set("richtiges-passwort")
    assert store.is_set()
    assert stat.S_IMODE((tmp_path / "pw").stat().st_mode) == 0o600
    assert store.verify("richtiges-passwort") is VerifyOutcome.VALID
    assert store.verify("falsches-passwort") is VerifyOutcome.INVALID
    assert store.verify("") is VerifyOutcome.INVALID


def test_set_rejects_weak_password(tmp_path: Path) -> None:
    store = PasswordStore(tmp_path / "pw", fast_hasher())
    with pytest.raises(PasswordPolicyError):
        store.set("kurz")
    assert not store.is_set()


def test_damaged_file_counts_as_not_set(tmp_path: Path) -> None:
    (tmp_path / "pw").write_text("kein argon2 hash")
    store = PasswordStore(tmp_path / "pw", fast_hasher())
    assert store.verify("irgendein-passwort") is VerifyOutcome.NOT_SET
