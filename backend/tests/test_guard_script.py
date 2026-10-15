"""host-scripts/guard.sh: Nur feste Anfragen sind erlaubt (Konzept 5.6).

Die Tests laufen mit einer Kopie des Skripts, in der poweroff, virsh und logger
durch Platzhalter ersetzt sind, die ihre Aufrufe mitschreiben.
"""

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

GUARD = REPO_ROOT / "host-scripts" / "guard.sh"
UUID = "0b5c4e1a-2f3d-4c5b-8a9e-112233445566"
# Platzhalter: schreibt Name und Argumente als JSON-Liste in die Aufrufdatei.
_FAKE = """#!{python}
import json, sys
from pathlib import Path
with open({calls!r}, "a") as handle:
    handle.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + "\\n")
"""


@dataclass
class Guard:
    script: Path
    calls_file: Path

    def run(self, mode: str | None, request: str | None) -> subprocess.CompletedProcess[str]:
        env = {"PATH": "/usr/bin:/bin"}
        if request is not None:
            env["SSH_ORIGINAL_COMMAND"] = request
        arguments = ["/bin/bash", str(self.script)]
        if mode is not None:
            arguments.append(mode)
        return subprocess.run(  # noqa: S603 - feste Argumente, nur im Test
            arguments, env=env, capture_output=True, text=True, check=False, timeout=10
        )

    def calls(self) -> list[list[str]]:
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text().splitlines()]

    def commands(self) -> list[list[str]]:
        """Aufrufe außer logger."""
        return [call for call in self.calls() if call[0] != "logger"]


@pytest.fixture
def guard(tmp_path: Path) -> Guard:
    calls = tmp_path / "calls.jsonl"
    fakes = {}
    for name in ("poweroff", "virsh", "logger"):
        fake = tmp_path / name
        fake.write_text(_FAKE.format(python=sys.executable, calls=str(calls)))
        fake.chmod(0o755)
        fakes[name] = fake
    text = GUARD.read_text()
    for name, real in (
        ("POWEROFF", "/sbin/poweroff"),
        ("VIRSH", "/usr/bin/virsh"),
        ("LOGGER", "/usr/bin/logger"),
    ):
        line = f"readonly {name}={real}\n"
        assert line in text
        text = text.replace(line, f"readonly {name}={fakes[real.rsplit('/', 1)[1]]}\n")
    script = tmp_path / "guard.sh"
    script.write_text(text)
    return Guard(script, calls)


def test_check_does_nothing(guard: Guard) -> None:
    result = guard.run("power", "check")
    assert result.returncode == 0
    assert result.stdout == "twinraid-guard 1 power ok\n"
    assert guard.commands() == []


def test_poweroff(guard: Guard) -> None:
    result = guard.run("power", "poweroff")
    assert result.returncode == 0
    assert guard.commands() == [["poweroff"]]
    assert [
        "logger",
        "-t",
        "twinraid-guard",
        "--",
        "poweroff requested by TwinRaid",
    ] in guard.calls()


def test_vm_shutdown_sends_only_acpi(guard: Guard) -> None:
    result = guard.run("power", f"vm-shutdown {UUID}")
    assert result.returncode == 0
    assert guard.commands() == [["virsh", "shutdown", "--domain", UUID]]


@pytest.mark.parametrize(
    "request_text",
    [
        "",
        "Check",
        "check ",
        " poweroff",
        "poweroff now",
        "poweroff; rm -rf /",
        "check && poweroff",
        "reboot",
        "rm -rf /",
        "vm-shutdown",
        "vm-shutdown ",
        "vm-shutdown Windows 11 Gaming",
        f"vm-shutdown {UUID.upper()}",
        f"vm-shutdown {UUID} extra",
        f"vm-shutdown {UUID};poweroff",
        f"vm-shutdown {UUID}\npoweroff",
        f"vm-shutdown --domain {UUID}",
        "vm-shutdown $(poweroff)",
        "vm-destroy " + UUID,
        "rsync --server -logDtpre.iLsfxC . /mnt/user/appdata",
    ],
)
def test_everything_else_is_denied(guard: Guard, request_text: str) -> None:
    result = guard.run("power", request_text)
    assert result.returncode == 2
    assert result.stderr == "twinraid-guard: denied\n"
    assert guard.commands() == []
    # Das Syslog bekommt nur feste Texte, nie die Anfrage selbst.
    for call in guard.calls():
        if request_text.strip():
            assert request_text not in " ".join(call)


def test_missing_request_is_denied(guard: Guard) -> None:
    assert guard.run("power", None).returncode == 2
    assert guard.commands() == []


@pytest.mark.parametrize("mode", ["sync", "", "Power", "power extra", None])
def test_other_modes_are_denied(guard: Guard, mode: str | None) -> None:
    for request_text in ("check", "poweroff", f"vm-shutdown {UUID}"):
        assert guard.run(mode, request_text).returncode == 2
    assert guard.commands() == []


def test_repository_script_uses_absolute_paths() -> None:
    text = GUARD.read_text()
    assert text.startswith("#!/bin/bash\n")
    assert "set -euo pipefail" in text
    assert "eval" not in text
    assert "readonly POWEROFF=/sbin/poweroff\n" in text
    assert "readonly VIRSH=/usr/bin/virsh\n" in text
