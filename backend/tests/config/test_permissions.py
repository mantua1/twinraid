"""Rechte der Konfiguration (Konzept 6.1, „Rechte der Konfiguration“, ab v0.5.4)."""

import os
from pathlib import Path

import pytest

from twinraid.config.permissions import config_unprotected

# In den Tests gehören Datei und Ordner dem eigenen Benutzer; er steht für root im Container.
ME = os.getuid()


def _config(tmp_path: Path, file_mode: int = 0o644, dir_mode: int = 0o755) -> Path:
    folder = tmp_path / "config"
    folder.mkdir()
    path = folder / "twinraid.yaml"
    path.write_text("web: {}\n", encoding="utf-8")
    path.chmod(file_mode)
    folder.chmod(dir_mode)
    return path


def test_protected_file_and_folder(tmp_path: Path) -> None:
    assert config_unprotected(_config(tmp_path), owner=ME) is False


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o444, 0o400, 0o755])
def test_file_modes_without_write_for_others_are_fine(tmp_path: Path, mode: int) -> None:
    assert config_unprotected(_config(tmp_path, file_mode=mode), owner=ME) is False


@pytest.mark.parametrize("mode", [0o664, 0o646, 0o666, 0o662, 0o777])
def test_file_writable_for_group_or_others(tmp_path: Path, mode: int) -> None:
    assert config_unprotected(_config(tmp_path, file_mode=mode), owner=ME) is True


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777, 0o1777])
def test_folder_writable_for_group_or_others(tmp_path: Path, mode: int) -> None:
    assert config_unprotected(_config(tmp_path, dir_mode=mode), owner=ME) is True


@pytest.mark.parametrize("mode", [0o700, 0o750, 0o755, 0o711])
def test_folder_modes_without_write_for_others_are_fine(tmp_path: Path, mode: int) -> None:
    assert config_unprotected(_config(tmp_path, dir_mode=mode), owner=ME) is False


def test_other_owner_is_unprotected(tmp_path: Path) -> None:
    # Unraids „New Permissions“ setzt nobody:users (99); hier: ein anderer Besitzer als erwartet.
    assert config_unprotected(_config(tmp_path), owner=ME + 1) is True


def test_default_owner_is_root(tmp_path: Path) -> None:
    path = _config(tmp_path)
    assert config_unprotected(path) is (ME != 0)


def test_symlink_counts_as_writable(tmp_path: Path) -> None:
    path = _config(tmp_path)
    real = path.with_name("echt.yaml")
    path.rename(real)
    path.symlink_to(real)
    assert config_unprotected(path, owner=ME) is True


def test_missing_file_gives_no_notice(tmp_path: Path) -> None:
    folder = tmp_path / "config"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    assert config_unprotected(folder / "twinraid.yaml", owner=ME) is False


def test_missing_file_in_open_folder_still_reports_the_folder(tmp_path: Path) -> None:
    folder = tmp_path / "config"
    folder.mkdir()
    folder.chmod(0o777)
    assert config_unprotected(folder / "twinraid.yaml", owner=ME) is True
