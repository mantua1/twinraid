"""Startverhalten des Workers bei fehlerhafter Umgebung."""

import asyncio
import logging
import os
import signal
from ipaddress import IPv4Address, ip_address
from pathlib import Path
from typing import cast

import pytest

from tests.conftest import EXAMPLE_CONFIG
from twinraid.config import load_config
from twinraid.integrations.wol import BroadcastError
from twinraid.shared.mode import RunMode
from twinraid.sync.coordinator import SyncCoordinator
from twinraid.worker.actions import ActionCoordinator
from twinraid.worker.main import EXIT_STARTUP_ERROR, _serve, heartbeat, main, wake_on_lan
from twinraid.worker.monitor import StatusMonitor
from twinraid.worker.password_check import PasswordCheck
from twinraid.worker.server import WorkerServer

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


def test_signals_from_the_admin_cli(tmp_path: Path) -> None:
    """SIGUSR1 kommt von unlock, SIGUSR2 von trust-host-key (s6-svc -1/-2, Konzept 3.5)."""
    calls: list[str] = []

    class Server:
        path = tmp_path / "worker.sock"

        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def start(self) -> None:
            self.started.set()

        async def close(self) -> None:
            calls.append("server closed")

    class Check:
        def reset(self) -> None:
            calls.append("throttle reset")

    class Monitor:
        async def run(self) -> None:
            await asyncio.Event().wait()

    class Actions:
        async def close(self) -> None:
            calls.append("actions closed")

    class Sync:
        async def close(self) -> None:
            calls.append("sync closed")

    async def run() -> None:
        server = Server()
        task = asyncio.create_task(
            _serve(
                cast("WorkerServer", server),
                cast("PasswordCheck", Check()),
                cast("StatusMonitor", Monitor()),
                cast("ActionCoordinator", Actions()),
                cast("SyncCoordinator", Sync()),
                retry_status_now=lambda: calls.append("status retry"),
            )
        )
        # Die Handler stehen, bevor der Server startet.
        await server.started.wait()
        os.kill(os.getpid(), signal.SIGUSR2)
        os.kill(os.getpid(), signal.SIGUSR1)
        for _ in range(100):
            if len(calls) == 2:
                break
            await asyncio.sleep(0.01)
        os.kill(os.getpid(), signal.SIGTERM)
        await task

    asyncio.run(run())
    assert sorted(calls[:2]) == ["status retry", "throttle reset"]
    # Ein laufender Probelauf endet vor den Aktionen (interrupted im Aktionslog).
    assert calls[2:] == ["sync closed", "actions closed", "server closed"]


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
