"""Startdienst twinraid-init."""

import os
from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from twinraid.init import main
from twinraid.shared.tls import certificate_info, read_runtime


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    monkeypatch.setenv("TWINRAID_CONFIG", str(EXAMPLE_CONFIG))
    monkeypatch.setenv("TWINRAID_DATA_DIR", str(data))
    monkeypatch.setenv("TWINRAID_RUNTIME_TLS_DIR", str(tmp_path / "run-tls"))
    monkeypatch.setenv("TWINRAID_CUSTOM_TLS_DIR", str(tmp_path / "custom"))
    monkeypatch.setenv("TWINRAID_WEB_GID", str(os.getgid()))
    return tmp_path


def test_init_sets_up_tls_and_logs_fingerprint(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main() == 0
    err = capsys.readouterr().err
    cert, source = read_runtime(env / "run-tls")
    assert source == "self-signed"
    assert f"SHA-256 fingerprint: {certificate_info(cert).fingerprint}" in err
    assert "No password is set" in err


def test_init_refuses_unsafe_data_dir(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (env / "data").chmod(0o777)
    assert main() == 1
    err = capsys.readouterr().err
    assert "writable by group or others" in err
    assert "will not start" in err


def test_init_requires_web_gid(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TWINRAID_WEB_GID")
    assert main() == 1
