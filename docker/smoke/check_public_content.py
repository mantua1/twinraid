"""Liefert die öffentlichen Pfade der Oberfläche und die schützenswerten Werte (Konzept 5.3).

Läuft im Container mit dem Python der Anwendung ("docker exec ... python -c").
Der Starttest ruft die Pfade danach von außen ab und durchsucht sie nach den
Werten. Für einen aussagekräftigen Test muss die gemountete Konfiguration
eindeutige Testwerte enthalten (docker/smoke/canary-config.yaml).
"""

import json
from pathlib import Path

from twinraid.config import load_config
from twinraid.config.sensitive import sensitive_values

FRONTEND_DIR = Path("/app/frontend")

config = load_config(Path("/config/twinraid.yaml"))
paths = ["/health", "/api/auth/state"]
for file in sorted(FRONTEND_DIR.rglob("*")):
    if file.is_file():
        relative = file.relative_to(FRONTEND_DIR).as_posix()
        paths.append("/" if relative == "index.html" else f"/{relative}")
print(
    json.dumps(
        {
            "paths": paths,
            "needles": sorted(value.lower() for value in sensitive_values(config)),
            "source_name": config.servers.source.name,
        }
    )
)
