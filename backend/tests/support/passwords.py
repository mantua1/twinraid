"""Schnelle Argon2-Parameter für Tests. Die echten Parameter prüft test_passwords.py."""

from pathlib import Path

from argon2 import PasswordHasher, Type

from twinraid.shared.passwords import PasswordStore
from twinraid.worker.password_check import PasswordCheck

TEST_PASSWORD = "richtiges-test-passwort-42"


def fast_hasher() -> PasswordHasher:
    return PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, type=Type.ID)


async def _no_sleep(_seconds: float) -> None:
    return None


def password_check(directory: Path, password: str | None = TEST_PASSWORD) -> PasswordCheck:
    """PasswordCheck ohne Wartezeiten; optional mit gesetztem Passwort."""
    store = PasswordStore(directory / "password.argon2", fast_hasher())
    if password is not None:
        store.set(password)
    return PasswordCheck(store, sleep=_no_sleep)
