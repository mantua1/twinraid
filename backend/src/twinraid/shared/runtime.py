"""Laufzeitpfade und -werte aus der Umgebung.

Die Standardwerte entsprechen dem Container. Für die lokale Entwicklung
setzen die Entwicklungs-Skripte eigene Werte. Diese Werte kommen nie aus der
Weboberfläche.
"""

from collections.abc import Mapping
from pathlib import Path

CONFIG_ENV_VAR = "TWINRAID_CONFIG"
DATA_DIR_ENV_VAR = "TWINRAID_DATA_DIR"
RUNTIME_TLS_DIR_ENV_VAR = "TWINRAID_RUNTIME_TLS_DIR"
CUSTOM_TLS_DIR_ENV_VAR = "TWINRAID_CUSTOM_TLS_DIR"
WEB_GID_ENV_VAR = "TWINRAID_WEB_GID"
SOCKET_ENV_VAR = "TWINRAID_WORKER_SOCKET"
WEB_UID_ENV_VAR = "TWINRAID_WEB_UID"
FRONTEND_DIR_ENV_VAR = "TWINRAID_FRONTEND_DIR"
BIND_HOST_ENV_VAR = "TWINRAID_BIND_HOST"

DEFAULT_CONFIG_PATH = Path("/config/twinraid.yaml")
DEFAULT_DATA_DIR = Path("/data")
DEFAULT_RUNTIME_TLS_DIR = Path("/run/twinraid-tls")
DEFAULT_CUSTOM_TLS_DIR = Path("/config/tls")
DEFAULT_SOCKET_PATH = Path("/run/twinraid/worker.sock")
DEFAULT_FRONTEND_DIR = Path("/app/frontend")
# Sicherer Standard für die lokale Entwicklung; der Container setzt 0.0.0.0.
DEFAULT_BIND_HOST = "127.0.0.1"


class RuntimeSettingError(Exception):
    """Ein Laufzeitwert aus der Umgebung fehlt oder ist ungültig."""


def config_path(env: Mapping[str, str]) -> Path:
    return Path(env.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH)


def socket_path(env: Mapping[str, str]) -> Path:
    return Path(env.get(SOCKET_ENV_VAR) or DEFAULT_SOCKET_PATH)


def frontend_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get(FRONTEND_DIR_ENV_VAR) or DEFAULT_FRONTEND_DIR)


def bind_host(env: Mapping[str, str]) -> str:
    return env.get(BIND_HOST_ENV_VAR) or DEFAULT_BIND_HOST


def data_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get(DATA_DIR_ENV_VAR) or DEFAULT_DATA_DIR)


def runtime_tls_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get(RUNTIME_TLS_DIR_ENV_VAR) or DEFAULT_RUNTIME_TLS_DIR)


def custom_tls_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get(CUSTOM_TLS_DIR_ENV_VAR) or DEFAULT_CUSTOM_TLS_DIR)


def _positive_id(env: Mapping[str, str], name: str) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        raise RuntimeSettingError(f"{name} is not set.")
    if not (raw.isascii() and raw.isdigit()):
        raise RuntimeSettingError(f"{name} must be a positive integer.")
    value = int(raw)
    if value == 0:
        # root als Web-Prozess würde die Prozesstrennung aufheben.
        raise RuntimeSettingError(f"{name} must not be 0 (root).")
    return value


def web_uid(env: Mapping[str, str]) -> int:
    """UID des Web-Prozesses; nur sie darf den Worker-Socket benutzen. Pflichtwert."""
    return _positive_id(env, WEB_UID_ENV_VAR)


def web_gid(env: Mapping[str, str]) -> int:
    """GID des Web-Prozesses; ihr gehören Socket, Web-Daten und TLS-Kopie. Pflichtwert."""
    return _positive_id(env, WEB_GID_ENV_VAR)
