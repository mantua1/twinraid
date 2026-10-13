"""Admin-CLI im Entwicklungsmodus (gleicher Benutzer für Datenablage und Web-Prozess).

Der Weg als root mit Kindprozess als Web-Benutzer wird im Container-Starttest geprüft.
"""

import getpass
import io
import os
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from tests.support.fake_unraid import FakeUnraid, Reply, graphql_reply
from twinraid import admin_cli
from twinraid.init import main as init_main
from twinraid.integrations.unraid_api import ApiEndpoint
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


# --- API-Key und Zertifikat von Unraid Groß (Konzept 3.5 und 5.5) ---------------------

API_KEY = "Test-Key_0123456789abcdef"


@pytest.fixture
def fake_target(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[FakeUnraid]:
    """Leitet alle Verbindungen zu Groß auf einen lokalen Ersatz um."""
    encrypted = bool(getattr(request, "param", False))
    server = FakeUnraid(tmp_path, tls=encrypted)
    server.reply = graphql_reply([{"name": "Windows 11 Gaming", "state": "SHUTOFF"}])

    def local(cls: type[ApiEndpoint], _server: object) -> ApiEndpoint:
        return cls("127.0.0.1", server.port, encrypted)

    monkeypatch.setattr(ApiEndpoint, "of", classmethod(local))
    try:
        yield server
    finally:
        server.close()


def _https_config(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = env / "https.yaml"
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    marker = "    host: 192.168.10.11\n    webgui:\n      scheme: http\n      port: 80\n"
    assert marker in text
    config.write_text(
        text.replace(marker, "    host: 192.168.10.11\n    webgui:\n      scheme: https\n"),
        encoding="utf-8",
    )
    monkeypatch.setenv("TWINRAID_CONFIG", str(config))


def _set_key(monkeypatch: pytest.MonkeyPatch, key: str = API_KEY) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(key + "\n"))
    return admin_cli.main(["set-api-key", "target", "--stdin"])


def test_set_api_key_in_mock_mode_is_not_tested(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _set_key(monkeypatch) == 0
    key_file = env / "data" / "secrets" / "target-api-key"
    assert key_file.read_text() == API_KEY + "\n"
    assert key_file.stat().st_mode & 0o777 == 0o600
    out = capsys.readouterr()
    assert "not tested" in out.out
    assert "unencrypted" in out.err
    assert API_KEY not in out.out + out.err
    with _db(env) as db:
        assert db.execute("SELECT action FROM audit_log").fetchall() == [("cli_set_api_key",)]


def test_set_api_key_interactive_is_hidden(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: API_KEY)
    assert admin_cli.main(["set-api-key", "target"]) == 0


@pytest.mark.parametrize("key", ["", "mit leerzeichen", "a;rm -rf /"])
def test_set_api_key_rejects_invalid_keys(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], key: str
) -> None:
    assert _set_key(monkeypatch, key) == 1
    assert "Nothing was saved" in capsys.readouterr().err
    assert not (env / "data" / "secrets" / "target-api-key").exists()


def test_set_api_key_in_real_mode_tests_the_key(
    env: Path,
    fake_target: FakeUnraid,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TWINRAID_MODE", "real")
    assert _set_key(monkeypatch) == 0
    assert "OK: the API key works. VM 'Windows 11 Gaming' is SHUTOFF." in capsys.readouterr().out
    assert fake_target.received[0].headers["x-api-key"] == API_KEY


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (Reply(status=401), "Unraid rejected the API key"),
        (Reply(status=403), "VMS: Read (any)"),
        (Reply(status=302, headers={"Location": "https://x/"}), "Unraid redirects to HTTPS"),
        (graphql_reply([{"name": "Andere", "state": "RUNNING"}]), "VMs found: Andere"),
    ],
)
def test_set_api_key_explains_problems(
    env: Path,
    fake_target: FakeUnraid,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reply: Reply,
    message: str,
) -> None:
    monkeypatch.setenv("TWINRAID_MODE", "real")
    fake_target.reply = reply
    assert _set_key(monkeypatch) == 1
    assert message in capsys.readouterr().err
    # Der Key bleibt gespeichert; die Diagnose sagt nur, was nicht passt.
    assert (env / "data" / "secrets" / "target-api-key").exists()


@pytest.mark.parametrize("fake_target", [True], indirect=True)
def test_trust_api_cert_with_expected_fingerprint(
    env: Path,
    fake_target: FakeUnraid,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TWINRAID_MODE", "real")
    _https_config(env, monkeypatch)
    assert fake_target.certificate_fingerprint is not None
    assert _set_key(monkeypatch) == 0
    assert "trust-api-cert target" in capsys.readouterr().err
    assert fake_target.received == []
    args = ["trust-api-cert", "target", "--fingerprint", fake_target.certificate_fingerprint]
    assert admin_cli.main(args) == 0
    out = capsys.readouterr().out
    assert "Certificate confirmed" in out
    assert "OK: the API key works" in out
    pin = (env / "data" / "secrets" / "target-api-cert.sha256").read_text().strip()
    assert pin == fake_target.certificate_fingerprint
    with _db(env) as db:
        actions = [r[0] for r in db.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions == ["cli_set_api_key", "cli_trust_api_cert"]


@pytest.mark.parametrize("fake_target", [True], indirect=True)
def test_trust_api_cert_interactive(
    env: Path, fake_target: FakeUnraid, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWINRAID_MODE", "real")
    _https_config(env, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert admin_cli.main(["trust-api-cert", "target"]) == 0
    assert (env / "data" / "secrets" / "target-api-cert.sha256").exists()


@pytest.mark.parametrize("fake_target", [True], indirect=True)
@pytest.mark.parametrize("answer", ["", "no", "ja"])
def test_trust_api_cert_needs_explicit_yes(
    env: Path, fake_target: FakeUnraid, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    monkeypatch.setenv("TWINRAID_MODE", "real")
    _https_config(env, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    assert admin_cli.main(["trust-api-cert", "target"]) == 1
    assert not (env / "data" / "secrets" / "target-api-cert.sha256").exists()


@pytest.mark.parametrize("fake_target", [True], indirect=True)
def test_trust_api_cert_rejects_wrong_expected_fingerprint(
    env: Path,
    fake_target: FakeUnraid,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TWINRAID_MODE", "real")
    _https_config(env, monkeypatch)
    assert admin_cli.main(["trust-api-cert", "target", "--fingerprint", "AB" * 32]) == 1
    assert "does not match" in capsys.readouterr().err
    assert not (env / "data" / "secrets" / "target-api-cert.sha256").exists()


def test_trust_api_cert_needs_https(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TWINRAID_MODE", "real")
    assert admin_cli.main(["trust-api-cert", "target"]) == 1
    assert "webgui.scheme of the target is http" in capsys.readouterr().err


def test_trust_api_cert_needs_real_mode(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert admin_cli.main(["trust-api-cert", "target"]) == 1
    assert "needs TWINRAID_MODE=real" in capsys.readouterr().err


def test_only_the_target_key_exists_in_this_phase(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        admin_cli.main(["set-api-key", "source"])


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
