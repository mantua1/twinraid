"""Prozesse aus Tests sicher beenden, auch wenn pytest selbst abgebrochen wird.

Wächter (source-guard.sh) und guard.sh laufen in den Tests mit setsid in einer eigenen
Sitzung. Endet pytest, ohne die Fixtures abzubauen (Zeitlimit, Strg+C, kill -9), liefen sie
sonst weiter; so lief einmal ein Wächter aus einem Test 19 Tage lang.

Erkannt werden die Prozesse an ihrer Befehlszeile: Sie enthält den Pfad des Test-Ordners.
Dazu kommen alle Prozesse einer Sitzung, die ein solcher Prozess leitet (etwa sleep oder
timeout des Wächters, deren Befehlszeile den Pfad nicht enthält).

Das Modul läuft auch als eigenes Skript (Aufräumer je pytest-Sitzung, siehe conftest.py) und
verwendet deshalb nur die Standardbibliothek.
"""

import contextlib
import os
import select
import signal
import sys
import time
from collections.abc import Iterator
from pathlib import Path

_PROC = Path("/proc")


def marker_for(directory: Path) -> bytes:
    """Merkmal in der Befehlszeile: der Ordner mit abschließendem Schrägstrich, damit
    …/test_a1 nicht auch …/test_a10 trifft."""
    return f"{directory}/".encode()


def _processes() -> Iterator[tuple[int, int, bytes]]:
    """(PID, Sitzung, Befehlszeile) aller Prozesse außer Zombies."""
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # Nach dem Namen in Klammern: Zustand, PPID, PGID, Sitzung, …
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            cmdline = (entry / "cmdline").read_bytes()
        except OSError, IndexError:
            continue
        if fields[0] == "Z":
            continue
        yield int(entry.name), int(fields[3]), cmdline


def kill_marked(marker: bytes, exclude: int) -> int:
    """Ein Durchgang mit SIGKILL; gibt die Zahl der getroffenen Prozesse zurück."""
    processes = list(_processes())
    marked = {pid for pid, _, cmdline in processes if marker in cmdline}
    # Ganze Sitzungen nur, wenn ein markierter Prozess sie leitet (setsid im Test); nie die
    # Sitzung von pytest oder des Terminals.
    targets = marked | {pid for pid, session, _ in processes if session in marked}
    targets.discard(exclude)
    for pid in targets:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    return len(targets)


def kill_until_gone(marker: bytes, exclude: int, timeout: float = 5.0) -> bool:
    """Wiederholt, bis ein Durchgang nichts mehr findet: Eine Überwachungsschleife kann
    zwischen zwei Blicken in /proc noch einen neuen Prozess starten."""
    deadline = time.monotonic() + timeout
    while kill_marked(marker, exclude):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def main(argv: list[str]) -> int:
    """Aufräumer: wartet auf das Ende von pytest, räumt auf und endet selbst.

    argv: Deskriptor (pidfd) des pytest-Prozesses und der Ordner der Sitzung. Ein pidfd statt
    einer PID, weil eine PID nach dem Ende von pytest an einen anderen Prozess gehen kann.
    """
    pidfd, directory = int(argv[1]), Path(argv[2])
    # Vom Starter lösen: Er wartet nur auf diesen Prozess, der Enkel wartet auf pytest.
    if os.fork():
        return 0
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    while not poller.poll():
        pass
    os.close(pidfd)
    return 0 if kill_until_gone(marker_for(directory), exclude=os.getpid()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
