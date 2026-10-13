"""TLS: eigene CA, Serverzertifikat, eigenes Zertifikat und Bereitstellung (Konzept 5.2).

Die CA ist auf die Adressen aus web.tls beschränkt (NameConstraints) und
signiert ein separates Serverzertifikat. Auf den Geräten wird nur die CA
installiert; das Serverzertifikat erneuert sich, ohne dass die Geräte etwas
merken. Ein eigenes Zertifikat unter /config/tls hat Vorrang. Das aktive Paar
liegt für den Web-Prozess in /run/twinraid-tls.
"""

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv6Address, ip_network
from pathlib import Path
from typing import Literal

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from twinraid.config.models import TlsConfig
from twinraid.shared.datadirs import DataPaths
from twinraid.shared.files import write_file_atomic

CA_VALIDITY = timedelta(days=3650)
SERVER_VALIDITY = timedelta(days=825)
# Das Serverzertifikat wird erneuert, wenn weniger als 60 Tage übrig sind.
SERVER_RENEW_BEFORE = timedelta(days=60)
# Warnungen im Dashboard: Serverzertifikat (dann ist die Erneuerung gescheitert) und CA.
SERVER_EXPIRY_WARNING = timedelta(days=30)
CA_EXPIRY_WARNING = timedelta(days=90)
# Kleiner Vorlauf, damit leicht falsch gehende Uhren das Zertifikat nicht ablehnen.
BACKDATE = timedelta(minutes=5)
# Reservierte Domain (RFC 6761). Ohne Hostnamen in web.tls erlaubt die CA nur
# DNS-Namen darunter, also keine echten. Ohne diesen Eintrag wäre nach
# RFC 5280 jeder DNS-Name erlaubt.
NO_DNS_NAMES = "invalid"

FIRST_CA_REASON = "no CA exists yet"
LEGACY_REASON = "the self-signed certificate of an earlier version was replaced"

CertificateSource = Literal["ca", "custom"]
IpAddress = IPv4Address | IPv6Address

_RUNTIME_CHAIN = "cert.pem"
_RUNTIME_KEY = "key.pem"
_RUNTIME_CA = "ca.pem"
_RUNTIME_INFO = "info.json"


class TlsError(Exception):
    """Ein Zertifikat oder ein Schlüssel ist unbrauchbar."""


@dataclass(frozen=True)
class CertificateInfo:
    fingerprint: str
    fingerprint_sha1: str
    name: str
    not_before: datetime
    not_after: datetime
    ip_addresses: frozenset[str]
    hostnames: frozenset[str]

    def days_left(self, now: datetime) -> int:
        return (self.not_after - now).days

    def expires_within(self, now: datetime, period: timedelta) -> bool:
        return self.not_after - now <= period


@dataclass(frozen=True)
class ActiveCertificate:
    """Ergebnis der Einrichtung: was ausgeliefert wird und was neu erzeugt wurde."""

    info: CertificateInfo
    source: CertificateSource
    ca: CertificateInfo
    # Gründe für eine Neuerzeugung; None, wenn das Vorhandene weiterverwendet wird.
    ca_generated_because: str | None
    server_generated_because: str | None
    legacy_removed: bool


@dataclass(frozen=True)
class RuntimeTls:
    """Was der Web-Prozess unter /run/twinraid-tls vorfindet."""

    leaf: x509.Certificate
    source: CertificateSource
    ca: x509.Certificate | None
    renewal_failed_at: datetime | None


def fingerprint(
    certificate: x509.Certificate, algorithm: hashes.HashAlgorithm | None = None
) -> str:
    """Fingerabdruck in der Schreibweise der Browser: AB:CD:… (Standard SHA-256)."""
    digest = certificate.fingerprint(algorithm or hashes.SHA256())
    return digest.hex(":").upper()


def der_fingerprint(der: bytes) -> str:
    """SHA-256-Fingerabdruck eines Zertifikats in DER-Form, z. B. vom Server ausgeliefert."""
    return fingerprint(x509.load_der_x509_certificate(der))


def certificate_info(certificate: x509.Certificate) -> CertificateInfo:
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        ips = frozenset(str(ip) for ip in san.get_values_for_type(x509.IPAddress))
        names = frozenset(name.lower() for name in san.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        ips, names = frozenset(), frozenset()
    common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    name = str(common_names[0].value) if common_names else ""
    return CertificateInfo(
        fingerprint=fingerprint(certificate),
        # SHA-1 nur zum Vergleich mit der Anzeige von Windows, nicht zur Prüfung.
        fingerprint_sha1=fingerprint(certificate, hashes.SHA1()),  # noqa: S303
        name=name,
        not_before=certificate.not_valid_before_utc,
        not_after=certificate.not_valid_after_utc,
        ip_addresses=ips,
        hostnames=names,
    )


def _new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _key_usage(*, digital_signature: bool = False, cert_sign: bool = False) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=digital_signature,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=cert_sign,
        crl_sign=cert_sign,
        encipher_only=False,
        decipher_only=False,
    )


def _alt_names(
    ip_addresses: Sequence[IpAddress], hostnames: Sequence[str]
) -> list[x509.GeneralName]:
    names: list[x509.GeneralName] = [x509.IPAddress(ip) for ip in ip_addresses]
    names += [x509.DNSName(host) for host in hostnames]
    return names


def name_constraints(tls_config: TlsConfig) -> x509.NameConstraints:
    """Erlaubt nur die eigenen Adressen: jede IP als Einzeladresse, die Hostnamen aus web.tls."""
    permitted: list[x509.GeneralName] = [
        x509.IPAddress(ip_network(f"{ip}/{ip.max_prefixlen}")) for ip in tls_config.ip_addresses
    ]
    dns = [x509.DNSName(host) for host in tls_config.hostnames] or [x509.DNSName(NO_DNS_NAMES)]
    return x509.NameConstraints(permitted_subtrees=[*permitted, *dns], excluded_subtrees=None)


def generate_ca(tls_config: TlsConfig, now: datetime) -> tuple[bytes, bytes]:
    """Erzeugt CA-Schlüssel und -Zertifikat. Gibt (Zertifikat-PEM, Schlüssel-PEM) zurück."""
    key = _new_key()
    public_key = key.public_key()
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, f"TwinRaid CA {now.date().isoformat()}")]
    )
    not_before = now - BACKDATE
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_before + CA_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_key_usage(cert_sign=True), critical=True)
        # Zusätzliche Begrenzung: Die CA taugt nur für Serverzertifikate.
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(name_constraints(tls_config), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM), _key_pem(key)


def generate_server(
    tls_config: TlsConfig,
    ca_cert: x509.Certificate,
    ca_key: ec.EllipticCurvePrivateKey,
    now: datetime,
) -> tuple[bytes, bytes]:
    """Erzeugt ein von der CA signiertes Serverzertifikat mit leerem Subject."""
    key = _new_key()
    public_key = key.public_key()
    not_before = now - BACKDATE
    not_after = min(not_before + SERVER_VALIDITY, ca_cert.not_valid_after_utc)
    alt_names = _alt_names(tls_config.ip_addresses, tls_config.hostnames)
    certificate = (
        x509.CertificateBuilder()
        # Leeres Subject: Kein Prüfprogramm kann einen Namen im Subject gegen die
        # Namensbeschränkung prüfen. Die SAN-Erweiterung ist dann kritisch (RFC 5280).
        .subject_name(x509.Name([]))
        .issuer_name(ca_cert.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_key_usage(digital_signature=True), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM), _key_pem(key)


def load_pair(cert_pem: bytes, key_pem: bytes) -> x509.Certificate:
    """Lädt Zertifikat (ggf. mit Kette) und Schlüssel und prüft, dass sie zusammenpassen."""
    return _load_pair(cert_pem, key_pem)[0]


def _load_pair(cert_pem: bytes, key_pem: bytes) -> tuple[x509.Certificate, PrivateKeyTypes]:
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
    return leaf, key


def _load_ca(
    cert_pem: bytes, key_pem: bytes
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    certificate, key = _load_pair(cert_pem, key_pem)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise TlsError("the CA key is not an ECDSA key")
    try:
        basic = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        raise TlsError("the CA certificate has no basic constraints") from None
    if not basic.ca:
        raise TlsError("the CA certificate is not a CA")
    return certificate, key


def _ca_renewal_reason(
    ca_cert: x509.Certificate, tls_config: TlsConfig, now: datetime
) -> str | None:
    if ca_cert.not_valid_after_utc <= now:
        return "the CA has expired"
    try:
        current = ca_cert.extensions.get_extension_for_class(x509.NameConstraints).value
    except x509.ExtensionNotFound:
        return "the CA has no name constraints"
    wanted = name_constraints(tls_config)
    if set(current.permitted_subtrees or ()) != set(wanted.permitted_subtrees or ()):
        return "the addresses in web.tls have changed"
    return None


def issued_by(certificate: x509.Certificate, ca_cert: x509.Certificate) -> bool:
    try:
        certificate.verify_directly_issued_by(ca_cert)
    except ValueError, TypeError, InvalidSignature:
        return False
    return True


def server_renewal_due(
    server_cert: x509.Certificate, ca_cert: x509.Certificate, now: datetime
) -> bool:
    """Weniger als 60 Tage übrig und ein neues Zertifikat würde länger gelten.

    Die zweite Bedingung verhindert tägliche Erneuerungen, wenn die CA selbst
    bald abläuft und das Serverzertifikat nicht über sie hinaus gelten kann.
    """
    not_after = server_cert.not_valid_after_utc
    return not_after - now < SERVER_RENEW_BEFORE and ca_cert.not_valid_after_utc > not_after


def _server_renewal_reason(
    server_cert: x509.Certificate, ca_cert: x509.Certificate, tls_config: TlsConfig, now: datetime
) -> str | None:
    if server_cert.not_valid_after_utc <= now:
        return "the server certificate has expired"
    if not issued_by(server_cert, ca_cert):
        return "the server certificate was not issued by the current CA"
    info = certificate_info(server_cert)
    wanted_ips = frozenset(str(ip) for ip in tls_config.ip_addresses)
    if info.ip_addresses != wanted_ips or info.hostnames != frozenset(tls_config.hostnames):
        return "the addresses in web.tls have changed"
    if server_renewal_due(server_cert, ca_cert, now):
        return f"the server certificate expires in {info.days_left(now)} days"
    return None


def _read_optional(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class _Material:
    ca_pem: bytes
    ca_cert: x509.Certificate
    server_pem: bytes
    server_key_pem: bytes
    server_cert: x509.Certificate
    ca_reason: str | None
    server_reason: str | None
    legacy_removed: bool


def _ensure_ca(
    paths: DataPaths, tls_config: TlsConfig, now: datetime, force_reason: str | None
) -> tuple[bytes, x509.Certificate, ec.EllipticCurvePrivateKey, str | None]:
    reason = force_reason
    cert_pem = _read_optional(paths.tls_ca_cert)
    key_pem = _read_optional(paths.tls_ca_key)
    if reason is None:
        if cert_pem is None or key_pem is None:
            reason = FIRST_CA_REASON
        else:
            try:
                ca_cert, ca_key = _load_ca(cert_pem, key_pem)
            except TlsError as exc:
                reason = f"the stored CA is unusable ({exc})"
            else:
                reason = _ca_renewal_reason(ca_cert, tls_config, now)
                if reason is None:
                    return cert_pem, ca_cert, ca_key, None
    cert_pem, key_pem = generate_ca(tls_config, now)
    write_file_atomic(paths.tls_ca_key, key_pem, 0o600)
    write_file_atomic(paths.tls_ca_cert, cert_pem, 0o600)
    ca_cert, ca_key = _load_ca(cert_pem, key_pem)
    return cert_pem, ca_cert, ca_key, reason


def _ensure_server(
    paths: DataPaths,
    tls_config: TlsConfig,
    ca_cert: x509.Certificate,
    ca_key: ec.EllipticCurvePrivateKey,
    now: datetime,
    force_reason: str | None,
) -> tuple[bytes, bytes, x509.Certificate, str | None]:
    reason = force_reason
    cert_pem = _read_optional(paths.tls_server_cert)
    key_pem = _read_optional(paths.tls_server_key)
    if reason is None:
        if cert_pem is None or key_pem is None:
            reason = "no server certificate exists yet"
        else:
            try:
                server_cert = load_pair(cert_pem, key_pem)
            except TlsError as exc:
                reason = f"the stored server certificate is unusable ({exc})"
            else:
                reason = _server_renewal_reason(server_cert, ca_cert, tls_config, now)
                if reason is None:
                    return cert_pem, key_pem, server_cert, None
    cert_pem, key_pem = generate_server(tls_config, ca_cert, ca_key, now)
    write_file_atomic(paths.tls_server_key, key_pem, 0o600)
    write_file_atomic(paths.tls_server_cert, cert_pem, 0o600)
    return cert_pem, key_pem, x509.load_pem_x509_certificate(cert_pem), reason


def _ensure_material(
    paths: DataPaths, tls_config: TlsConfig, now: datetime, renew_server: bool, new_ca: bool
) -> _Material:
    legacy = paths.legacy_tls_cert.exists() or paths.legacy_tls_key.exists()
    ca_pem, ca_cert, ca_key, ca_reason = _ensure_ca(
        paths, tls_config, now, "a new CA was requested" if new_ca else None
    )
    if ca_reason == FIRST_CA_REASON and legacy:
        ca_reason = LEGACY_REASON
    server_force = "renewal was requested" if renew_server and ca_reason is None else None
    server_pem, server_key_pem, server_cert, server_reason = _ensure_server(
        paths, tls_config, ca_cert, ca_key, now, server_force
    )
    if legacy:
        # Erst löschen, wenn CA und Serverzertifikat sicher geschrieben sind.
        paths.legacy_tls_cert.unlink(missing_ok=True)
        paths.legacy_tls_key.unlink(missing_ok=True)
    return _Material(
        ca_pem=ca_pem,
        ca_cert=ca_cert,
        server_pem=server_pem,
        server_key_pem=server_key_pem,
        server_cert=server_cert,
        ca_reason=ca_reason,
        server_reason=server_reason,
        legacy_removed=legacy,
    )


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


def _write_info(
    runtime_dir: Path, source: CertificateSource, renewal_failed_at: datetime | None, web_gid: int
) -> None:
    failed = renewal_failed_at.isoformat() if renewal_failed_at is not None else None
    info = json.dumps({"source": source, "renewal_failed_at": failed}).encode()
    write_file_atomic(runtime_dir / _RUNTIME_INFO, info, 0o644, web_gid)


def install_runtime(
    runtime_dir: Path,
    chain_pem: bytes,
    key_pem: bytes,
    ca_pem: bytes | None,
    source: CertificateSource,
    web_gid: int,
) -> None:
    """Legt das aktive Paar für den Web-Prozess ab (nur im RAM unter /run).

    Bei eigenem Zertifikat gibt es kein CA-Zertifikat zum Download.
    """
    runtime_dir.mkdir(mode=0o750, exist_ok=True)
    runtime_dir.chmod(0o750)
    os.chown(runtime_dir, -1, web_gid)
    write_file_atomic(runtime_dir / _RUNTIME_KEY, key_pem, 0o640, web_gid)
    write_file_atomic(runtime_dir / _RUNTIME_CHAIN, chain_pem, 0o644, web_gid)
    if ca_pem is None:
        (runtime_dir / _RUNTIME_CA).unlink(missing_ok=True)
    else:
        write_file_atomic(runtime_dir / _RUNTIME_CA, ca_pem, 0o644, web_gid)
    _write_info(runtime_dir, source, None, web_gid)
    # Ablage aus Phase 2, ersetzt durch info.json.
    (runtime_dir / "source.json").unlink(missing_ok=True)


def record_refresh_failure(runtime_dir: Path, now: datetime, web_gid: int) -> None:
    """Vermerkt eine gescheiterte automatische Erneuerung für das Dashboard."""
    source = read_runtime(runtime_dir).source
    _write_info(runtime_dir, source, now, web_gid)


def read_runtime(runtime_dir: Path) -> RuntimeTls:
    """Aktives Zertifikat, CA und Status, wie sie der Web-Prozess verwendet."""
    try:
        chain = x509.load_pem_x509_certificates((runtime_dir / _RUNTIME_CHAIN).read_bytes())
        info = json.loads((runtime_dir / _RUNTIME_INFO).read_text())
        raw_source = info["source"]
        raw_failed = info.get("renewal_failed_at")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise TlsError(f"no active certificate in {runtime_dir}") from exc
    source: CertificateSource = "custom" if raw_source == "custom" else "ca"
    ca: x509.Certificate | None = None
    ca_pem = _read_optional(runtime_dir / _RUNTIME_CA)
    if ca_pem is not None:
        try:
            ca = x509.load_pem_x509_certificate(ca_pem)
        except ValueError as exc:
            raise TlsError(f"the CA certificate in {runtime_dir} is unreadable") from exc
    failed_at: datetime | None = None
    if isinstance(raw_failed, str):
        try:
            failed_at = datetime.fromisoformat(raw_failed)
        except ValueError:
            failed_at = None
    return RuntimeTls(leaf=chain[0], source=source, ca=ca, renewal_failed_at=failed_at)


def runtime_ca_der(runtime_dir: Path) -> bytes | None:
    """CA-Zertifikat im DER-Format für den Download; None bei eigenem Zertifikat."""
    tls = read_runtime(runtime_dir)
    if tls.source != "ca" or tls.ca is None:
        return None
    return tls.ca.public_bytes(serialization.Encoding.DER)


def activate(
    paths: DataPaths,
    tls_config: TlsConfig,
    custom_dir: Path,
    runtime_dir: Path,
    web_gid: int,
    now: datetime | None = None,
    renew_server: bool = False,
    new_ca: bool = False,
) -> ActiveCertificate:
    """Pflegt CA und Serverzertifikat, wählt das aktive Zertifikat und stellt es bereit."""
    now = now or datetime.now(UTC)
    material = _ensure_material(paths, tls_config, now, renew_server, new_ca)
    custom = load_custom(custom_dir, now)
    if custom is not None:
        chain_pem, key_pem = custom
        install_runtime(runtime_dir, chain_pem, key_pem, None, "custom", web_gid)
        leaf = x509.load_pem_x509_certificates(chain_pem)[0]
        source: CertificateSource = "custom"
    else:
        chain_pem = material.server_pem + material.ca_pem
        install_runtime(
            runtime_dir, chain_pem, material.server_key_pem, material.ca_pem, "ca", web_gid
        )
        leaf = material.server_cert
        source = "ca"
    return ActiveCertificate(
        info=certificate_info(leaf),
        source=source,
        ca=certificate_info(material.ca_cert),
        ca_generated_because=material.ca_reason,
        server_generated_because=material.server_reason,
        legacy_removed=material.legacy_removed,
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
