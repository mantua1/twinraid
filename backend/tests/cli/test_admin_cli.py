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

from tests.conftest import EXAMPLE_CONFIG
from twinraid import admin_cli
from twinraid.init import main as init_main
from twinraid.shared.passwords import PasswordStore, VerifyOutcome
from twinraid.shared.tls import certificate_info, read_runtime

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
    assert init_main() == 0
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


def test_renew_cert(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = certificate_info(read_runtime(env / "run-tls")[0]).fingerprint
    assert admin_cli.main(["renew-cert"]) == 0
    after = certificate_info(read_runtime(env / "run-tls")[0]).fingerprint
    assert before != after
    out = capsys.readouterr().out
    assert f"SHA-256 fingerprint: {after}" in out
    with _db(env) as db:
        assert db.execute("SELECT action FROM audit_log").fetchall() == [("cli_renew_cert",)]


def test_show_cert_fingerprint(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    expected = certificate_info(read_runtime(env / "run-tls")[0]).fingerprint
    assert admin_cli.main(["show-cert-fingerprint"]) == 0
    out = capsys.readouterr().out
    assert f"SHA-256 fingerprint: {expected}" in out
    assert "Active certificate: self-signed" in out
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
