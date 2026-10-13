"""Drosselung im Worker: Mindestdauer, steigende Verzögerung, eine Prüfung gleichzeitig."""

import asyncio
from pathlib import Path

import pytest

from tests.support.passwords import TEST_PASSWORD, fast_hasher
from twinraid.shared.passwords import PasswordStore, VerifyOutcome
from twinraid.worker.password_check import PasswordCheck

pytestmark = pytest.mark.anyio


class FakeTime:
    """Uhr, die nur durch sleep() vorrückt; merkt sich alle Wartezeiten."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def check(tmp_path: Path, fake_time: FakeTime) -> PasswordCheck:
    store = PasswordStore(tmp_path / "pw", fast_hasher())
    store.set(TEST_PASSWORD)
    return PasswordCheck(store, clock=fake_time.clock, sleep=fake_time.sleep)


async def test_every_check_takes_at_least_one_second(
    check: PasswordCheck, fake_time: FakeTime
) -> None:
    assert await check.verify(TEST_PASSWORD) is VerifyOutcome.VALID
    assert fake_time.sleeps == [1.0]


async def test_delay_grows_after_five_failures(check: PasswordCheck, fake_time: FakeTime) -> None:
    for _ in range(11):
        assert await check.verify("falsch-falsch-falsch") is VerifyOutcome.INVALID
    # Vor jeder Prüfung: Strafzeit (falls vorhanden), danach Auffüllen auf 1 Sekunde.
    penalties = [s for s in fake_time.sleeps if s != 1.0]
    assert penalties == [2.0, 4.0, 8.0, 16.0, 30.0]
    assert check.penalty() == 30.0


async def test_first_penalty_is_one_second(check: PasswordCheck) -> None:
    for _ in range(5):
        await check.verify("falsch-falsch-falsch")
    assert check.penalty() == 1.0


async def test_failures_expire_after_15_minutes(check: PasswordCheck, fake_time: FakeTime) -> None:
    for _ in range(6):
        await check.verify("falsch-falsch-falsch")
    assert check.penalty() > 0
    fake_time.now += 15 * 60 + 1
    assert check.penalty() == 0.0


async def test_success_resets(check: PasswordCheck) -> None:
    for _ in range(7):
        await check.verify("falsch-falsch-falsch")
    assert await check.verify(TEST_PASSWORD) is VerifyOutcome.VALID
    assert check.penalty() == 0.0


async def test_reset_by_admin(check: PasswordCheck) -> None:
    for _ in range(7):
        await check.verify("falsch-falsch-falsch")
    check.reset()
    assert check.penalty() == 0.0


async def test_not_set_does_not_count_as_failure(tmp_path: Path, fake_time: FakeTime) -> None:
    store = PasswordStore(tmp_path / "pw", fast_hasher())
    check = PasswordCheck(store, clock=fake_time.clock, sleep=fake_time.sleep)
    for _ in range(8):
        assert await check.verify("irgendwas-langes") is VerifyOutcome.NOT_SET
    assert check.penalty() == 0.0


async def test_only_one_check_at_a_time(tmp_path: Path) -> None:
    active = 0
    peak = 0

    class SlowStore(PasswordStore):
        def verify(self, password: str) -> VerifyOutcome:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            import time

            time.sleep(0.02)
            active -= 1
            return VerifyOutcome.INVALID

    async def no_sleep(_seconds: float) -> None:
        return None

    check = PasswordCheck(SlowStore(tmp_path / "pw", fast_hasher()), sleep=no_sleep)
    await asyncio.gather(*(check.verify("x") for _ in range(5)))
    assert peak == 1
