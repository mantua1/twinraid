"""Laufzeitwerte aus der Umgebung."""

from pathlib import Path

import pytest

from twinraid.shared.runtime import (
    DEFAULT_BIND_HOST,
    DEFAULT_CONFIG_PATH,
    DEFAULT_SOCKET_PATH,
    RuntimeSettingError,
    bind_host,
    config_path,
    socket_path,
    web_uid,
)


def test_container_defaults() -> None:
    assert config_path({}) == DEFAULT_CONFIG_PATH
    assert socket_path({}) == DEFAULT_SOCKET_PATH
    assert bind_host({}) == DEFAULT_BIND_HOST == "127.0.0.1"


def test_overrides() -> None:
    assert config_path({"TWINRAID_CONFIG": "/x/y.yaml"}) == Path("/x/y.yaml")
    assert socket_path({"TWINRAID_WORKER_SOCKET": "/x/w.sock"}) == Path("/x/w.sock")


def test_web_uid_is_required() -> None:
    with pytest.raises(RuntimeSettingError, match="not set"):
        web_uid({})


@pytest.mark.parametrize("value", ["abc", "-1", "1.5", "²", " 10001"])
def test_web_uid_must_be_a_number(value: str) -> None:
    with pytest.raises(RuntimeSettingError, match="positive integer"):
        web_uid({"TWINRAID_WEB_UID": value})


def test_web_uid_must_not_be_root() -> None:
    with pytest.raises(RuntimeSettingError, match="must not be 0"):
        web_uid({"TWINRAID_WEB_UID": "0"})


def test_web_uid_valid() -> None:
    assert web_uid({"TWINRAID_WEB_UID": "10001"}) == 10001
