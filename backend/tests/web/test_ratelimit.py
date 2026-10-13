"""Rate-Limit beim Login (Konzept 5.3)."""

import asyncio
import sqlite3
from pathlib import Path

import httpx2
import pytest

from tests.support.passwords import TEST_PASSWORD
from tests.web.helpers import (
    BASE_URL,
    FakeClock,
    FakeWorker,
    make_app,
    make_client,
)
from twinraid.shared.passwords import VerifyOutcome
from twinraid.web.db import WebDatabase
from twinraid.web.ratelimit import (
    GLOBAL_DELAY_SECONDS,
    LoginLimiter,
    ip_key,
)

WRONG = {"password": "falsches-passwort"}
RIGHT = {"password": TEST_PASSWORD}


def _audit(tmp_path: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(tmp_path / "web.sqlite") as db:
        return db.execute("SELECT action, result FROM audit_log ORDER BY id").fetchall()


def test_five_failures_lock_the_ip_for_15_minutes(tmp_path: Path) -> None:
    clock = FakeClock()
    worker = FakeWorker()
    app = make_app(tmp_path, worker, clock=clock)
    client = make_client(app)
    for _ in range(5):
        assert client.post("/api/auth/login", json=WRONG).status_code == 401
    assert ("login", "lockout_started") in _audit(tmp_path)

    locked = client.post("/api/auth/login", json=RIGHT)
    assert locked.status_code == 429
    assert locked.json()["detail"] == "locked"
    assert 0 < int(locked.headers["retry-after"]) <= 15 * 60
    # Während der Sperre wird das Passwort gar nicht erst geprüft.
    assert worker.verify_calls == 5

    clock.advance(15 * 60 + 1)
    assert client.post("/api/auth/login", json=RIGHT).status_code == 200


def test_lock_does_not_affect_other_ips(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    attacker = make_client(app, ip="192.168.20.66")
    for _ in range(6):
        attacker.post("/api/auth/login", json=WRONG)
    assert attacker.post("/api/auth/login", json=RIGHT).status_code == 429
    owner = make_client(app, ip="192.168.20.5")
    assert owner.post("/api/auth/login", json=RIGHT).status_code == 200


def test_success_resets_the_ip_counter(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path))
    for _ in range(4):
        client.post("/api/auth/login", json=WRONG)
    assert client.post("/api/auth/login", json=RIGHT).status_code == 200
    for _ in range(4):
        assert client.post("/api/auth/login", json=WRONG).status_code == 401
    assert client.post("/api/auth/login", json=RIGHT).status_code == 200


def test_failures_older_than_15_minutes_do_not_count(tmp_path: Path) -> None:
    clock = FakeClock()
    client = make_client(make_app(tmp_path, clock=clock))
    for _ in range(4):
        client.post("/api/auth/login", json=WRONG)
    clock.advance(15 * 60 + 1)
    for _ in range(4):
        client.post("/api/auth/login", json=WRONG)
    assert client.post("/api/auth/login", json=RIGHT).status_code == 200


def test_locks_survive_a_restart(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path))
    for _ in range(5):
        client.post("/api/auth/login", json=WRONG)
    restarted = make_client(make_app(tmp_path))
    assert restarted.post("/api/auth/login", json=RIGHT).status_code == 429


def test_unlock_all(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path))
    for _ in range(5):
        client.post("/api/auth/login", json=WRONG)
    database = WebDatabase(tmp_path / "web.sqlite")
    assert LoginLimiter(database).unlock_all() == 1
    assert client.post("/api/auth/login", json=RIGHT).status_code == 200


@pytest.mark.parametrize(
    ("address", "key"),
    [
        ("192.168.20.5", "192.168.20.5"),
        ("::ffff:192.168.20.5", "192.168.20.5"),
        ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:bbbb::99", "2001:db8:1:2::/64"),
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
    ],
)
def test_ip_keys(address: str, key: str) -> None:
    assert ip_key(address) == key


def test_ipv6_addresses_of_one_network_share_a_lock(tmp_path: Path) -> None:
    clock = FakeClock()
    database = WebDatabase(tmp_path / "web.sqlite")
    database.initialize()
    limiter = LoginLimiter(database, clock)
    for host in range(1, 6):
        limiter.record_failure(ip_key(f"2001:db8:1:2::{host}"))
    assert limiter.locked_until(ip_key("2001:db8:1:2::99")) is not None
    assert limiter.locked_until(ip_key("2001:db8:1:3::1")) is None


def test_global_delay_without_hard_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await real_sleep(0)

    # Die Login-Route nutzt asyncio.sleep für die globale Verzögerung.
    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    app = make_app(tmp_path)
    # 20 Fehlversuche von 5 verschiedenen IPs: Jede IP bleibt unter ihrer Grenze.
    for host in range(10, 15):
        attacker = make_client(app, ip=f"192.168.20.{host}")
        for _ in range(4):
            attacker.post("/api/auth/login", json=WRONG)
    assert sleeps == []
    owner = make_client(app, ip="192.168.20.5")
    # Keine Sperre: Der Nutzer kommt herein, nur mit Verzögerung.
    assert owner.post("/api/auth/login", json=RIGHT).status_code == 200
    assert sleeps == [GLOBAL_DELAY_SECONDS]


class BlockingWorker(FakeWorker):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.both_waiting = asyncio.Event()
        self.waiting = 0

    async def verify_password(self, password: str) -> VerifyOutcome:
        self.waiting += 1
        if self.waiting >= 2:
            self.both_waiting.set()
        await self.release.wait()
        return await super().verify_password(password)


@pytest.mark.anyio
async def test_at_most_two_checks_at_the_same_time(tmp_path: Path) -> None:
    worker = BlockingWorker()
    app = make_app(tmp_path, worker)

    def client(host: int) -> httpx2.AsyncClient:
        transport = httpx2.ASGITransport(app=app, client=(f"192.168.20.{host}", 1))
        return httpx2.AsyncClient(
            transport=transport, base_url=BASE_URL, headers={"Origin": BASE_URL}
        )

    async with client(10) as a, client(11) as b, client(12) as c:
        first = asyncio.create_task(a.post("/api/auth/login", json=WRONG))
        second = asyncio.create_task(b.post("/api/auth/login", json=WRONG))
        await asyncio.wait_for(worker.both_waiting.wait(), timeout=5)
        busy = await c.post("/api/auth/login", json=RIGHT)
        assert busy.status_code == 429
        assert busy.json()["detail"] == "busy"
        assert busy.headers["retry-after"] == "5"
        worker.release.set()
        assert (await first).status_code == 401
        assert (await second).status_code == 401
        assert (await c.post("/api/auth/login", json=RIGHT)).status_code == 200
    assert ("login", "busy") in _audit(tmp_path)
