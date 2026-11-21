"""tests/support/reaper.py: Prozesse aus Tests überleben weder den Test noch pytest."""

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from tests.support import reaper

_TIMEOUT = 10.0


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


def _session_members(session: int) -> list[int]:
    return [pid for pid, sid, _ in reaper._processes() if sid == session]


def _wait_until(condition: Callable[[], bool], timeout: float = _TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def _guard_like(directory: Path) -> subprocess.Popen[bytes]:
    """Wie ein Wächter im Test: eigene Sitzung, Pfad des Test-Ordners in der Befehlszeile,
    dazu ein Kind ohne diesen Pfad (wie sleep im Wächter)."""
    directory.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(  # noqa: S603 - feste Argumente, nur im Test
        ["/bin/bash", "-c", "sleep 300 & wait", str(directory / "source-guard.sh")],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_for_child(leader: subprocess.Popen[bytes]) -> None:
    assert _wait_until(lambda: len(_session_members(leader.pid)) == 2)


def test_marker_ends_with_a_slash(tmp_path: Path) -> None:
    """…/test_a1 trifft nicht …/test_a10."""
    assert reaper.marker_for(tmp_path / "test_a1") == f"{tmp_path}/test_a1/".encode()


def test_kill_until_gone_ends_marked_processes_and_their_session(tmp_path: Path) -> None:
    leader = _guard_like(tmp_path / "test_a1")
    other = _guard_like(tmp_path / "test_a10")
    try:
        _wait_for_child(leader)
        _wait_for_child(other)
        marker = reaper.marker_for(tmp_path / "test_a1")
        assert reaper.kill_until_gone(marker, exclude=os.getpid())
        leader.wait(timeout=_TIMEOUT)
        # Auch das Kind ohne Pfad in der Befehlszeile ist weg.
        assert _wait_until(lambda: _session_members(leader.pid) == [])
        # Ein anderer Test-Ordner bleibt unberührt.
        assert other.poll() is None
        assert len(_session_members(other.pid)) == 2
    finally:
        reaper.kill_until_gone(reaper.marker_for(tmp_path), exclude=os.getpid())
        leader.wait(timeout=_TIMEOUT)
        other.wait(timeout=_TIMEOUT)


def test_reaper_cleans_up_after_pytest_was_killed(tmp_path: Path) -> None:
    """Statt pytest stirbt ein Platzhalter mit SIGKILL; danach räumt der Aufräumer seinen
    Ordner auf und endet selbst."""
    session = tmp_path / "session"
    placeholder = subprocess.Popen(["/bin/sleep", "300"], stdin=subprocess.DEVNULL)
    leader = _guard_like(session / "test_x0")
    try:
        _wait_for_child(leader)
        pidfd = os.pidfd_open(placeholder.pid)
        try:
            starter = subprocess.Popen(  # noqa: S603 - feste Argumente, nur im Test
                [sys.executable, "-I", reaper.__file__, str(pidfd), str(session)],
                pass_fds=(pidfd,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            os.close(pidfd)
        assert starter.wait(timeout=_TIMEOUT) == 0
        # Der Aufräumer ist das einzige weitere Mitglied der Sitzung des Starters.
        assert _wait_until(lambda: len(_session_members(starter.pid)) == 1)
        cleaner = _session_members(starter.pid)[0]
        time.sleep(0.5)
        assert _alive(leader.pid), "vor dem Ende des Platzhalters räumt er nichts auf"

        placeholder.kill()
        placeholder.wait(timeout=_TIMEOUT)

        leader.wait(timeout=_TIMEOUT)
        assert _wait_until(lambda: _session_members(leader.pid) == [])
        assert _wait_until(lambda: not _alive(cleaner)), "der Aufräumer endet selbst"
    finally:
        placeholder.kill()
        placeholder.wait(timeout=_TIMEOUT)
        reaper.kill_until_gone(reaper.marker_for(tmp_path), exclude=os.getpid())
        leader.wait(timeout=_TIMEOUT)


def test_reaper_of_this_session_is_waiting() -> None:
    """conftest.py hat für diese pytest-Sitzung einen Aufräumer gestartet."""
    me = str(os.getpid())
    found = [
        pid
        for pid, _, cmdline in reaper._processes()
        if reaper.__file__.encode() in cmdline and pid != os.getpid()
    ]
    assert found, "kein Aufräumer gefunden"
    # Er hält einen pidfd auf genau diesen pytest-Prozess.
    holders = []
    for pid in found:
        for fd in Path(f"/proc/{pid}/fdinfo").iterdir():
            try:
                info = fd.read_text()
            except OSError:
                continue
            if f"Pid:\t{me}\n" in info:
                holders.append(pid)
    assert holders
