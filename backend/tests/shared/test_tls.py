"""TLS-Zertifikat: Eigenschaften, Beständigkeit, eigenes Zertifikat (Konzept 5.2)."""

import os
import stat
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from twinraid.config.models import TlsConfig
from twinraid.shared.datadirs import DataPaths, prepare_data_dirs
from twinraid.shared.tls import (
    ActiveCertificate,
    TlsError,
    activate,
    certificate_info,
    generate_self_signed,
    read_runtime,
    uncovered_hosts,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
GID = os.getgid()
TLS = TlsConfig(ip_addresses=[ip_address("192.168.10.20")], hostnames=["twinraid.home.arpa"])


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    root = tmp_path / "data"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    data = DataPaths(root)
    prepare_data_dirs(data, os.getuid(), GID)
    return data


def _activate(
    paths: DataPaths,
    tmp_path: Path,
    tls: TlsConfig = TLS,
    now: datetime = NOW,
    force: bool = False,
) -> ActiveCertificate:
    return activate(paths, tls, tmp_path / "custom", tmp_path / "run-tls", GID, now, force)


def test_certificate_properties() -> None:
    cert_pem, key_pem = generate_self_signed(TLS.ip_addresses, TLS.hostnames, NOW)
    cert = x509.load_pem_x509_certificate(cert_pem)
    # Selbstsigniert und gültig signiert
    assert cert.issuer == cert.subject
    public_key = cert.public_key()
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    assert public_key.curve.name == "secp256r1"
    public_key.verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(hashes.SHA256()))
    assert isinstance(cert.signature_hash_algorithm, hashes.SHA256)
    # Höchstens 825 Tage
    assert cert.not_valid_after_utc - cert.not_valid_before_utc == timedelta(days=825)
    assert cert.not_valid_before_utc <= NOW
    # Kein CA-Zertifikat
    basic = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic.critical
    assert basic.value.ca is False
    usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    assert usage.digital_signature
    assert not usage.key_cert_sign
    assert not usage.crl_sign
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]
    info = certificate_info(cert)
    assert info.ip_addresses == {"192.168.10.20"}
    assert info.hostnames == {"twinraid.home.arpa"}
    # Schlüssel ist unverschlüsselt im PKCS#8-Format
    key = serialization.load_pem_private_key(key_pem, password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)


def test_fingerprint_format() -> None:
    cert_pem, _ = generate_self_signed(TLS.ip_addresses, [], NOW)
    fingerprint = certificate_info(x509.load_pem_x509_certificate(cert_pem)).fingerprint
    parts = fingerprint.split(":")
    assert len(parts) == 32
    assert all(len(p) == 2 and p == p.upper() for p in parts)


def test_first_start_generates_and_restart_keeps(paths: DataPaths, tmp_path: Path) -> None:
    first = _activate(paths, tmp_path)
    assert first.source == "self-signed"
    assert first.generated_because == "no certificate exists yet"
    second = _activate(paths, tmp_path, now=NOW + timedelta(days=100))
    assert second.generated_because is None
    assert second.info.fingerprint == first.info.fingerprint


def test_file_modes(paths: DataPaths, tmp_path: Path) -> None:
    _activate(paths, tmp_path)
    assert stat.S_IMODE(paths.tls_key.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.tls_cert.stat().st_mode) == 0o600
    runtime = tmp_path / "run-tls"
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o750
    assert stat.S_IMODE((runtime / "key.pem").stat().st_mode) == 0o640
    assert stat.S_IMODE((runtime / "cert.pem").stat().st_mode) == 0o644
    assert (runtime / "key.pem").stat().st_gid == GID


def test_expired_certificate_is_renewed(paths: DataPaths, tmp_path: Path) -> None:
    first = _activate(paths, tmp_path)
    later = _activate(paths, tmp_path, now=NOW + timedelta(days=826))
    assert later.generated_because == "the certificate has expired"
    assert later.info.fingerprint != first.info.fingerprint


def test_changed_addresses_renew(paths: DataPaths, tmp_path: Path) -> None:
    first = _activate(paths, tmp_path)
    changed = TlsConfig(ip_addresses=[ip_address("192.168.10.21")], hostnames=[])
    later = _activate(paths, tmp_path, tls=changed)
    assert later.generated_because == "the addresses in web.tls have changed"
    assert later.info.ip_addresses == {"192.168.10.21"}
    assert later.info.fingerprint != first.info.fingerprint


def test_forced_renewal(paths: DataPaths, tmp_path: Path) -> None:
    first = _activate(paths, tmp_path)
    renewed = _activate(paths, tmp_path, force=True)
    assert renewed.generated_because == "renewal was requested"
    assert renewed.info.fingerprint != first.info.fingerprint


def test_damaged_certificate_is_replaced(paths: DataPaths, tmp_path: Path) -> None:
    _activate(paths, tmp_path)
    paths.tls_cert.write_bytes(b"kaputt")
    later = _activate(paths, tmp_path)
    assert later.generated_because is not None
    assert "unusable" in later.generated_because


def _write_custom(tmp_path: Path, cert_pem: bytes, key_pem: bytes) -> None:
    custom = tmp_path / "custom"
    custom.mkdir(exist_ok=True)
    (custom / "cert.pem").write_bytes(cert_pem)
    (custom / "key.pem").write_bytes(key_pem)


def test_custom_certificate_has_priority(paths: DataPaths, tmp_path: Path) -> None:
    cert_pem, key_pem = generate_self_signed([ip_address("192.168.10.99")], [], NOW)
    _write_custom(tmp_path, cert_pem, key_pem)
    active = _activate(paths, tmp_path)
    assert active.source == "custom"
    assert active.info.ip_addresses == {"192.168.10.99"}
    runtime_cert, source = read_runtime(tmp_path / "run-tls")
    assert source == "custom"
    assert certificate_info(runtime_cert).fingerprint == active.info.fingerprint
    # Das selbstsignierte bleibt als Rückfall erhalten.
    assert paths.tls_cert.exists()
    assert uncovered_hosts(active.info, TLS) == ["192.168.10.20", "twinraid.home.arpa"]


def test_custom_wildcard_covers_hostname() -> None:
    cert_pem, _ = generate_self_signed([ip_address("192.168.10.20")], ["*.home.arpa"], NOW)
    info = certificate_info(x509.load_pem_x509_certificate(cert_pem))
    assert uncovered_hosts(info, TLS) == []


def test_custom_mismatched_key_is_rejected(paths: DataPaths, tmp_path: Path) -> None:
    cert_pem, _ = generate_self_signed(TLS.ip_addresses, [], NOW)
    _, other_key = generate_self_signed(TLS.ip_addresses, [], NOW)
    _write_custom(tmp_path, cert_pem, other_key)
    with pytest.raises(TlsError, match="do not belong together"):
        _activate(paths, tmp_path)


def test_custom_expired_is_rejected(paths: DataPaths, tmp_path: Path) -> None:
    cert_pem, key_pem = generate_self_signed(TLS.ip_addresses, [], NOW - timedelta(days=900))
    _write_custom(tmp_path, cert_pem, key_pem)
    with pytest.raises(TlsError, match="has expired"):
        _activate(paths, tmp_path)


def test_custom_needs_both_files(paths: DataPaths, tmp_path: Path) -> None:
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "cert.pem").write_bytes(b"x")
    with pytest.raises(TlsError, match=r"both cert\.pem and key\.pem"):
        _activate(paths, tmp_path)


def test_custom_encrypted_key_is_rejected(paths: DataPaths, tmp_path: Path) -> None:
    cert_pem, key_pem = generate_self_signed(TLS.ip_addresses, [], NOW)
    key = serialization.load_pem_private_key(key_pem, password=None)
    encrypted = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"geheim"),
    )
    _write_custom(tmp_path, cert_pem, encrypted)
    with pytest.raises(TlsError, match="encrypted"):
        _activate(paths, tmp_path)


def test_custom_garbage_is_rejected(paths: DataPaths, tmp_path: Path) -> None:
    _write_custom(tmp_path, b"kein zertifikat", b"kein schluessel")
    with pytest.raises(TlsError, match=r"not a valid PEM certificate"):
        _activate(paths, tmp_path)
