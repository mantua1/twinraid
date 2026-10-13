"""Laufzeitpfade und -werte aus der Umgebung.

Die Standardwerte entsprechen dem Container. Für die lokale Entwicklung
setzen die Entwicklungs-Skripte eigene Werte. Diese Werte kommen nie aus der
Weboberfläche.
"""

from collections.abc import Mapping
from pathlib import Path

CONFIG_ENV_VAR = "TWINRAID_CONFIG"
SOCKET_ENV_VAR = "TWINRAID_WORKER_SOCKET"
WEB_UID_ENV_VAR = "TWINRAID_WEB_UID"
FRONTEND_DIR_ENV_VAR = "TWINRAID_FRONTEND_DIR"
BIND_HOST_ENV_VAR = "TWINRAID_BIND_HOST"

DEFAULT_CONFIG_PATH = Path("/config/twinraid.yaml")
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


def web_uid(env: Mapping[str, str]) -> int:
    """UID des Web-Prozesses; nur sie darf den Worker-Socket benutzen. Pflichtwert."""
    raw = env.get(WEB_UID_ENV_VAR)
    if raw is None or raw == "":
        raise RuntimeSettingError(f"{WEB_UID_ENV_VAR} is not set.")
    if not (raw.isascii() and raw.isdigit()):
        raise RuntimeSettingError(f"{WEB_UID_ENV_VAR} must be a positive integer.")
    uid = int(raw)
    if uid == 0:
        # root als Web-Prozess würde die Prozesstrennung aufheben.
        raise RuntimeSettingError(f"{WEB_UID_ENV_VAR} must not be 0 (root).")
    return uid
