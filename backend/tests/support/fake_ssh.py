"""Nachgebautes ssh bzw. ssh-keyscan für Tests: merkt sich Aufrufe, antwortet nach Vorgabe.

Das Programm ist ein kleines Python-Skript in einem Testordner. Es liest seine
Antwort aus behavior.json im selben Ordner und hängt jeden Aufruf an calls.jsonl an.
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path

_SCRIPT = """#!{python}
import json, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
with (here / "calls.jsonl").open("a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
behavior = json.loads((here / "behavior.json").read_text())
# Je Key eine eigene Antwort: Name der Datei hinter -i, z. B. ssh-status.
args = sys.argv[1:]
if "-i" in args:
    key = Path(args[args.index("-i") + 1]).name
    behavior = {{**behavior, **behavior.get("by_key", {{}}).get(key, {{}})}}
time.sleep(behavior.get("sleep", 0))
sys.stdout.write(behavior.get("stdout", ""))
sys.stderr.write(behavior.get("stderr", ""))
sys.exit(behavior.get("exit", 0))
"""


@dataclass
class FakeSsh:
    directory: Path

    @classmethod
    def install(cls, directory: Path, name: str = "ssh") -> FakeSsh:
        directory.mkdir(parents=True, exist_ok=True)
        fake = cls(directory)
        fake.binary_path(name).write_text(_SCRIPT.format(python=sys.executable))
        fake.binary_path(name).chmod(0o755)
        fake.behave()
        return fake

    def binary_path(self, name: str = "ssh") -> Path:
        return self.directory / name

    @property
    def binary(self) -> Path:
        return self.binary_path()

    def behave(
        self,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        sleep: float = 0,
        by_key: dict[str, dict[str, object]] | None = None,
    ) -> None:
        """by_key: abweichende Antwort je Key-Datei, z. B. {"ssh-status": {"stdout": …}}."""
        behavior = {
            "exit": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "sleep": sleep,
            "by_key": by_key or {},
        }
        (self.directory / "behavior.json").write_text(json.dumps(behavior))

    def calls(self) -> list[list[str]]:
        path = self.directory / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


# Streamt Zeilen wie vm-watch. Jeder Aufruf nimmt den nächsten Eintrag aus runs.json
# (der letzte gilt für alle weiteren): {"lines": [[Pause, Zeile], …], "hold": s,
# "stderr": …, "exit": Code}.
_WATCH_SCRIPT = """#!{python}
import json, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
calls = here / "calls.jsonl"
with calls.open("a") as handle:
    handle.write(json.dumps([time.monotonic(), *sys.argv[1:]]) + "\\n")
count = len(calls.read_text().splitlines())
runs = json.loads((here / "runs.json").read_text())
run = runs[min(count, len(runs)) - 1]
for delay, line in run.get("lines", []):
    time.sleep(delay)
    sys.stdout.write(line + "\\n")
    sys.stdout.flush()
time.sleep(run.get("hold", 0))
sys.stderr.write(run.get("stderr", ""))
sys.exit(run.get("exit", 0))
"""


@dataclass
class FakeWatchSsh:
    directory: Path

    @classmethod
    def install(cls, directory: Path) -> FakeWatchSsh:
        directory.mkdir(parents=True, exist_ok=True)
        fake = cls(directory)
        fake.binary.write_text(_WATCH_SCRIPT.format(python=sys.executable))
        fake.binary.chmod(0o755)
        fake.runs({"hold": 60})
        return fake

    @property
    def binary(self) -> Path:
        return self.directory / "ssh"

    def runs(self, *runs: dict[str, object]) -> None:
        (self.directory / "runs.json").write_text(json.dumps(list(runs)))

    def calls(self) -> list[list[str]]:
        """Argumente je Aufruf (ohne Zeitstempel)."""
        return [call[1:] for call in self._raw()]

    def call_times(self) -> list[float]:
        return [float(call[0]) for call in self._raw()]

    def _raw(self) -> list[list[str]]:
        path = self.directory / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]
