"""TLS: eigene CA, Serverzertifikat, Erneuerung, Übergang, eigenes Zertifikat (Konzept 5.2)."""

import os
import ssl
import stat
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address, ip_network
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509 import verification
from cryptography.x509.oid import ExtendedKeyUsageOID

from twinraid.shared.addresses import TlsAddresses, configured
from twinraid.shared.datadirs import DataPaths, prepare_data_dirs
from twinraid.shared.tls import (
    LEGACY_REASON,
    ActiveCertificate,
    TlsError,
    activate,
    certificate_info,
    generate_ca,
    generate_server,
    read_runtime,
    record_refresh_failure,
    runtime_ca_der,
    server_renewal_due,
    uncovered_hosts,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
GID = os.getgid()
TLS = configured([ip_address("192.168.10.20")], ["twinraid.home.arpa"])
TLS_NO_HOSTNAME = configured([ip_address("192.168.10.20")], [])
type ServerName = x509.DNSName | x509.IPAddress
LOOPBACK = configured([ip_address("127.0.0.1")], ["localhost"])


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
    tls: TlsAddresses = TLS,
    now: datetime = NOW,
    renew_server: bool = False,
    new_ca: bool = False,
) -> ActiveCertificate:
    return activate(
        paths, tls, tmp_path / "custom", tmp_path / "run-tls", GID, now, renew_server, new_ca
    )


def _ca_and_key(
    tls: TlsAddresses = TLS, now: datetime = NOW
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    ca_pem, key_pem = generate_ca(tls, now)
    key = serialization.load_pem_private_key(key_pem, password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    return x509.load_pem_x509_certificate(ca_pem), key


def _server(
    tls: TlsAddresses, ca: x509.Certificate, key: ec.EllipticCurvePrivateKey, now: datetime = NOW
) -> x509.Certificate:
    return x509.load_pem_x509_certificate(generate_server(tls, ca, key, now)[0])


def _verify(
    ca: x509.Certificate,
    leaf: x509.Certificate,
    name: ServerName,
) -> None:
    store = verification.Store([ca])
    verifier = verification.PolicyBuilder().store(store).time(NOW).build_server_verifier(name)
    verifier.verify(leaf, [])


# --- CA-Zertifikat --------------------------------------------------------------------


def test_ca_properties() -> None:
    ca, _ = _ca_and_key()
    assert ca.issuer == ca.subject
    assert certificate_info(ca).name == "TwinRaid CA 2026-10-13"
    public_key = ca.public_key()
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    assert public_key.curve.name == "secp256r1"
    assert isinstance(ca.signature_hash_algorithm, hashes.SHA256)
    # 10 Jahre
    assert ca.not_valid_after_utc - ca.not_valid_before_utc == timedelta(days=3650)
    basic = ca.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic.critical
    assert basic.value.ca is True
    assert basic.value.path_length == 0
    usage = ca.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.critical
    assert usage.value.key_cert_sign
    assert usage.value.crl_sign
    assert not usage.value.digital_signature
    eku = ca.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]


def test_ca_name_constraints_allow_only_own_addresses() -> None:
    ca, _ = _ca_and_key()
    constraints = ca.extensions.get_extension_for_class(x509.NameConstraints)
    assert constraints.critical
    assert set(constraints.value.permitted_subtrees or ()) == {
        x509.IPAddress(ip_network("192.168.10.20/32")),
        x509.DNSName("twinraid.home.arpa"),
    }
    assert constraints.value.excluded_subtrees is None


def test_ca_without_hostnames_allows_no_real_dns_name() -> None:
    """Ohne dNSName-Eintrag wäre nach RFC 5280 jeder DNS-Name erlaubt."""
    ca, _ = _ca_and_key(TLS_NO_HOSTNAME)
    constraints = ca.extensions.get_extension_for_class(x509.NameConstraints).value
    assert x509.DNSName("invalid") in (constraints.permitted_subtrees or ())


def test_ipv6_constraint_is_a_single_address() -> None:
    tls = configured([ip_address("fd00::20")], [])
    ca, _ = _ca_and_key(tls)
    constraints = ca.extensions.get_extension_for_class(x509.NameConstraints).value
    assert x509.IPAddress(ip_network("fd00::20/128")) in (constraints.permitted_subtrees or ())


# --- Serverzertifikat -----------------------------------------------------------------


def test_server_certificate_properties() -> None:
    ca, key = _ca_and_key()
    cert_pem, key_pem = generate_server(TLS, ca, key, NOW)
    cert = x509.load_pem_x509_certificate(cert_pem)
    assert cert.issuer == ca.subject
    cert.verify_directly_issued_by(ca)
    assert cert.not_valid_after_utc - cert.not_valid_before_utc == timedelta(days=825)
    basic = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic.critical
    assert basic.value.ca is False
    usage = cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.critical
    assert usage.value.digital_signature
    assert not usage.value.key_cert_sign
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]
    info = certificate_info(cert)
    assert info.ip_addresses == {"192.168.10.20"}
    assert info.hostnames == {"twinraid.home.arpa"}
    aki = cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    assert aki.key_identifier == ski.digest
    private = serialization.load_pem_private_key(key_pem, password=None)
    assert isinstance(private, ec.EllipticCurvePrivateKey)


def test_server_subject_is_empty_and_san_is_critical() -> None:
    """RFC 5280: Bei leerem Subject muss die SAN-Erweiterung kritisch sein."""
    ca, key = _ca_and_key()
    cert = _server(TLS, ca, key)
    assert len(cert.subject) == 0
    assert cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).critical


def test_server_certificate_never_outlives_the_ca() -> None:
    ca, key = _ca_and_key()
    late = ca.not_valid_after_utc - timedelta(days=100)
    cert = _server(TLS, ca, key, late)
    assert cert.not_valid_after_utc == ca.not_valid_after_utc


@pytest.mark.parametrize(
    "name",
    [
        x509.IPAddress(ip_address("192.168.10.20")),
        x509.DNSName("twinraid.home.arpa"),
    ],
)
def test_chain_verifies_for_own_addresses(
    name: ServerName,
) -> None:
    ca, key = _ca_and_key()
    _verify(ca, _server(TLS, ca, key), name)


@pytest.mark.parametrize(
    ("tls", "name"),
    [
        (
            configured([ip_address("192.168.10.21")], []),
            x509.IPAddress(ip_address("192.168.10.21")),
        ),
        (
            configured([ip_address("10.0.0.1")], ["bank.example"]),
            x509.DNSName("bank.example"),
        ),
    ],
)
@pytest.mark.parametrize("ca_tls", [TLS, TLS_NO_HOSTNAME])
def test_ca_cannot_vouch_for_foreign_addresses(
    ca_tls: TlsAddresses, tls: TlsAddresses, name: ServerName
) -> None:
    """Ein gestohlener CA-Schlüssel taugt nur für die eigenen Adressen."""
    ca, key = _ca_and_key(ca_tls)
    with pytest.raises(verification.VerificationError, match="name constraint"):
        _verify(ca, _server(tls, ca, key), name)


def test_openssl_accepts_the_chain_with_only_the_ca_installed(tmp_path: Path) -> None:
    """Handshake mit OpenSSL (wie curl): nur die CA als Vertrauensanker, Hostname geprüft."""
    ca, key = _ca_and_key(LOOPBACK)
    cert_pem, key_pem = generate_server(LOOPBACK, ca, key, datetime.now(UTC))
    chain_file, key_file = tmp_path / "chain.pem", tmp_path / "key.pem"
    chain_file.write_bytes(cert_pem + ca.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key_pem)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(chain_file, key_file)
    for host in ("127.0.0.1", "localhost"):
        client_ctx = ssl.create_default_context(
            cadata=ca.public_bytes(serialization.Encoding.PEM).decode()
        )
        _handshake(server_ctx, client_ctx, host)


def _handshake(server_ctx: ssl.SSLContext, client_ctx: ssl.SSLContext, host: str) -> None:
    c_in, c_out, s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_ctx.wrap_bio(c_in, c_out, server_hostname=host)
    server = server_ctx.wrap_bio(s_in, s_out, server_side=True)
    done = {"client": False, "server": False}
    for _ in range(10):
        for name, side in (("client", client), ("server", server)):
            if not done[name]:
                try:
                    side.do_handshake()
                    done[name] = True
                except ssl.SSLWantReadError:
                    pass
        s_in.write(c_out.read())
        c_in.write(s_out.read())
        if all(done.values()):
            return
    raise AssertionError("handshake did not finish")


# --- Ablage, Erneuerung und Übergang ----------------------------------------------------


def test_first_start_generates_and_restart_keeps(paths: DataPaths, tmp_path: Path) -> None:
    first = _activate(paths, tmp_path)
    assert first.source == "ca"
    assert first.ca_generated_because == "no CA exists yet"
    assert first.server_generated_because == "no server certificate exists yet"
    assert not first.legacy_removed
    second = _activate(paths, tmp_path, now=NOW + timedelta(days=100))
    assert second.ca_generated_because is None
    assert second.server_generated_because is None
    assert second.info.fingerprint == first.info.fingerprint
    assert second.ca.fingerprint == first.ca.fingerprint


def test_file_modes(paths: DataPaths, tmp_path: Path) -> None:
    _activate(paths, tmp_path)
    for path in (
        paths.tls_ca_cert,
        paths.tls_ca_key,
        paths.tls_server_cert,
        paths.tls_server_key,
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    runtime = tmp_path / "run-tls"
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o750
    assert stat.S_IMODE((runtime / "key.pem").stat().st_mode) == 0o640
    assert stat.S_IMODE((runtime / "cert.pem").stat().st_mode) == 0o644
    assert stat.S_IMODE((runtime / "ca.pem").stat().st_mode) == 0o644
    assert (runtime / "key.pem").stat().st_gid == GID


def test_runtime_holds_chain_but_never_the_ca_key(paths: DataPaths, tmp_path: Path) -> None:
    active = _activate(paths, tmp_path)
    runtime = tmp_path / "run-tls"
    chain = x509.load_pem_x509_certificates((runtime / "cert.pem").read_bytes())
    assert [certificate_info(c).fingerprint for c in chain] == [
        active.info.fingerprint,
        active.ca.fingerprint,
    ]
    ca_key = paths.tls_ca_key.read_bytes()
    for entry in runtime.iterdir():
        assert ca_key not in entry.read_bytes()
    state = read_runtime(runtime)
    assert state.source == "ca"
    assert state.ca is not None
    assert certificate_info(state.ca).fingerprint == active.ca.fingerprint
    assert state.renewal_failed_at is None
    der = runtime_ca_der(runtime)
    assert der is not None
    assert x509.load_der_x509_certificate(der) == state.ca


def test_server_renewal_keeps_the_ca(paths: DataPaths, tmp_path: Path) -> None:
    first = _activate(paths, tmp_path)
    later = NOW + timedelta(days=825 - 59)
    renewed = _activate(paths, tmp_path, now=later)
    assert renewed.ca_generated_because is None
    assert renewed.server_generated_because == "the server certificate expires in 58 days"
    assert renewed.ca.fingerprint == first.ca.fingerprint
    assert renewed.info.fingerprint != first.info.fingerprint
    assert renewed.info.not_after > first.info.not_after


def test_no_renewal_61_days_before_expiry(paths: DataPaths, tmp_path: Path) -> None:
    first = _activate(paths, tmp_path)
    later = _activate(paths, tmp_path, now=NOW + timedelta(days=825 - 61))
    assert later.server_generated_because is None
    assert later.info.fingerprint == first.info.fingerprint


def test_renewal_due_only_if_a_new_certificate_would_last_longer() -> None:
    ca, key = _ca_and_key()
    fresh = _server(TLS, ca, key)
    assert not server_renewal_due(fresh, ca, NOW)
    assert server_renewal_due(fresh, ca, NOW + timedelta(days=825 - 30))
    # Kurz vor Ablauf der CA endet das Serverzertifikat mit der CA; eine Erneuerung
    # brächte nichts und würde sich täglich wiederholen.
    capped = _server(TLS, ca, key, ca.not_valid_after_utc - timedelta(days=40))
    assert not server_renewal_due(capped, ca, ca.not_valid_after_utc - timedelta(days=30))


def test_forced_server_renewal(paths: DataPaths, tmp_path: Path) -> None:
    first = _activate(paths, tmp_path)
    renewed = _activate(paths, tmp_path, renew_server=True)
    assert renewed.server_generated_because == "renewal was requested"
    assert renewed.ca_generated_because is None
    assert renewed.info.fingerprint != first.info.fingerprint
    assert renewed.ca.fingerprint == first.ca.fingerprint


def test_new_ca_on_request_also_renews_the_server(paths: DataPaths, tmp_path: Path) -> None:
    first = _activate(paths, tmp_path)
    renewed = _activate(paths, tmp_path, new_ca=True)
    assert renewed.ca_generated_because == "a new CA was requested"
    assert renewed.server_generated_because == (
        "the server certificate was not issued by the current CA"
    )
    assert renewed.ca.fingerprint != first.ca.fingerprint


def test_changed_addresses_need_a_new_ca(paths: DataPaths, tmp_path: Path) -> None:
    first = _activate(paths, tmp_path)
    changed = configured([ip_address("192.168.10.21")], [])
    later = _activate(paths, tmp_path, tls=changed)
    assert later.ca_generated_because == "the TLS addresses have changed"
    assert later.ca.fingerprint != first.ca.fingerprint
    assert later.info.ip_addresses == {"192.168.10.21"}


def test_expired_ca_is_replaced(paths: DataPaths, tmp_path: Path) -> None:
    first = _activate(paths, tmp_path)
    later = _activate(paths, tmp_path, now=NOW + timedelta(days=3651))
    assert later.ca_generated_because == "the CA has expired"
    assert later.ca.fingerprint != first.ca.fingerprint


@pytest.mark.parametrize("damaged", ["tls_ca_cert", "tls_server_cert"])
def test_damaged_files_are_replaced(paths: DataPaths, tmp_path: Path, damaged: str) -> None:
    _activate(paths, tmp_path)
    getattr(paths, damaged).write_bytes(b"kaputt")
    later = _activate(paths, tmp_path)
    reason = later.ca_generated_because or later.server_generated_because
    assert reason is not None
    assert "unusable" in reason


def test_transition_from_self_signed_certificate(paths: DataPaths, tmp_path: Path) -> None:
    """Phase 2 legte ein selbstsigniertes Paar in /data/tls ab; es wird ersetzt."""
    paths.legacy_tls_cert.write_bytes(b"altes zertifikat")
    paths.legacy_tls_key.write_bytes(b"alter schluessel")
    active = _activate(paths, tmp_path)
    assert active.legacy_removed
    assert active.ca_generated_because == LEGACY_REASON
    assert not paths.legacy_tls_cert.exists()
    assert not paths.legacy_tls_key.exists()
    assert paths.tls_ca_cert.exists()
    second = _activate(paths, tmp_path)
    assert not second.legacy_removed


def test_refresh_failure_is_recorded(paths: DataPaths, tmp_path: Path) -> None:
    _activate(paths, tmp_path)
    runtime = tmp_path / "run-tls"
    record_refresh_failure(runtime, NOW, GID)
    assert read_runtime(runtime).renewal_failed_at == NOW
    # Eine erfolgreiche Einrichtung setzt den Vermerk zurück.
    _activate(paths, tmp_path)
    assert read_runtime(runtime).renewal_failed_at is None


# --- Eigenes Zertifikat ------------------------------------------------------------------


def _custom_pair(tls: TlsAddresses = TLS, now: datetime = NOW) -> tuple[bytes, bytes]:
    ca, key = _ca_and_key(tls, now)
    return generate_server(tls, ca, key, now)


def _write_custom(tmp_path: Path, cert_pem: bytes, key_pem: bytes) -> None:
    custom = tmp_path / "custom"
    custom.mkdir(exist_ok=True)
    (custom / "cert.pem").write_bytes(cert_pem)
    (custom / "key.pem").write_bytes(key_pem)


def test_custom_certificate_has_priority(paths: DataPaths, tmp_path: Path) -> None:
    other = configured([ip_address("192.168.10.99")], [])
    cert_pem, key_pem = _custom_pair(other)
    _write_custom(tmp_path, cert_pem, key_pem)
    active = _activate(paths, tmp_path)
    assert active.source == "custom"
    assert active.info.ip_addresses == {"192.168.10.99"}
    state = read_runtime(tmp_path / "run-tls")
    assert state.source == "custom"
    assert certificate_info(state.leaf).fingerprint == active.info.fingerprint
    # Kein CA-Download, aber CA und Serverzertifikat bleiben als Rückfall erhalten.
    assert state.ca is None
    assert runtime_ca_der(tmp_path / "run-tls") is None
    assert paths.tls_ca_cert.exists()
    assert paths.tls_server_cert.exists()
    assert uncovered_hosts(active.info, TLS) == ["192.168.10.20", "twinraid.home.arpa"]


def test_switching_back_from_custom_restores_the_ca_download(
    paths: DataPaths, tmp_path: Path
) -> None:
    _write_custom(tmp_path, *_custom_pair())
    _activate(paths, tmp_path)
    for name in ("cert.pem", "key.pem"):
        (tmp_path / "custom" / name).unlink()
    active = _activate(paths, tmp_path)
    assert active.source == "ca"
    assert read_runtime(tmp_path / "run-tls").ca is not None


def test_custom_wildcard_covers_hostname() -> None:
    # web.tls selbst erlaubt keine Platzhalter, ein eigenes Zertifikat schon.
    wildcard = configured([ip_address("192.168.10.20")], ["*.home.arpa"])
    ca, key = _ca_and_key(TLS_NO_HOSTNAME)
    cert = x509.load_pem_x509_certificate(generate_server(wildcard, ca, key, NOW)[0])
    assert uncovered_hosts(certificate_info(cert), TLS) == []


def test_custom_mismatched_key_is_rejected(paths: DataPaths, tmp_path: Path) -> None:
    cert_pem, _ = _custom_pair()
    _, other_key = _custom_pair()
    _write_custom(tmp_path, cert_pem, other_key)
    with pytest.raises(TlsError, match="do not belong together"):
        _activate(paths, tmp_path)


def test_custom_expired_is_rejected(paths: DataPaths, tmp_path: Path) -> None:
    cert_pem, key_pem = _custom_pair(now=NOW - timedelta(days=900))
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
    cert_pem, key_pem = _custom_pair()
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


def test_sha1_fingerprint_for_windows() -> None:
    ca, _ = _ca_and_key()
    info = certificate_info(ca)
    assert len(info.fingerprint_sha1.split(":")) == 20
    assert len(info.fingerprint.split(":")) == 32
