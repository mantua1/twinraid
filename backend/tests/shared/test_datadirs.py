"""Datenablage unter /data: Anlegen, Prüfen und Korrigieren (Konzept 5.9)."""

import os
import stat
from pathlib import Path

import pytest

from twinraid.shared.datadirs import DataDirError, DataPaths, prepare_data_dirs

UID = os.getuid()
GID = os.getgid()


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    root = tmp_path / "data"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    return DataPaths(root)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_creates_directories_with_modes(paths: DataPaths) -> None:
    prepare_data_dirs(paths, UID, GID)
    assert _mode(paths.secrets) == 0o700
    assert _mode(paths.tls) == 0o700
    assert _mode(paths.web) == 0o770
    assert paths.web.stat().st_gid == GID
    # Aktionslog des Workers: nur root (Konzept 5.3 und 5.9).
    assert _mode(paths.worker) == 0o700
    assert paths.action_log.parent == paths.worker


def test_is_idempotent(paths: DataPaths) -> None:
    prepare_data_dirs(paths, UID, GID)
    assert prepare_data_dirs(paths, UID, GID) == []


def test_corrects_modes(paths: DataPaths) -> None:
    prepare_data_dirs(paths, UID, GID)
    paths.secrets.chmod(0o755)
    paths.web.chmod(0o777)
    (paths.secrets / "password.argon2").write_text("x")
    (paths.secrets / "password.argon2").chmod(0o644)
    paths.action_log.write_text("{}\n")
    paths.action_log.chmod(0o644)
    corrections = prepare_data_dirs(paths, UID, GID)
    assert len(corrections) == 4
    assert _mode(paths.action_log) == 0o600
    assert _mode(paths.secrets) == 0o700
    assert _mode(paths.web) == 0o770
    assert _mode(paths.secrets / "password.argon2") == 0o600


@pytest.mark.parametrize("mode", [0o777, 0o775, 0o757, 0o1777])
def test_rejects_root_writable_by_others(paths: DataPaths, mode: int) -> None:
    paths.root.chmod(mode)
    with pytest.raises(DataDirError, match="writable by group or others") as excinfo:
        prepare_data_dirs(paths, UID, GID)
    assert "chown root:root" in str(excinfo.value)
    assert "chmod 755" in str(excinfo.value)


def test_rejects_foreign_owner_of_root(paths: DataPaths) -> None:
    with pytest.raises(DataDirError, match="expected uid"):
        prepare_data_dirs(paths, UID + 1, GID)


def test_rejects_read_only_root(paths: DataPaths) -> None:
    paths.root.chmod(0o555)
    try:
        with pytest.raises(DataDirError, match="not writable"):
            prepare_data_dirs(paths, UID, GID)
    finally:
        paths.root.chmod(0o755)


def test_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(DataDirError, match="does not exist"):
        prepare_data_dirs(DataPaths(tmp_path / "missing"), UID, GID)


def test_rejects_symlinked_root(tmp_path: Path, paths: DataPaths) -> None:
    link = tmp_path / "link"
    link.symlink_to(paths.root)
    with pytest.raises(DataDirError, match="not a link"):
        prepare_data_dirs(DataPaths(link), UID, GID)


def test_rejects_symlinked_subdirectory(tmp_path: Path, paths: DataPaths) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    paths.secrets.symlink_to(elsewhere)
    with pytest.raises(DataDirError, match="not a link"):
        prepare_data_dirs(paths, UID, GID)


def test_rejects_symlinked_secret_file(tmp_path: Path, paths: DataPaths) -> None:
    prepare_data_dirs(paths, UID, GID)
    target = tmp_path / "target"
    target.write_text("x")
    (paths.secrets / "password.argon2").symlink_to(target)
    with pytest.raises(DataDirError, match="regular file"):
        prepare_data_dirs(paths, UID, GID)
