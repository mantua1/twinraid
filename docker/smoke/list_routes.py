"""Listet alle API-Routen der Web-Anwendung mit ihrer Öffentlichkeit (für den Starttest).

Läuft im Container mit dem Python der Anwendung ("docker exec ... python -c") und
baut die Anwendung mit einer temporären Datenbank in /tmp auf. Gibt JSON aus.
"""

import json
import tempfile
from pathlib import Path

from fastapi.routing import APIRoute

from twinraid.config import load_config
from twinraid.shared.mode import RunMode
from twinraid.web.app import WebSettings, create_app
from twinraid.web.auth import is_public_route
from twinraid.web.db import WebDatabase
from twinraid.web.worker_client import WorkerClient

with tempfile.TemporaryDirectory() as directory:
    database = WebDatabase(Path(directory) / "routes.sqlite")
    database.initialize()
    settings = WebSettings(
        mode=RunMode.MOCK,
        config=load_config(Path("/config/twinraid.yaml")),
        frontend_dir=Path("/app/frontend"),
        runtime_tls_dir=Path("/run/twinraid-tls"),
    )
    app = create_app(settings, WorkerClient(Path(directory) / "none.sock"), database)
    routes = [
        {"path": route.path, "methods": sorted(route.methods or ()), "public": is_public_route(route)}
        for route in app.api.routes
        if isinstance(route, APIRoute)
    ]
print(json.dumps(routes))
