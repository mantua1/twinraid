"""Rechte der Konfiguration (Konzept 6.1, „Rechte der Konfiguration“, ab v0.5.4).

twinraid.yaml und ihr Ordner sollen root gehören und nur für root beschreibbar sein. /config
ist ein Bind-Mount; lstat im Container liefert Modus und Besitzer der Inodes des Hosts, für den
Mount-Punkt und die Datei darin. Den Ordner darüber sieht der Container nicht (Grenze im Konzept).
"""

import os
import stat
from pathlib import Path

_WRITABLE_BY_OTHERS = stat.S_IWGRP | stat.S_IWOTH


def config_unprotected(path: Path, owner: int = 0) -> bool:
    """True, wenn die Datei oder ihr Ordner für Gruppe oder andere beschreibbar ist oder nicht
    `owner` gehört. Ein Symlink zählt als beschreibbar, weil lstat für ihn 0777 meldet. Was sich
    nicht prüfen lässt, ergibt keinen Hinweis; eine fehlende Datei bemerkt die Startprüfung."""
    for item in (path.parent, path):
        try:
            info = os.lstat(item)
        except OSError:
            continue
        if info.st_mode & _WRITABLE_BY_OTHERS or info.st_uid != owner:
            return True
    return False
