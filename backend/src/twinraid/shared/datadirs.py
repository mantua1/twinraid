"""Datenablage unter /data: anlegen, prüfen, korrigieren (Konzept 5.9).

Läuft im Container als root mit GID 10001 und ohne CHOWN, FOWNER oder
DAC_OVERRIDE. Korrigieren lassen sich deshalb nur Rechte und Gruppe von
Einträgen, die root gehören. Alles andere führt zum Abbruch mit Meldung.
"""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

REPAIR_HINT = (
    "On the Unraid host run: chown root:root <data dir> && chmod 755 <data dir> "
    "(the data dir is usually /mnt/user/appdata/twinraid/data)."
)


class DataDirError(Exception):
    """Die Datenablage ist unsicher oder unbrauchbar."""


@dataclass(frozen=True)
class DataPaths:
    root: Path

    @property
    def secrets(self) -> Path:
        return self.root / "secrets"

    @property
    def tls(self) -> Path:
        return self.root / "tls"

    @property
    def web(self) -> Path:
        return self.root / "web"

    @property
    def worker(self) -> Path:
        """Aktionslog des Workers; der Web-Prozess kann es weder lesen noch ändern (Konzept 5.3)."""
        return self.root / "worker"

    @property
    def action_log(self) -> Path:
        return self.worker / "actions.jsonl"

    @property
    def password_file(self) -> Path:
        return self.secrets / "password.argon2"

    @property
    def target_api_key(self) -> Path:
        """API-Key von Unraid Groß (Konzept 3.5 und 5.5)."""
        return self.secrets / "target-api-key"

    @property
    def target_api_cert(self) -> Path:
        """Bestätigter SHA-256-Fingerabdruck des Zertifikats von Unraid Groß."""
        return self.secrets / "target-api-cert.sha256"

    @property
    def ssh_power_key(self) -> Path:
        """Privater SSH-Key „power“ für Herunterfahren und VM-Shutdown (Konzept 5.6)."""
        return self.secrets / "ssh-power"

    @property
    def ssh_power_public_key(self) -> Path:
        return self.secrets / "ssh-power.pub"

    @property
    def target_known_hosts(self) -> Path:
        """Bestätigter SSH-Hostkey von Unraid Groß (twinraid-admin trust-host-key target)."""
        return self.secrets / "target-ssh-known-hosts"

    @property
    def tls_ca_cert(self) -> Path:
        return self.tls / "ca.pem"

    @property
    def tls_ca_key(self) -> Path:
        return self.tls / "ca-key.pem"

    @property
    def tls_ca_change(self) -> Path:
        """Vermerk des letzten CA-Wechsels für den Hinweis im Dashboard (Konzept 5.2)."""
        return self.tls / "ca-change.json"

    @property
    def tls_server_cert(self) -> Path:
        return self.tls / "server.pem"

    @property
    def tls_server_key(self) -> Path:
        return self.tls / "server-key.pem"

    @property
    def legacy_tls_cert(self) -> Path:
        """Selbstsigniertes Zertifikat aus Phase 2; wird beim Übergang gelöscht."""
        return self.tls / "cert.pem"

    @property
    def legacy_tls_key(self) -> Path:
        return self.tls / "key.pem"

    @property
    def web_database(self) -> Path:
        return self.web / "web.sqlite"

    @property
    def tls_renewal_marker(self) -> Path:
        """Vermerk des Web-Prozesses, wann er sich zuletzt für eine Erneuerung beendet hat."""
        return self.web / "tls-renewal.json"


def check_data_root(root: Path, owner_uid: int) -> None:
    try:
        info = root.lstat()
    except FileNotFoundError:
        raise DataDirError(f"{root} does not exist; mount a volume there.") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise DataDirError(f"{root} must be a directory, not a link or file.")
    if info.st_uid != owner_uid:
        raise DataDirError(
            f"{root} is owned by uid {info.st_uid}, expected uid {owner_uid} (root). {REPAIR_HINT}"
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022:
        raise DataDirError(
            f"{root} is writable by group or others (mode {mode:04o}); only root may write "
            f"there. {REPAIR_HINT}"
        )
    if not os.access(root, os.W_OK):
        raise DataDirError(f"{root} is not writable; mount a writable volume at {root}.")


def _ensure_dir(path: Path, mode: int, owner_uid: int, group: int | None) -> list[str]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=mode)
        path.chmod(mode)
        if group is not None:
            os.chown(path, -1, group)
        return [f"created {path}"]
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise DataDirError(f"{path} must be a directory, not a link or file.")
    if info.st_uid != owner_uid:
        raise DataDirError(
            f"{path} is owned by uid {info.st_uid}, expected uid {owner_uid}. "
            "Remove it on the host or give it back to root (chown root)."
        )
    corrections: list[str] = []
    if group is not None and info.st_gid != group:
        try:
            os.chown(path, -1, group)
        except PermissionError:
            raise DataDirError(f"cannot set group {group} on {path}") from None
        corrections.append(f"set group {group} on {path}")
    if stat.S_IMODE(info.st_mode) != mode:
        path.chmod(mode)
        corrections.append(f"set mode {mode:04o} on {path} (was {stat.S_IMODE(info.st_mode):04o})")
    return corrections


def _ensure_private_files(directory: Path, owner_uid: int) -> list[str]:
    """Dateien in secrets/, tls/ und worker/: reguläre Dateien von root mit 0600."""
    corrections: list[str] = []
    for entry in sorted(directory.iterdir()):
        info = entry.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise DataDirError(f"{entry} must be a regular file, not a link or directory.")
        if info.st_uid != owner_uid:
            raise DataDirError(f"{entry} is owned by uid {info.st_uid}, expected uid {owner_uid}.")
        if stat.S_IMODE(info.st_mode) != 0o600:
            entry.chmod(0o600)
            corrections.append(f"set mode 0600 on {entry}")
    return corrections


def prepare_data_dirs(paths: DataPaths, owner_uid: int, web_gid: int) -> list[str]:
    """Legt die Unterordner an, prüft und korrigiert sie. Gibt die Korrekturen zurück."""
    check_data_root(paths.root, owner_uid)
    corrections: list[str] = []
    corrections += _ensure_dir(paths.secrets, 0o700, owner_uid, None)
    corrections += _ensure_dir(paths.tls, 0o700, owner_uid, None)
    corrections += _ensure_dir(paths.web, 0o770, owner_uid, web_gid)
    corrections += _ensure_dir(paths.worker, 0o700, owner_uid, None)
    corrections += _ensure_private_files(paths.secrets, owner_uid)
    corrections += _ensure_private_files(paths.tls, owner_uid)
    corrections += _ensure_private_files(paths.worker, owner_uid)
    return corrections
