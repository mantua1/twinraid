"""Admin-CLI im Entwicklungsmodus (gleicher Benutzer für Datenablage und Web-Prozess).

Der Weg als root mit Kindprozess als Web-Benutzer wird im Container-Starttest geprüft.
"""

import getpass
import io
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.conftest import EXAMPLE_CONFIG
from tests.support.fake_ssh import FakeSsh
from twinraid import admin_cli
from twinraid.init import main as init_main
from twinraid.integrations.ssh import (
    GuardClient,
    GuardKey,
    HostKey,
    SshTarget,
    key_fingerprint,
    parse_host_key,
)
from twinraid.shared.actions import ActionName, Trigger
from twinraid.shared.passwords import PasswordStore, VerifyOutcome
from twinraid.shared.tls import certificate_info, read_runtime
from twinraid.worker.action_log import ActionLog

GOOD_PASSWORD = "ein-gutes-neues-passwort"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    monkeypatch.setenv("TWINRAID_CONFIG", str(EXAMPLE_CONFIG))
    monkeypatch.setenv("TWINRAID_DATA_DIR", str(data))
    monkeypatch.setenv("TWINRAID_RUNTIME_TLS_DIR", str(tmp_path / "run-tls"))
    monkeypatch.setenv("TWINRAID_CUSTOM_TLS_DIR", str(tmp_path / "custom"))
    monkeypatch.setenv("TWINRAID_WEB_UID", str(os.getuid()))
    monkeypatch.setenv("TWINRAID_WEB_GID", str(os.getgid()))
    # Kein s6 in Tests.
    monkeypatch.setattr(admin_cli, "S6_SVC", tmp_path / "no-s6")
    assert init_main([]) == 0
    return tmp_path


def _db(env: Path) -> sqlite3.Connection:
    return sqlite3.connect(env / "data" / "web" / "web.sqlite")


def _set_password(monkeypatch: pytest.MonkeyPatch, password: str) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(password + "\n"))
    return admin_cli.main(["set-password", "--stdin"])


def test_set_password_from_stdin(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _set_password(monkeypatch, GOOD_PASSWORD) == 0
    store = PasswordStore(env / "data" / "secrets" / "password.argon2")
    assert store.verify(GOOD_PASSWORD) is VerifyOutcome.VALID
    out = capsys.readouterr()
    assert "All existing sessions have been ended" in out.out
    assert GOOD_PASSWORD not in out.out + out.err


def test_set_password_interactive(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([GOOD_PASSWORD, GOOD_PASSWORD])
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: next(answers))
    assert admin_cli.main(["set-password"]) == 0


def test_set_password_mismatch(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter([GOOD_PASSWORD, GOOD_PASSWORD + "x"])
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: next(answers))
    assert admin_cli.main(["set-password"]) == 1
    assert "do not match" in capsys.readouterr().err
    assert not (env / "data" / "secrets" / "password.argon2").exists()


@pytest.mark.parametrize("password", ["zu-kurz", "", "x" * 1025])
def test_set_password_policy(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], password: str
) -> None:
    assert _set_password(monkeypatch, password) == 1
    assert "the password must" in capsys.readouterr().err


def test_set_password_ends_all_sessions(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _set_password(monkeypatch, GOOD_PASSWORD) == 0
    with _db(env) as db:
        db.execute("INSERT INTO sessions VALUES ('a', 'x', 9e9, 9e9)")
        db.execute("INSERT INTO sessions VALUES ('b', 'y', 9e9, 9e9)")
    assert _set_password(monkeypatch, GOOD_PASSWORD + "2") == 0
    with _db(env) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        actions = [r[0] for r in db.execute("SELECT action FROM audit_log")]
    assert actions == ["cli_set_password", "cli_set_password"]


def test_unlock_clears_locks(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _set_password(monkeypatch, GOOD_PASSWORD) == 0
    with _db(env) as db:
        db.execute("INSERT INTO lockouts VALUES ('192.168.20.5', 9e9)")
        db.execute("INSERT INTO login_failures VALUES ('192.168.20.6', 9e9)")
    assert admin_cli.main(["unlock"]) == 0
    with _db(env) as db:
        assert db.execute("SELECT COUNT(*) FROM lockouts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM login_failures").fetchone()[0] == 0
        assert db.execute("SELECT action FROM audit_log ORDER BY id DESC").fetchone() == (
            "cli_unlock",
        )


def test_unlock_signals_worker_via_s6(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    s6 = env / "s6-svc"
    s6.write_text("")
    (env / "service" / "twinraid-worker").mkdir(parents=True)
    monkeypatch.setattr(admin_cli, "S6_SVC", s6)
    monkeypatch.setattr(admin_cli, "SERVICE_DIR", env / "service")

    def fake_run(args: list[str], check: bool) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert admin_cli.main(["unlock"]) == 0
    assert calls == [[str(s6), "-1", str(env / "service" / "twinraid-worker")]]


def _fingerprints(env: Path) -> tuple[str, str]:
    state = read_runtime(env / "run-tls")
    assert state.ca is not None
    return certificate_info(state.ca).fingerprint, certificate_info(state.leaf).fingerprint


def test_renew_cert_keeps_the_ca(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ca_before, server_before = _fingerprints(env)
    assert admin_cli.main(["renew-cert"]) == 0
    ca_after, server_after = _fingerprints(env)
    assert ca_after == ca_before
    assert server_after != server_before
    out = capsys.readouterr().out
    assert f"SHA-256 fingerprint: {server_after}" in out
    assert "Devices that trust the CA need no change" in out
    with _db(env) as db:
        assert db.execute("SELECT action FROM audit_log").fetchall() == [("cli_renew_cert",)]


def test_renew_cert_with_new_ca(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ca_before, server_before = _fingerprints(env)
    assert admin_cli.main(["renew-cert", "--new-ca"]) == 0
    ca_after, server_after = _fingerprints(env)
    assert ca_after != ca_before
    assert server_after != server_before
    assert "Install the new CA certificate on all devices" in capsys.readouterr().out


def test_show_cert_fingerprint(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ca, server = _fingerprints(env)
    assert admin_cli.main(["show-cert-fingerprint"]) == 0
    out = capsys.readouterr().out
    assert f"SHA-256 fingerprint: {ca}" in out
    assert f"SHA-256 fingerprint: {server}" in out
    assert "SHA-1 fingerprint (shown by Windows)" in out
    assert "Server certificate: TwinRaid CA" in out
    assert "192.168.10.20" in out


def test_requires_owner_of_data_dir(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    assert admin_cli.main(["unlock"]) == 1
    assert "Run this command as root inside the container" in capsys.readouterr().err


def test_requires_prepared_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("TWINRAID_CONFIG", str(EXAMPLE_CONFIG))
    monkeypatch.setenv("TWINRAID_DATA_DIR", str(data))
    monkeypatch.setenv("TWINRAID_WEB_UID", str(os.getuid()))
    monkeypatch.setenv("TWINRAID_WEB_GID", str(os.getgid()))
    assert admin_cli.main(["unlock"]) == 1
    assert "start the container once" in capsys.readouterr().err


# --- API-Key von Groß bis v0.4.2 (Konzept 3.5 und 5.5) -------------------------------


@pytest.mark.parametrize("command", ["set-api-key", "trust-api-cert"])
def test_api_commands_are_gone(env: Path, command: str) -> None:
    """Seit v0.4.3 (E22) braucht TwinRaid für Groß keinen API-Key mehr."""
    with pytest.raises(SystemExit):
        admin_cli.main([command, "target"])


# --- Simulierter Kopierjob ----------------------------------------------------------------


def test_mock_job_switch(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    socket_dir = env / "run"
    socket_dir.mkdir()
    monkeypatch.setenv("TWINRAID_WORKER_SOCKET", str(socket_dir / "worker.sock"))
    assert admin_cli.main(["mock-job", "on"]) == 0
    assert (socket_dir / "mock-job-running").exists()
    assert admin_cli.main(["mock-job", "off"]) == 0
    assert not (socket_dir / "mock-job-running").exists()


def test_mock_job_only_in_simulation(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TWINRAID_MODE", "real")
    assert admin_cli.main(["mock-job", "on"]) == 1
    assert "only works in simulation mode" in capsys.readouterr().err


# --- SSH-Keys „status“ und „power“, Hostkey von Groß (Konzept 5.5 und 5.6) -------------

SECRETS = Path("data") / "secrets"


def _line(env: Path, key: str) -> str:
    public = (env / SECRETS / f"ssh-{key}.pub").read_text().strip()
    command = f"/bin/bash /boot/config/twinraid/guard.sh {key}"
    return f'restrict,from="192.168.10.20",command="{command}" {public}'


def _real_config(env: Path, monkeypatch: pytest.MonkeyPatch, **actions: bool) -> None:
    """Modus real mit der Beispielkonfiguration und den gewünschten Freischaltungen."""
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    for name, on in actions.items():
        old = f"  {name}: false"
        assert old in text
        text = text.replace(old, f"  {name}: {'true' if on else 'false'}")
    config = env / "real.yaml"
    config.write_text(text, encoding="utf-8")
    monkeypatch.setenv("TWINRAID_CONFIG", str(config))
    monkeypatch.setenv("TWINRAID_MODE", "real")


def test_init_ssh_keys_in_simulation_creates_both_keys(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admin_cli.main(["init-ssh-keys"]) == 0
    for name in ("ssh-status", "ssh-status.pub", "ssh-power", "ssh-power.pub"):
        assert (env / SECRETS / name).stat().st_mode & 0o777 == 0o600
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert _line(env, "status") in lines
    assert _line(env, "power") in lines
    assert (env / SECRETS / "ssh-status.pub").read_text().strip().endswith(" twinraid-status")
    assert (env / SECRETS / "ssh-power.pub").read_text().strip().endswith(" twinraid-power")
    assert "PRIVATE KEY" not in out
    assert "power.conf" in out
    # Ein zweiter Aufruf gibt dieselben Zeilen aus und erzeugt keine neuen Keys.
    assert admin_cli.main(["init-ssh-keys"]) == 0
    again = capsys.readouterr().out
    assert again.count("already exists") == 2
    assert _line(env, "status") in again.splitlines()
    with _db(env) as db:
        actions = [r[0] for r in db.execute("SELECT action FROM audit_log")]
    assert actions.count("cli_init_ssh_keys") == 2


def test_init_ssh_keys_read_only_needs_only_status(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _real_config(env, monkeypatch, wake=True)
    assert admin_cli.main(["init-ssh-keys"]) == 0
    assert (env / SECRETS / "ssh-status").exists()
    assert not (env / SECRETS / "ssh-power").exists()
    out = capsys.readouterr().out
    assert _line(env, "status") in out.splitlines()
    assert "guard.sh power" not in out
    assert "The key 'power' is only needed" in out


@pytest.mark.parametrize("action", ["vm", "shutdown"])
def test_init_ssh_keys_with_actions_needs_power(
    env: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    _real_config(env, monkeypatch, **{action: True})
    assert admin_cli.main(["init-ssh-keys"]) == 0
    assert (env / SECRETS / "ssh-status").exists()
    assert (env / SECRETS / "ssh-power").exists()


def test_init_ssh_keys_for_one_key(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert admin_cli.main(["init-ssh-keys", "--key", "power"]) == 0
    assert (env / SECRETS / "ssh-power").exists()
    assert not (env / SECRETS / "ssh-status").exists()
    lines = capsys.readouterr().out.splitlines()
    assert _line(env, "power") in lines
    assert not any("guard.sh status" in line for line in lines)


def test_init_ssh_keys_new_replaces_only_the_named_key(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admin_cli.main(["init-ssh-keys"]) == 0
    status = (env / SECRETS / "ssh-status.pub").read_text()
    power = (env / SECRETS / "ssh-power.pub").read_text()
    assert admin_cli.main(["init-ssh-keys", "--key", "status", "--new"]) == 0
    assert (env / SECRETS / "ssh-status.pub").read_text() != status
    assert (env / SECRETS / "ssh-power.pub").read_text() == power
    assert "The old key 'status' no longer works" in capsys.readouterr().err


def test_init_ssh_keys_new_needs_a_key_name(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert admin_cli.main(["init-ssh-keys"]) == 0
    before = (env / SECRETS / "ssh-power.pub").read_text()
    assert admin_cli.main(["init-ssh-keys", "--new"]) == 1
    assert "name it with --key" in capsys.readouterr().err
    assert (env / SECRETS / "ssh-power.pub").read_text() == before


def test_init_ssh_keys_rejects_unknown_keys(env: Path) -> None:
    with pytest.raises(SystemExit):
        admin_cli.main(["init-ssh-keys", "--key", "sync"])


class FakeGuard:
    """Ersetzt ssh-keyscan und ssh für trust-host-key."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.ssh = FakeSsh.install(tmp_path / "fake-ssh")
        self.answer()
        public = Ed25519PrivateKey.generate().public_key()
        self.key = public.public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode().split()[1]
        self.fingerprint = key_fingerprint(self.key)
        self.scanned: list[tuple[str, int]] = []

        async def scan(host: str, port: int) -> HostKey:
            self.scanned.append((host, port))
            return parse_host_key(f"{host} ssh-ed25519 {self.key}\n", host, port)

        def client(context: admin_cli.Context, key: GuardKey) -> GuardClient:
            return GuardClient(SshTarget.of(context.config, context.paths, key), self.ssh.binary)

        monkeypatch.setattr(admin_cli, "scan_host_key", scan)
        monkeypatch.setattr(admin_cli, "guard_client", client)

    def answer(self, status: str = "2 status", power: str = "2 power") -> None:
        """Antwort von guard.sh auf "check" je Key."""
        self.ssh.behave(
            by_key={
                "ssh-status": {"stdout": f"twinraid-guard {status} ok\n"},
                "ssh-power": {"stdout": f"twinraid-guard {power} ok\n"},
            }
        )

    def keys_used(self) -> list[str]:
        return [Path(call[call.index("-i") + 1]).name for call in self.ssh.calls()]


@pytest.fixture
def guard(env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGuard:
    monkeypatch.setenv("TWINRAID_MODE", "real")
    return FakeGuard(tmp_path, monkeypatch)


def test_trust_host_key_needs_real_mode(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert admin_cli.main(["trust-host-key", "target"]) == 1
    assert "needs TWINRAID_MODE=real" in capsys.readouterr().err


def test_trust_host_key_and_test_the_guard(
    env: Path, guard: FakeGuard, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admin_cli.main(["init-ssh-keys"]) == 0
    assert admin_cli.main(["trust-host-key", "target", "--fingerprint", guard.fingerprint]) == 0
    known = env / SECRETS / "target-ssh-known-hosts"
    assert known.read_text() == f"192.168.10.11 ssh-ed25519 {guard.key}\n"
    assert known.stat().st_mode & 0o777 == 0o600
    assert guard.scanned == [("192.168.10.11", 22)]
    out = capsys.readouterr().out
    assert guard.fingerprint in out
    assert "OK: guard.sh answers (twinraid-guard 2 status ok)" in out
    # Nur der Key „status“ existiert (Nur Statusanzeige); der Test schickt nur "check".
    assert guard.keys_used() == ["ssh-status"]
    assert [call[-1] for call in guard.ssh.calls()] == ["check"]


def test_trust_host_key_tests_both_keys(
    env: Path, guard: FakeGuard, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admin_cli.main(["init-ssh-keys", "--key", "status"]) == 0
    assert admin_cli.main(["init-ssh-keys", "--key", "power"]) == 0
    assert admin_cli.main(["trust-host-key", "target", "--fingerprint", guard.fingerprint]) == 0
    out = capsys.readouterr().out
    assert "OK: guard.sh answers (twinraid-guard 2 status ok)" in out
    assert "OK: guard.sh answers (twinraid-guard 2 power ok)" in out
    assert sorted(guard.keys_used()) == ["ssh-power", "ssh-status"]


def test_trust_host_key_interactive(
    env: Path, guard: FakeGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert admin_cli.main(["trust-host-key", "target"]) == 0
    assert (env / SECRETS / "target-ssh-known-hosts").exists()


@pytest.mark.parametrize("answer", ["no", "", "ja"])
def test_trust_host_key_needs_yes(
    env: Path, guard: FakeGuard, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    assert admin_cli.main(["trust-host-key", "target"]) == 1
    assert not (env / SECRETS / "target-ssh-known-hosts").exists()


def test_trust_host_key_rejects_a_wrong_fingerprint(env: Path, guard: FakeGuard) -> None:
    assert admin_cli.main(["trust-host-key", "target", "--fingerprint", "SHA256:falsch"]) == 1
    assert not (env / SECRETS / "target-ssh-known-hosts").exists()


@pytest.mark.parametrize(
    ("exit_code", "stdout", "stderr", "message"),
    [
        (0, "", "", "probably lacks command="),
        (
            127,
            "",
            "/bin/bash: /boot/config/twinraid/guard.sh: No such file",
            'command="/bin/bash /boot/config/twinraid/guard.sh status"',
        ),
        (255, "", "root@x: Permission denied (publickey).", "init-ssh-keys --key status"),
        (255, "", "ssh: connect to host x port 22: Connection refused", "Use SSH"),
        (2, "", "twinraid-guard: denied\n", "version 1 does not know the mode 'status'"),
    ],
)
def test_guard_test_explains_problems(
    env: Path,
    guard: FakeGuard,
    capsys: pytest.CaptureFixture[str],
    exit_code: int,
    stdout: str,
    stderr: str,
    message: str,
) -> None:
    assert admin_cli.main(["init-ssh-keys"]) == 0
    guard.ssh.behave(exit_code=exit_code, stdout=stdout, stderr=stderr)
    assert admin_cli.main(["trust-host-key", "target", "--fingerprint", guard.fingerprint]) == 1
    assert message in capsys.readouterr().err


def test_guard_test_finds_an_outdated_guard(
    env: Path, guard: FakeGuard, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admin_cli.main(["init-ssh-keys", "--key", "power"]) == 0
    guard.answer(power="1 power")
    assert admin_cli.main(["trust-host-key", "target", "--fingerprint", guard.fingerprint]) == 1
    assert "guard.sh on Unraid Groß is version 1; TwinRaid needs version 2" in (
        capsys.readouterr().err
    )


def test_guard_test_finds_swapped_lines(
    env: Path, guard: FakeGuard, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admin_cli.main(["init-ssh-keys"]) == 0
    guard.answer(status="2 power")
    assert admin_cli.main(["trust-host-key", "target", "--fingerprint", guard.fingerprint]) == 1
    assert "The line for the key 'status' runs guard.sh in mode 'power'" in (
        capsys.readouterr().err
    )


class FakeS6:
    """Merkt sich die Signale an den Worker (s6-svc)."""

    def __init__(self, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[list[str]] = []
        self.binary = env / "s6-svc"
        self.binary.write_text("")
        self.service = env / "service" / "twinraid-worker"
        self.service.mkdir(parents=True)
        monkeypatch.setattr(admin_cli, "S6_SVC", self.binary)
        monkeypatch.setattr(admin_cli, "SERVICE_DIR", env / "service")
        real_run = subprocess.run

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if args and args[0] == str(self.binary):
                self.calls.append(args)
                return subprocess.CompletedProcess(args, 0)
            return real_run(args, **kwargs)  # type: ignore[call-overload,no-any-return]

        monkeypatch.setattr(subprocess, "run", fake_run)

    def signals(self) -> list[str]:
        return [call[1] for call in self.calls]


def test_trust_host_key_ends_the_wait_of_the_status_connection(
    env: Path, guard: FakeGuard, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bestandener Test mit dem Key „status“: SIGUSR2 an den Worker (Konzept 3.5, ab v0.4.4)."""
    s6 = FakeS6(env, monkeypatch)
    assert admin_cli.main(["init-ssh-keys"]) == 0
    assert admin_cli.main(["trust-host-key", "target", "--fingerprint", guard.fingerprint]) == 0
    assert s6.calls == [[str(s6.binary), "-2", str(s6.service)]]
    assert "TwinRaid connects with the key 'status' right away." in capsys.readouterr().out
    # Auch wenn der Hostkey schon bestätigt war: Die Zeile auf Groß kann neu sein.
    assert admin_cli.main(["trust-host-key", "target", "--fingerprint", guard.fingerprint]) == 0
    out = capsys.readouterr().out
    assert "This host key was already confirmed." in out
    assert "right away" in out
    assert s6.signals() == ["-2", "-2"]


@pytest.mark.parametrize(
    ("stderr", "exit_code"),
    [
        ("root@x: Permission denied (publickey).", 255),
        ("twinraid-guard: denied\n", 2),
    ],
)
def test_no_signal_when_the_status_test_fails(
    env: Path,
    guard: FakeGuard,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stderr: str,
    exit_code: int,
) -> None:
    s6 = FakeS6(env, monkeypatch)
    assert admin_cli.main(["init-ssh-keys"]) == 0
    guard.ssh.behave(exit_code=exit_code, stderr=stderr)
    assert admin_cli.main(["trust-host-key", "target", "--fingerprint", guard.fingerprint]) == 1
    assert s6.calls == []
    assert "right away" not in capsys.readouterr().out


def test_no_signal_for_an_outdated_guard_on_the_status_key(
    env: Path, guard: FakeGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    s6 = FakeS6(env, monkeypatch)
    assert admin_cli.main(["init-ssh-keys"]) == 0
    guard.answer(status="1 status")
    assert admin_cli.main(["trust-host-key", "target", "--fingerprint", guard.fingerprint]) == 1
    assert s6.calls == []


def test_a_failed_power_test_still_ends_the_wait(
    env: Path, guard: FakeGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Die Statusverbindung braucht nur den Key „status“."""
    s6 = FakeS6(env, monkeypatch)
    assert admin_cli.main(["init-ssh-keys", "--key", "status"]) == 0
    assert admin_cli.main(["init-ssh-keys", "--key", "power"]) == 0
    guard.answer(power="1 power")
    assert admin_cli.main(["trust-host-key", "target", "--fingerprint", guard.fingerprint]) == 1
    assert s6.signals() == ["-2"]


def test_no_signal_without_the_key_status(
    env: Path, guard: FakeGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    s6 = FakeS6(env, monkeypatch)
    assert admin_cli.main(["init-ssh-keys", "--key", "power"]) == 0
    assert admin_cli.main(["trust-host-key", "target", "--fingerprint", guard.fingerprint]) == 0
    assert s6.calls == []


def test_without_s6_the_cli_explains_the_restart(
    env: Path, guard: FakeGuard, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admin_cli.main(["init-ssh-keys"]) == 0
    assert admin_cli.main(["trust-host-key", "target", "--fingerprint", guard.fingerprint]) == 0
    assert "restart it to connect right away" in capsys.readouterr().err


def test_trust_host_key_without_keys(
    env: Path, guard: FakeGuard, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admin_cli.main(["trust-host-key", "target", "--fingerprint", guard.fingerprint]) == 0
    assert "twinraid-admin init-ssh-keys" in capsys.readouterr().err
    assert guard.ssh.calls() == []


# --- Aktionslog und Simulation (Konzept 5.3 und 6.4) -----------------------------------


def test_show_action_log(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert admin_cli.main(["show-action-log"]) == 0
    assert "No actions recorded yet." in capsys.readouterr().out
    log = ActionLog(env / "data" / "worker" / "actions.jsonl")
    log.record("0a1b2c3d", ActionName.VM_START, Trigger.DASHBOARD, "started")
    log.record(
        "0a1b2c3d", ActionName.WAKE_TARGET, Trigger.AUTOMATIC, "success", ActionName.VM_START
    )
    # Ergebnisse aus v0.4.2 und früher bleiben lesbar.
    log.record("0a1b2c3d", ActionName.VM_START, Trigger.DASHBOARD, "api_forbidden")
    log.record("1a1b2c3d", ActionName.VM_START, Trigger.DASHBOARD, "vm_not_configured")
    assert admin_cli.main(["show-action-log", "--lines", "3"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert lines[0].endswith("0a1b2c3d  wake_target  automatic (part of vm_start): success")
    assert lines[1].endswith("0a1b2c3d  vm_start  dashboard: api_forbidden")
    assert lines[2].endswith("1a1b2c3d  vm_start  dashboard: vm_not_configured")


def test_mock_tour_and_faults(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    socket_dir = env / "run"
    socket_dir.mkdir()
    monkeypatch.setenv("TWINRAID_WORKER_SOCKET", str(socket_dir / "worker.sock"))
    assert admin_cli.main(["mock-tour", "on"]) == 0
    assert (socket_dir / "mock-tour").exists()
    assert admin_cli.main(["mock-tour", "off"]) == 0
    assert not (socket_dir / "mock-tour").exists()
    assert admin_cli.main(["mock-fault", "vm-hang", "on"]) == 0
    assert (socket_dir / "mock-fault-vm-hang").stat().st_mode & 0o777 == 0o600
    assert admin_cli.main(["mock-fault", "vm-hang", "off"]) == 0
    assert not (socket_dir / "mock-fault-vm-hang").exists()
    for fault in ("vm-not-configured", "libvirt-down", "guard-outdated", "connect-plugin"):
        assert admin_cli.main(["mock-fault", fault, "on"]) == 0
        assert (socket_dir / f"mock-fault-{fault}").exists()
    for removed in ("../etc", "api-forbidden"):
        with pytest.raises(SystemExit):
            admin_cli.main(["mock-fault", removed, "on"])


@pytest.mark.parametrize("command", [["mock-tour", "on"], ["mock-fault", "no-boot", "on"]])
def test_simulation_switches_only_in_mock_mode(
    env: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    monkeypatch.setenv("TWINRAID_MODE", "real")
    assert admin_cli.main(command) == 1
