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
    # Modus real ohne freigegebene Aktionen: Banner „Nur Statusanzeige“ (Konzept 6.4).
    read_only: bool
    # False bei webgui.scheme http: Hinweis auf unverschlüsselte Verbindung (Konzept 3.5).
    target_connection_encrypted: bool


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


class CertificateDetails(StrictModel):
    fingerprint_sha256: str
    not_after: datetime
    days_left: int
    # Server: 30 Tage vor Ablauf (dann ist die Erneuerung gescheitert); CA: 90 Tage.
    expires_soon: bool


class CaDetails(CertificateDetails):
    name: str
    # SHA-1 nur zum Vergleich mit der Anzeige von Windows.
    fingerprint_sha1: str


class TlsAddressInfo(StrictModel):
    """Verwendete Adressen und ihre Herkunft (Konzept 5.1)."""

    ip_addresses: list[str]
    hostnames: list[str]
    origin: Literal["detected", "configured"]
    interface: str | None


class CaChangeInfo(StrictModel):
    """Unbestätigter Hinweis: Die CA wurde ersetzt (Konzept 5.2)."""

    changed_at: datetime
    reason: Literal["addresses_changed", "expired", "requested", "unusable", "legacy_replaced"]


class TlsInfo(StrictModel):
    source: Literal["ca", "custom"]
    server: CertificateDetails
    # None bei eigenem Zertifikat: dann gibt es keine CA zum Download.
    ca: CaDetails | None
    renewal_failed: bool
    addresses: TlsAddressInfo
    # None, wenn es keinen Wechsel gab oder der Hinweis bestätigt ist.
    ca_change: CaChangeInfo | None
