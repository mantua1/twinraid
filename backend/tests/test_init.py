"""Startdienst twinraid-init."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509

from tests.conftest import EXAMPLE_CONFIG
from twinraid.init import main
from twinraid.shared.datadirs import DataPaths
from twinraid.shared.tls import certificate_info, read_runtime


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    monkeypatch.setenv("TWINRAID_CONFIG", str(EXAMPLE_CONFIG))
    monkeypatch.setenv("TWINRAID_DATA_DIR", str(data))
    monkeypatch.setenv("TWINRAID_RUNTIME_TLS_DIR", str(tmp_path / "run-tls"))
    monkeypatch.setenv("TWINRAID_CUSTOM_TLS_DIR", str(tmp_path / "custom"))
    monkeypatch.setenv("TWINRAID_WEB_GID", str(os.getgid()))
    return tmp_path


def _served_fingerprint(env: Path) -> str:
    return certificate_info(read_runtime(env / "run-tls").leaf).fingerprint


def test_init_sets_up_tls_and_logs_fingerprints(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([]) == 0
    err = capsys.readouterr().err
    state = read_runtime(env / "run-tls")
    assert state.source == "ca"
    assert state.ca is not None
    ca = certificate_info(state.ca)
    assert "Generated the TwinRaid CA" in err
    assert f"TLS CA SHA-256 fingerprint: {ca.fingerprint}" in err
    assert f"TLS CA SHA-1 fingerprint (shown by Windows): {ca.fingerprint_sha1}" in err
    assert f"TLS server certificate (ca) SHA-256 fingerprint: {_served_fingerprint(env)}" in err
    assert "No password is set" in err


def test_init_reports_the_transition_from_self_signed(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = DataPaths(env / "data")
    assert main([]) == 0
    paths.legacy_tls_cert.write_bytes(b"alt")
    assert main([]) == 0
    assert "Replaced the self-signed certificate" in capsys.readouterr().err
    assert not paths.legacy_tls_cert.exists()


def test_init_refuses_unsafe_data_dir(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (env / "data").chmod(0o777)
    assert main([]) == 1
    err = capsys.readouterr().err
    assert "writable by group or others" in err
    assert "will not start" in err


def test_init_requires_web_gid(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TWINRAID_WEB_GID")
    assert main([]) == 1


def test_refresh_is_quiet_when_nothing_is_due(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([]) == 0
    before = _served_fingerprint(env)
    capsys.readouterr()
    assert main(["--refresh-tls"]) == 0
    assert capsys.readouterr().err == ""
    assert _served_fingerprint(env) == before


def test_refresh_renews_a_due_server_certificate(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([]) == 0
    before = read_runtime(env / "run-tls")
    later = datetime.now(UTC) + timedelta(days=800)
    monkeypatch.setattr("twinraid.init.datetime", _FrozenDatetime(later))
    assert main(["--refresh-tls"]) == 0
    after = read_runtime(env / "run-tls")
    assert certificate_info(after.leaf).fingerprint != certificate_info(before.leaf).fingerprint
    assert after.ca == before.ca
    assert "Renewed the TLS server certificate" in capsys.readouterr().err


def test_refresh_failure_keeps_the_certificate_and_is_recorded(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([]) == 0
    before = _served_fingerprint(env)
    # Defektes eigenes Zertifikat: Die Einrichtung scheitert.
    custom = env / "custom"
    custom.mkdir()
    (custom / "cert.pem").write_bytes(b"x")
    assert main(["--refresh-tls"]) == 1
    assert "Automatic TLS renewal failed" in capsys.readouterr().err
    state = read_runtime(env / "run-tls")
    assert certificate_info(state.leaf).fingerprint == before
    assert state.renewal_failed_at is not None


class _FrozenDatetime:
    """Ersetzt datetime im Modul twinraid.init; nur now() wird verwendet."""

    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self, tz: object = None) -> datetime:
        return self._value


def test_leaf_is_first_in_chain(env: Path) -> None:
    assert main([]) == 0
    chain = x509.load_pem_x509_certificates((env / "run-tls" / "cert.pem").read_bytes())
    assert len(chain) == 2
    assert chain[0].issuer == chain[1].subject
