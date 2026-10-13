"""Automatische Erneuerung im laufenden Betrieb ohne Neustart-Schleife (Konzept 5.2)."""

import json
import os
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path

import pytest

from tests.support.tls import install_test_runtime
from twinraid.config.models import TlsConfig
from twinraid.shared.tls import install_runtime
from twinraid.web.tls_renewal import RenewalWatcher

TLS = TlsConfig(ip_addresses=[ip_address("192.168.10.20")], hostnames=[])
ISSUED = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
# 30 Tage vor Ablauf des Serverzertifikats: Erneuerung fällig.
DUE = ISSUED + timedelta(days=825 - 30)


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.runtime = tmp_path / "run-tls"
        self.marker = tmp_path / "web" / "tls-renewal.json"
        self.marker.parent.mkdir()
        install_test_runtime(self.runtime, TLS, ISSUED)
        self.now = ISSUED
        self.restarts = 0

    def restart(self) -> None:
        self.restarts += 1

    def watcher(self) -> RenewalWatcher:
        return RenewalWatcher(self.runtime, self.marker, self.restart, lambda: self.now)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_no_restart_while_the_certificate_is_fresh(harness: Harness) -> None:
    harness.now = ISSUED + timedelta(days=700)
    assert harness.watcher().check() is False
    assert harness.restarts == 0
    assert not harness.marker.exists()


def test_due_renewal_restarts_and_notes_the_time(harness: Harness) -> None:
    harness.now = DUE
    assert harness.watcher().check() is True
    assert harness.restarts == 1
    assert json.loads(harness.marker.read_text()) == {"requested_at": DUE.isoformat()}
    assert harness.marker.stat().st_mode & 0o777 == 0o600


def test_failed_renewal_waits_24_hours(harness: Harness) -> None:
    """Scheitert die Erneuerung, beendet sich der Web-Prozess erst nach 24 Stunden erneut."""
    harness.now = DUE
    assert harness.watcher().check() is True
    # Neustart, aber das Zertifikat wurde nicht erneuert (z. B. Schreibfehler als root).
    for hours in (1, 12, 23):
        harness.now = DUE + timedelta(hours=hours)
        assert harness.watcher().check() is False
    assert harness.restarts == 1
    harness.now = DUE + timedelta(hours=24)
    assert harness.watcher().check() is True
    assert harness.restarts == 2


def test_note_survives_a_restart_of_the_web_process(harness: Harness) -> None:
    harness.now = DUE
    assert harness.watcher().check() is True
    # Neuer Prozess, neuer Watcher: Der Vermerk auf der Platte zählt.
    harness.now = DUE + timedelta(minutes=5)
    assert harness.watcher().check() is False


def test_unreadable_note_is_rewritten(harness: Harness) -> None:
    harness.marker.write_text("kaputt")
    harness.now = DUE
    assert harness.watcher().check() is True
    assert json.loads(harness.marker.read_text())["requested_at"] == DUE.isoformat()


def test_no_restart_if_the_note_cannot_be_written(harness: Harness) -> None:
    """Ohne Vermerk gibt es keinen Schutz vor einer Schleife: dann nicht beenden."""
    harness.marker.parent.chmod(0o500)
    try:
        harness.now = DUE
        assert harness.watcher().check() is False
    finally:
        harness.marker.parent.chmod(0o700)
    assert harness.restarts == 0


def test_custom_certificate_is_never_renewed(harness: Harness) -> None:
    runtime = harness.runtime
    chain = (runtime / "cert.pem").read_bytes()
    key = (runtime / "key.pem").read_bytes()
    install_runtime(runtime, chain, key, None, "custom", os.getgid())
    harness.now = DUE
    assert harness.watcher().check() is False


def test_missing_runtime_is_ignored(tmp_path: Path) -> None:
    calls: list[int] = []
    watcher = RenewalWatcher(tmp_path / "none", tmp_path / "m.json", lambda: calls.append(1))
    assert watcher.check() is False
    assert calls == []
