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
        self, exit_code: int = 0, stdout: str = "", stderr: str = "", sleep: float = 0
    ) -> None:
        behavior = {"exit": exit_code, "stdout": stdout, "stderr": stderr, "sleep": sleep}
        (self.directory / "behavior.json").write_text(json.dumps(behavior))

    def calls(self) -> list[list[str]]:
        path = self.directory / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]
