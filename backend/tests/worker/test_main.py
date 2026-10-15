"""Startverhalten des Workers bei fehlerhafter Umgebung."""

import asyncio
import logging
from ipaddress import IPv4Address, ip_address
from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from twinraid.config import load_config
from twinraid.integrations.wol import BroadcastError
from twinraid.shared.mode import RunMode
from twinraid.worker.main import EXIT_STARTUP_ERROR, heartbeat, main, wake_on_lan

CONFIG = load_config(EXAMPLE_CONFIG)
# Groß in einem anderen Netz als die Testschnittstelle (192.168.10.20/24).
ELSEWHERE = CONFIG.model_copy(
    update={
        "servers": CONFIG.servers.model_copy(
            update={
                "target": CONFIG.servers.target.model_copy(update={"host": ip_address("10.9.8.7")})
            }
        )
    }
)


@pytest.fixture(autouse=True)
def _valid_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TWINRAID_CONFIG", str(EXAMPLE_CONFIG))
    monkeypatch.setenv("TWINRAID_WEB_UID", "10001")
    monkeypatch.setenv("TWINRAID_WORKER_SOCKET", str(tmp_path / "w.sock"))


def test_missing_mode_stops_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TWINRAID_MODE")
    assert main() == EXIT_STARTUP_ERROR


def test_missing_web_uid_stops_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TWINRAID_WEB_UID")
    assert main() == EXIT_STARTUP_ERROR


def test_invalid_config_stops_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("web: {}\n")
    monkeypatch.setenv("TWINRAID_CONFIG", str(bad))
    assert main() == EXIT_STARTUP_ERROR


def test_heartbeat_updates_file(tmp_path: Path) -> None:

    path = tmp_path / "worker.heartbeat"

    async def run() -> list[float]:
        task = asyncio.create_task(heartbeat(path, interval=0.05))
        times: list[float] = []
        for _ in range(3):
            await asyncio.sleep(0.07)
            times.append(path.stat().st_mtime_ns)
        task.cancel()
        return [float(t) for t in times]

    times = asyncio.run(run())
    assert times == sorted(times)
    assert times[0] < times[-1]
    assert (path.stat().st_mode & 0o777) == 0o600


def test_wake_on_lan_is_not_used_in_simulation() -> None:
    assert wake_on_lan(ELSEWHERE, RunMode.MOCK) is None


def test_wake_on_lan_broadcast_is_derived(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        wol = wake_on_lan(CONFIG, RunMode.REAL)
    assert wol is not None
    assert wol.broadcast == IPv4Address("192.168.10.255")
    assert "Wake-on-LAN broadcast address: 192.168.10.255 (derived from" in caplog.text


def test_missing_broadcast_only_warns_without_wake(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert wake_on_lan(ELSEWHERE, RunMode.REAL) is None
    assert "Wake-on-LAN is not available" in caplog.text


def test_missing_broadcast_stops_the_worker_when_wake_is_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Konzept 4.2: Mit actions.wake muss sich die Broadcast-Adresse bestimmen lassen."""
    config = ELSEWHERE.model_copy(
        update={"actions": ELSEWHERE.actions.model_copy(update={"wake": True})}
    )
    with pytest.raises(BroadcastError):
        wake_on_lan(config, RunMode.REAL)
    text = EXAMPLE_CONFIG.read_text()
    text = text.replace("host: 192.168.10.11", "host: 10.9.8.7").replace(
        "  wake: false ", "  wake: true  "
    )
    path = tmp_path / "config.yaml"
    path.write_text(text)
    monkeypatch.setenv("TWINRAID_CONFIG", str(path))
    monkeypatch.setenv("TWINRAID_MODE", "real")
    assert main() == EXIT_STARTUP_ERROR
