"""Passwort mit Argon2id (Konzept 5.3).

Der Hash liegt in /data/secrets/password.argon2. Gesetzt wird er nur über die
Admin-CLI, geprüft nur im Worker. Passwörter werden nie geloggt.
"""

import logging
from enum import StrEnum
from functools import cached_property
from pathlib import Path

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from twinraid.config.validators import has_control_chars
from twinraid.shared.files import write_file_atomic

logger = logging.getLogger(__name__)

MIN_LENGTH = 12
MAX_LENGTH = 1024


class VerifyOutcome(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    NOT_SET = "not_set"


class PasswordPolicyError(ValueError):
    """Das Passwort erfüllt die Mindestanforderungen nicht."""


def default_hasher() -> PasswordHasher:
    """RFC 9106, zweite Empfehlung: 64 MiB, 3 Durchläufe, Parallelität 4."""
    return PasswordHasher(
        time_cost=3,
        memory_cost=64 * 1024,
        parallelism=4,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )


def check_policy(password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError(f"the password must have at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        raise PasswordPolicyError(f"the password must have at most {MAX_LENGTH} characters")
    if has_control_chars(password):
        raise PasswordPolicyError("the password must not contain control characters")


class PasswordStore:
    def __init__(self, path: Path, hasher: PasswordHasher | None = None) -> None:
        self._path = path
        self._hasher = hasher or default_hasher()

    @cached_property
    def _dummy_hash(self) -> str:
        # Gleich teure Prüfung, auch wenn kein Passwort gesetzt ist.
        return self._hasher.hash("twinraid-dummy-password-for-constant-timing")

    def is_set(self) -> bool:
        return self._path.is_file()

    def set(self, password: str) -> None:
        check_policy(password)
        encoded = self._hasher.hash(password)
        write_file_atomic(self._path, encoded.encode() + b"\n", 0o600)

    def verify(self, password: str) -> VerifyOutcome:
        """Zeitkonstanter Vergleich über argon2. Blockiert; im Worker in einem Thread aufrufen."""
        try:
            stored = self._path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            stored = ""
        except OSError, UnicodeDecodeError:
            logger.error("The password file cannot be read; set the password again.")
            stored = ""
        if not stored:
            self._check(self._dummy_hash, password)
            return VerifyOutcome.NOT_SET
        try:
            return VerifyOutcome.VALID if self._check(stored, password) else VerifyOutcome.INVALID
        except InvalidHashError:
            logger.error("The password file is damaged; set the password again.")
            self._check(self._dummy_hash, password)
            return VerifyOutcome.NOT_SET

    def _check(self, encoded: str, password: str) -> bool:
        try:
            return self._hasher.verify(encoded, password)
        except VerifyMismatchError:
            return False
        except VerificationError:
            return False
