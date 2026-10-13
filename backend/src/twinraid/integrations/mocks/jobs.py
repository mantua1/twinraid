"""Simulierter Kopierjob: geschaltet mit twinraid-admin mock-job on|off (Konzept 5.5).

Der Schalter ist eine Datei neben dem Worker-Socket. Nur root kann sie
anlegen; der Web-Prozess kann den Schalter nicht umlegen.
"""

from pathlib import Path


class MockJobSwitch:
    def __init__(self, flag: Path) -> None:
        self._flag = flag

    def is_running(self) -> bool:
        return self._flag.exists()

    def set_running(self, running: bool) -> None:
        if running:
            self._flag.touch(mode=0o600)
        else:
            self._flag.unlink(missing_ok=True)
