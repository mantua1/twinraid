"""Antwort- und Anfragemodelle der REST-API."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from twinraid.shared.base import StrictModel
from twinraid.shared.mode import RunMode


class DisplayNames(StrictModel):
    source: str
    target: str
    vm: str


class AppInfo(StrictModel):
    version: str
    mode: RunMode
    default_language: Literal["de", "en"]
    names: DisplayNames


class AuthState(StrictModel):
    """Öffentlich: enthält bewusst keine Werte aus der Konfiguration."""

    authenticated: bool
    mode: RunMode
    # None, wenn der Worker gerade nicht erreichbar ist.
    password_set: bool | None


class LoginRequest(StrictModel):
    # repr=False: Das Passwort darf nie in Logs oder Fehlermeldungen landen.
    password: Annotated[str, StringConstraints(min_length=1, max_length=1024)] = Field(repr=False)


class SessionInfo(StrictModel):
    csrf_token: str
    expires_at: datetime
    idle_timeout_seconds: int


class AuditEntryOut(StrictModel):
    at: datetime
    client_ip: str | None
    action: str
    result: str


class AuditList(StrictModel):
    entries: list[AuditEntryOut]


class TlsInfo(StrictModel):
    source: Literal["self-signed", "custom"]
    fingerprint_sha256: str
    not_after: datetime
    days_left: int
    expires_soon: bool
