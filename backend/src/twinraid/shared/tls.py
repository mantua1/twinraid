"""TLS-Zertifikat: erzeugen, prüfen, auswählen und für den Web-Prozess bereitstellen.

Konzept 5.2: selbstsigniert, ECDSA P-256, genau 825 Tage, kein CA-Zertifikat,
EKU serverAuth, SAN aus web.tls. Ein eigenes Zertifikat unter /config/tls hat
Vorrang. Das aktive Paar liegt für den Web-Prozess in /run/twinraid-tls.
"""

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Literal

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from twinraid.config.models import TlsConfig
from twinraid.shared.datadirs import DataPaths
from twinraid.shared.files import write_file_atomic

VALIDITY = timedelta(days=825)
# Kleiner Vorlauf, damit leicht falsch gehende Uhren das Zertifikat nicht ablehnen.
BACKDATE = timedelta(minutes=5)
EXPIRY_WARNING = timedelta(days=30)

CertificateSource = Literal["self-signed", "custom"]
IpAddress = IPv4Address | IPv6Address


class TlsError(Exception):
    """Das Zertifikat oder der Schlüssel ist unbrauchbar."""


@dataclass(frozen=True)
class CertificateInfo:
    fingerprint: str
    not_before: datetime
    not_after: datetime
    ip_addresses: frozenset[str]
    hostnames: frozenset[str]

    def days_left(self, now: datetime) -> int:
        return (self.not_after - now).days

    def expires_soon(self, now: datetime) -> bool:
        return self.not_after - now <= EXPIRY_WARNING


@dataclass(frozen=True)
class ActiveCertificate:
    info: CertificateInfo
    source: CertificateSource
    # Grund, warum neu erzeugt wurde; None, wenn das vorhandene weiterverwendet wird.
    generated_because: str | None


def fingerprint(certificate: x509.Certificate) -> str:
    """SHA-256-Fingerabdruck in der Schreibweise der Browser: AB:CD:…"""
    return certificate.fingerprint(hashes.SHA256()).hex(":").upper()


def certificate_info(certificate: x509.Certificate) -> CertificateInfo:
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        ips = frozenset(str(ip) for ip in san.get_values_for_type(x509.IPAddress))
        names = frozenset(name.lower() for name in san.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        ips, names = frozenset(), frozenset()
    return CertificateInfo(
        fingerprint=fingerprint(certificate),
        not_before=certificate.not_valid_before_utc,
        not_after=certificate.not_valid_after_utc,
        ip_addresses=ips,
        hostnames=names,
    )


def generate_self_signed(
    ip_addresses: Sequence[IpAddress], hostnames: Sequence[str], now: datetime
) -> tuple[bytes, bytes]:
    """Erzeugt Schlüssel und Zertifikat. Gibt (Zertifikat-PEM, Schlüssel-PEM) zurück."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TwinRaid")])
    not_before = now - BACKDATE
    alt_names: list[x509.GeneralName] = [x509.IPAddress(ip) for ip in ip_addresses]
    alt_names += [x509.DNSName(host) for host in hostnames]
    public_key = key.public_key()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_before + VALIDITY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def load_pair(cert_pem: bytes, key_pem: bytes) -> x509.Certificate:
    """Lädt Zertifikat (ggf. mit Kette) und Schlüssel und prüft, dass sie zusammenpassen."""
    try:
        chain = x509.load_pem_x509_certificates(cert_pem)
    except ValueError:
        raise TlsError("certificate file is not a valid PEM certificate") from None
    try:
        key = serialization.load_pem_private_key(key_pem, password=None)
    except TypeError:
        raise TlsError("private key is encrypted; provide an unencrypted key") from None
    except ValueError:
        raise TlsError("key file is not a valid PEM private key") from None
    leaf = chain[0]
    public_format = serialization.PublicFormat.SubjectPublicKeyInfo
    leaf_public = leaf.public_key().public_bytes(serialization.Encoding.DER, public_format)
    key_public = key.public_key().public_bytes(serialization.Encoding.DER, public_format)
    if leaf_public != key_public:
        raise TlsError("certificate and private key do not belong together")
    return leaf


def _renewal_reason(
    certificate: x509.Certificate, tls_config: TlsConfig, now: datetime
) -> str | None:
    info = certificate_info(certificate)
    if info.not_after <= now:
        return "the certificate has expired"
    wanted_ips = frozenset(str(ip) for ip in tls_config.ip_addresses)
    if info.ip_addresses != wanted_ips or info.hostnames != frozenset(tls_config.hostnames):
        return "the addresses in web.tls have changed"
    return None


def _read_optional(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def ensure_self_signed(
    paths: DataPaths, tls_config: TlsConfig, now: datetime, force_reason: str | None = None
) -> tuple[bytes, bytes, str | None]:
    """Verwendet das vorhandene selbstsignierte Zertifikat oder erzeugt ein neues.

    Gibt (Zertifikat, Schlüssel, Grund für Neuerzeugung oder None) zurück.
    """
    reason = force_reason
    cert_pem = _read_optional(paths.tls_cert)
    key_pem = _read_optional(paths.tls_key)
    if reason is None:
        if cert_pem is None or key_pem is None:
            reason = "no certificate exists yet"
        else:
            try:
                reason = _renewal_reason(load_pair(cert_pem, key_pem), tls_config, now)
            except TlsError as exc:
                reason = f"the stored certificate is unusable ({exc})"
    if reason is None and cert_pem is not None and key_pem is not None:
        return cert_pem, key_pem, None
    cert_pem, key_pem = generate_self_signed(tls_config.ip_addresses, tls_config.hostnames, now)
    write_file_atomic(paths.tls_key, key_pem, 0o600)
    write_file_atomic(paths.tls_cert, cert_pem, 0o600)
    return cert_pem, key_pem, reason


def load_custom(custom_dir: Path, now: datetime) -> tuple[bytes, bytes] | None:
    """Eigenes Zertifikat aus /config/tls; None, wenn keines vorhanden ist."""
    cert_path, key_path = custom_dir / "cert.pem", custom_dir / "key.pem"
    cert_exists, key_exists = cert_path.exists(), key_path.exists()
    if not cert_exists and not key_exists:
        return None
    if cert_exists != key_exists:
        raise TlsError(f"{custom_dir} must contain both cert.pem and key.pem")
    try:
        cert_pem, key_pem = cert_path.read_bytes(), key_path.read_bytes()
    except PermissionError:
        raise TlsError(f"cannot read the files in {custom_dir}") from None
    leaf = load_pair(cert_pem, key_pem)
    if leaf.not_valid_after_utc <= now:
        raise TlsError(f"the certificate in {custom_dir} has expired")
    return cert_pem, key_pem


def install_runtime(
    runtime_dir: Path, cert_pem: bytes, key_pem: bytes, source: CertificateSource, web_gid: int
) -> None:
    """Legt das aktive Paar für den Web-Prozess ab (nur im RAM unter /run)."""
    runtime_dir.mkdir(mode=0o750, exist_ok=True)
    runtime_dir.chmod(0o750)
    os.chown(runtime_dir, -1, web_gid)
    write_file_atomic(runtime_dir / "key.pem", key_pem, 0o640, web_gid)
    write_file_atomic(runtime_dir / "cert.pem", cert_pem, 0o644, web_gid)
    info = json.dumps({"source": source}).encode()
    write_file_atomic(runtime_dir / "source.json", info, 0o644, web_gid)


def read_runtime(runtime_dir: Path) -> tuple[x509.Certificate, CertificateSource]:
    """Aktives Zertifikat und seine Herkunft, wie sie der Web-Prozess verwendet."""
    try:
        chain = x509.load_pem_x509_certificates((runtime_dir / "cert.pem").read_bytes())
        raw_source = json.loads((runtime_dir / "source.json").read_text())["source"]
    except (OSError, ValueError, KeyError) as exc:
        raise TlsError(f"no active certificate in {runtime_dir}") from exc
    source: CertificateSource = "custom" if raw_source == "custom" else "self-signed"
    return chain[0], source


def activate(
    paths: DataPaths,
    tls_config: TlsConfig,
    custom_dir: Path,
    runtime_dir: Path,
    web_gid: int,
    now: datetime | None = None,
    force_new_self_signed: bool = False,
) -> ActiveCertificate:
    """Wählt das aktive Zertifikat und stellt es dem Web-Prozess bereit."""
    now = now or datetime.now(UTC)
    force = "renewal was requested" if force_new_self_signed else None
    self_cert, self_key, reason = ensure_self_signed(paths, tls_config, now, force)
    custom = load_custom(custom_dir, now)
    if custom is not None:
        cert_pem, key_pem = custom
        source: CertificateSource = "custom"
    else:
        cert_pem, key_pem = self_cert, self_key
        source = "self-signed"
    install_runtime(runtime_dir, cert_pem, key_pem, source, web_gid)
    leaf = x509.load_pem_x509_certificates(cert_pem)[0]
    return ActiveCertificate(
        info=certificate_info(leaf),
        source=source,
        generated_because=reason if source == "self-signed" else None,
    )


def uncovered_hosts(info: CertificateInfo, tls_config: TlsConfig) -> list[str]:
    """Adressen aus web.tls, für die das Zertifikat nicht gilt (z. B. eigenes Zertifikat)."""
    missing: list[str] = []
    for host in sorted(tls_config.allowed_hosts()):
        if host in info.ip_addresses or host in info.hostnames:
            continue
        parent = host.split(".", 1)[1] if "." in host else ""
        if parent and f"*.{parent}" in info.hostnames:
            continue
        missing.append(host)
    return missing
