"""Docker-Healthcheck: TLS-Handshake ohne HTTP und Herzschlag des Workers."""

import contextlib
import os
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from twinraid import healthcheck
from twinraid.shared.tls import generate_self_signed, install_runtime


def _write_pair(directory: Path) -> tuple[Path, Path]:
    cert, key = generate_self_signed([ip_address("127.0.0.1")], [], datetime.now(UTC))
    install_runtime(directory, cert, key, "self-signed", os.getgid())
    return directory / "cert.pem", directory / "key.pem"


class TlsServer:
    """Minimaler TLS-Server, der nur Handshakes annimmt und keine Daten liest."""

    def __init__(self, cert: Path, key: Path) -> None:
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.received: list[bytes] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                with self.context.wrap_socket(conn, server_side=True) as tls:
                    tls.settimeout(1)
                    with contextlib.suppress(TimeoutError, ssl.SSLError, OSError):
                        self.received.append(tls.recv(100))
            except ssl.SSLError, OSError:
                pass

    def close(self) -> None:
        # shutdown() weckt das blockierte accept() und beendet das Lauschen sofort.
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
        self.thread.join(timeout=2)


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, TlsServer]]:
    cert, key = _write_pair(tmp_path / "served")
    server = TlsServer(cert, key)
    config = tmp_path / "config.yaml"
    config.write_text(EXAMPLE_CONFIG.read_text().replace("port: 8443", f"port: {server.port}"))
    run = tmp_path / "run"
    run.mkdir()
    (run / "worker.heartbeat").touch()
    monkeypatch.setenv("TWINRAID_CONFIG", str(config))
    monkeypatch.setenv("TWINRAID_RUNTIME_TLS_DIR", str(tmp_path / "served"))
    monkeypatch.setenv("TWINRAID_WORKER_SOCKET", str(run / "worker.sock"))
    yield tmp_path, server
    server.close()


def test_healthy(setup: tuple[Path, TlsServer], capsys: pytest.CaptureFixture[str]) -> None:
    _, server = setup
    assert healthcheck.main() == 0
    assert capsys.readouterr().out == "healthy\n"
    # Keine HTTP-Anfrage: Der Server hat nach dem Handshake keine Daten erhalten.
    time.sleep(0.05)
    assert all(data == b"" for data in server.received)


def test_other_certificate_is_unhealthy(
    setup: tuple[Path, TlsServer],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tmp_path, _ = setup
    _write_pair(tmp_path / "other")
    monkeypatch.setenv("TWINRAID_RUNTIME_TLS_DIR", str(tmp_path / "other"))
    assert healthcheck.main() == 1
    assert "different certificate" in capsys.readouterr().err


def test_stale_heartbeat_is_unhealthy(
    setup: tuple[Path, TlsServer], capsys: pytest.CaptureFixture[str]
) -> None:
    tmp_path, _ = setup
    old = time.time() - 120
    os.utime(tmp_path / "run" / "worker.heartbeat", (old, old))
    assert healthcheck.main() == 1
    assert "heartbeat is 120 s old" in capsys.readouterr().err


def test_missing_heartbeat_is_unhealthy(setup: tuple[Path, TlsServer]) -> None:
    tmp_path, _ = setup
    (tmp_path / "run" / "worker.heartbeat").unlink()
    assert healthcheck.main() == 1


def test_closed_port_is_unhealthy(
    setup: tuple[Path, TlsServer], capsys: pytest.CaptureFixture[str]
) -> None:
    _, server = setup
    server.close()
    assert healthcheck.main() == 1
    assert "does not accept TLS connections" in capsys.readouterr().err
