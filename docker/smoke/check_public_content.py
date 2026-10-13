"""Durchsucht alle öffentlich ausgelieferten Dateien nach Konfigurationswerten (Konzept 5.3).

Läuft im Container mit dem Python der Anwendung ("docker exec ... python -c").
Grundlage ist die gemountete Konfiguration; für einen aussagekräftigen Test
muss sie eindeutige Testwerte enthalten (docker/smoke/canary-config.yaml).
Gibt das Ergebnis als JSON aus.
"""

import json
import urllib.request
from pathlib import Path

from twinraid.config import load_config
from twinraid.config.sensitive import sensitive_values

BASE_URL = "http://127.0.0.1:8443"
FRONTEND_DIR = Path("/app/frontend")


def fetch(path: str) -> str:
    with urllib.request.urlopen(BASE_URL + path, timeout=5) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


config = load_config(Path("/config/twinraid.yaml"))
needles = sorted(value.lower() for value in sensitive_values(config))

# Jede Datei der Oberfläche über ihre öffentliche URL abrufen, dazu /health.
paths = ["/health"]
for file in sorted(FRONTEND_DIR.rglob("*")):
    if file.is_file():
        relative = file.relative_to(FRONTEND_DIR).as_posix()
        paths.append("/" if relative == "index.html" else f"/{relative}")

leaks = []
for path in paths:
    body = fetch(path).lower()
    leaks.extend(f"{path}: {needle}" for needle in needles if needle in body)

# Gegenprobe: Über die geschützte API kommen die Namen sehr wohl an.
api_info = fetch("/api/info").lower()
print(
    json.dumps(
        {
            "checked": paths,
            "needles": len(needles),
            "leaks": leaks,
            "api_delivers_names": config.servers.source.name.lower() in api_info,
        }
    )
)
