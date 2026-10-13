"""Tests für das sichere Laden der YAML-Datei."""

import os
from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from twinraid.config import ConfigError, load_config
from twinraid.config.loader import MAX_CONFIG_BYTES


def _write(tmp_path: Path, content: str | bytes) -> Path:
    path = tmp_path / "config.yaml"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _problems(path: Path) -> str:
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    return "\n".join(excinfo.value.problems)


def test_example_config_is_valid() -> None:
    config = load_config(EXAMPLE_CONFIG)
    assert config.sync.shares == ["appdata", "dokumente", "medien"]
    assert config.servers.target.mac == "AA:BB:CC:DD:EE:FF"
    assert config.web.default_language == "de"


def test_missing_file(tmp_path: Path) -> None:
    assert "file not found" in _problems(tmp_path / "missing.yaml")


def test_directory_instead_of_file(tmp_path: Path) -> None:
    assert "is a directory" in _problems(tmp_path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "web: {}\n")
    path.chmod(0)
    assert "not readable" in _problems(path)


@pytest.mark.parametrize("content", ["", "# nur ein Kommentar\n"])
def test_empty_file(tmp_path: Path, content: str) -> None:
    assert "file is empty" in _problems(_write(tmp_path, content))


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    assert "top level must be a mapping" in _problems(_write(tmp_path, "- a\n- b\n"))


def test_yaml_syntax_error(tmp_path: Path) -> None:
    assert "YAML error" in _problems(_write(tmp_path, "web: [unclosed\n"))


def test_python_tags_are_rejected(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    content = f"web: !!python/object/apply:pathlib.Path.touch ['{marker}']\n"
    assert "YAML error" in _problems(_write(tmp_path, content))
    assert not marker.exists()


def test_duplicate_keys_are_rejected(tmp_path: Path) -> None:
    content = EXAMPLE_CONFIG.read_text(encoding="utf-8") + "\nweb:\n  port: 1\n"
    assert "duplicate key 'web'" in _problems(_write(tmp_path, content))


def test_anchors_and_aliases_are_rejected(tmp_path: Path) -> None:
    content = "a: &x [1, 2]\nb: *x\n"
    assert "anchors/aliases are not supported" in _problems(_write(tmp_path, content))


def test_file_too_large(tmp_path: Path) -> None:
    path = _write(tmp_path, "#" * (MAX_CONFIG_BYTES + 1))
    assert "larger than" in _problems(path)


def test_invalid_utf8(tmp_path: Path) -> None:
    assert "not valid UTF-8" in _problems(_write(tmp_path, b"web: \xff\xfe\n"))


def test_render_lists_every_problem(tmp_path: Path) -> None:
    path = _write(tmp_path, "web: {}\nservers: {}\ngaming_vm: {}\nsync: {}\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    rendered = excinfo.value.render()
    assert rendered.startswith(f"Invalid configuration in {path}:")
    assert "web.allowed_networks: required key is missing" in rendered
    assert "sync.shares: required key is missing" in rendered
