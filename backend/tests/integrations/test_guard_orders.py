"""Aufträge an den Wächter (Konzept 3.6, „Auftragsnummer und Neustarts“, ab v0.5.1)."""

import copy
import hashlib
import os
import re
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import REPO_ROOT
from tests.support.guard_status import HEARTBEAT, NOW, STATUS, guard_paths, raw
from twinraid.integrations.guard_orders import (
    MAX_SEQ,
    GuardOrderError,
    OrderTimings,
    RealGuardChannel,
    order_line,
    write_order,
)
from twinraid.integrations.source_guard import GuardPaths
from twinraid.shared.actions import ActionOutcome
from twinraid.shared.source_guard import GuardOrder, GuardOrderResult

pytestmark = pytest.mark.anyio

SCRIPT = REPO_ROOT / "host-scripts" / "source-guard.sh"


def _script_order_re() -> re.Pattern[str]:
    """Derselbe reguläre Ausdruck wie im Wächter (ERE, auch in Python gültig)."""
    match = re.search(r"^readonly ORDER_RE='(.*)'$", SCRIPT.read_text(), re.MULTILINE)
    assert match is not None
    return re.compile(match.group(1))


@pytest.mark.parametrize("order", [o for o in GuardOrder if o is not GuardOrder.STOP])
def test_order_line_is_accepted_by_the_guard(order: GuardOrder) -> None:
    line = order_line(42, order)
    assert len(line) == 256
    assert line.endswith(b"\n")
    text = line[:-1].decode("ascii")
    match = _script_order_re().fullmatch(text)
    assert match is not None
    payload, checksum = text.rstrip(" ").rsplit(" ", 1)
    assert payload == f"TRSG1 42 {order.value}"
    assert checksum == hashlib.sha256(payload.encode()).hexdigest()[:16]


@pytest.mark.parametrize(("seq", "order"), [(0, GuardOrder.PING), (MAX_SEQ + 1, GuardOrder.PING)])
def test_order_line_limits(seq: int, order: GuardOrder) -> None:
    with pytest.raises(ValueError, match="out of range"):
        order_line(seq, order)
    with pytest.raises(ValueError, match="container"):
        order_line(1, GuardOrder.STOP)


def test_write_order_in_place(tmp_path: Path) -> None:
    """Die Datei ist eingebunden: gleiche Datei, nur der Inhalt ändert sich."""
    request = tmp_path / "request"
    request.write_bytes(b"x" * 300)
    inode = request.stat().st_ino
    line = order_line(1, GuardOrder.SNAPSHOT)
    write_order(request, line)
    assert request.read_bytes() == line
    assert request.stat().st_ino == inode


def test_write_order_refuses_links_and_folders(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_bytes(b"")
    link = tmp_path / "request"
    link.symlink_to(target)
    with pytest.raises(OSError, match=r"symbolic link|Too many levels"):
        write_order(link, order_line(1, GuardOrder.SNAPSHOT))
    assert target.read_bytes() == b""
    folder = tmp_path / "folder"
    folder.mkdir()
    with pytest.raises(OSError):  # noqa: PT011 - Verzeichnis: EISDIR je nach System
        write_order(folder, order_line(1, GuardOrder.SNAPSHOT))


class FakeGuard:
    """Antwortet auf die Auftragsdatei wie source-guard.sh, während der Kanal wartet."""

    def __init__(self, paths: GuardPaths) -> None:
        self.paths = paths
        self.status: dict[str, Any] = copy.deepcopy(STATUS)
        self.status["last_seq"] = 7
        self.result = "created"
        self.answer = True
        self.on_wait: list[str] = []
        self.monotonic = 0.0
        self.sleeps: list[float] = []
        self.write()

    def write(self) -> None:
        self.paths.status_file.write_bytes(raw(self.status))

    def now(self) -> float:
        return self.monotonic

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.monotonic += seconds
        for change in self.on_wait:
            if change == "instance":
                self.status["instance"] = "fedcba9876543210"
            elif change == "lost":
                self.status["error"] = "state_lost"
            elif change == "ahead":
                self.status["last_seq"] += 5
        self.on_wait = []
        line = self.paths.request.read_bytes()
        match = re.match(rb"TRSG1 ([0-9]+) ([a-z]+) ", line)
        if self.answer and match is not None and int(match[1]) > self.status["last_seq"]:
            seq = int(match[1])
            self.status["last_seq"] = seq
            self.status["last"] = {
                "seq": seq,
                "order": match[2].decode(),
                "container": None,
                "result": self.result,
                "at": HEARTBEAT,
            }
        self.write()


def _channel(tmp_path: Path) -> tuple[RealGuardChannel, FakeGuard]:
    paths = guard_paths(tmp_path)
    guard = FakeGuard(paths)
    channel = RealGuardChannel(
        paths, OrderTimings(), clock=lambda: NOW, monotonic=guard.now, sleep=guard.sleep
    )
    return channel, guard


async def test_order_and_answer(tmp_path: Path) -> None:
    channel, guard = _channel(tmp_path)
    assert await channel.order(GuardOrder.SNAPSHOT) is GuardOrderResult.CREATED
    # Die nächste Nummer nach last_seq; die Nummer führt allein der Wächter.
    assert guard.paths.request.read_bytes() == order_line(8, GuardOrder.SNAPSHOT)
    guard.result = "preparing"
    assert await channel.order(GuardOrder.PREPARE) is GuardOrderResult.PREPARING
    assert guard.paths.request.read_bytes() == order_line(9, GuardOrder.PREPARE)


async def test_at_most_one_order_per_second(tmp_path: Path) -> None:
    channel, guard = _channel(tmp_path)
    await channel.order(GuardOrder.SNAPSHOT)
    first_waits = list(guard.sleeps)
    await channel.order(GuardOrder.CLOSE)
    # Vor dem zweiten Auftrag wartet der Kanal den Rest der Sekunde seit dem ersten ab.
    gap = guard.sleeps[len(first_waits)]
    assert gap == pytest.approx(1.0 - sum(first_waits))


async def test_inactive_guard_gets_no_order(tmp_path: Path) -> None:
    channel, guard = _channel(tmp_path)
    guard.status["heartbeat_at"] = HEARTBEAT - 60
    guard.write()
    with pytest.raises(GuardOrderError) as caught:
        await channel.order(GuardOrder.SNAPSHOT)
    assert caught.value.outcome is ActionOutcome.SOURCE_GUARD_INACTIVE
    assert guard.paths.request.read_bytes() == b""


@pytest.mark.parametrize(
    ("change", "outcome"),
    [
        ("instance", ActionOutcome.SOURCE_GUARD_LOST),
        ("lost", ActionOutcome.SOURCE_GUARD_LOST),
        ("ahead", ActionOutcome.SOURCE_GUARD_LOST),
    ],
)
async def test_guard_changes_while_waiting(
    tmp_path: Path, change: str, outcome: ActionOutcome
) -> None:
    channel, guard = _channel(tmp_path)
    guard.answer = False
    guard.on_wait = [change]
    with pytest.raises(GuardOrderError) as caught:
        await channel.order(GuardOrder.SNAPSHOT)
    assert caught.value.outcome is outcome


async def test_no_answer_in_time(tmp_path: Path) -> None:
    channel, guard = _channel(tmp_path)
    guard.answer = False
    with pytest.raises(GuardOrderError) as caught:
        await channel.order(GuardOrder.SNAPSHOT)
    assert caught.value.outcome is ActionOutcome.SOURCE_GUARD_LOST
    assert guard.monotonic >= 10.0


async def test_sequence_numbers_exhausted(tmp_path: Path) -> None:
    channel, guard = _channel(tmp_path)
    guard.status["last_seq"] = MAX_SEQ
    guard.write()
    with pytest.raises(GuardOrderError) as caught:
        await channel.order(GuardOrder.SNAPSHOT)
    assert caught.value.outcome is ActionOutcome.SOURCE_GUARD_LOST
    assert guard.paths.request.read_bytes() == b""


async def test_unusable_order_file(tmp_path: Path) -> None:
    channel, guard = _channel(tmp_path)
    guard.paths.request.unlink()
    os.mkfifo(guard.paths.request)
    with pytest.raises(GuardOrderError) as caught:
        await channel.order(GuardOrder.SNAPSHOT)
    assert caught.value.outcome is ActionOutcome.SOURCE_GUARD_INACTIVE
