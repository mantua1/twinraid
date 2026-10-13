"""Startpunkt des Web-Prozesses (twinraid-web)."""

import logging
import os
import sqlite3

import uvicorn

from twinraid.config import ConfigError, load_config
from twinraid.shared import runtime
from twinraid.shared.datadirs import DataPaths
from twinraid.shared.logs import configure_logging, warn_if_mock
from twinraid.shared.mode import RunModeError, resolve_run_mode
from twinraid.web.app import WebSettings, create_app
from twinraid.web.db import DatabaseError, WebDatabase
from twinraid.web.worker_client import WorkerClient

logger = logging.getLogger("twinraid.web")

EXIT_STARTUP_ERROR = 2
# Nur moderne Cipher für TLS 1.2 (ECDHE mit AES-GCM oder ChaCha20); TLS 1.3 ist davon
# unabhängig und immer sicher konfiguriert. TLS 1.0 und 1.1 sind in Python abgeschaltet.
TLS12_CIPHERS = "ECDHE+AESGCM:ECDHE+CHACHA20:!aNULL"


def main() -> int:
    # Der Web-Prozess legt nur Dateien an, die ausschließlich ihm gehören.
    os.umask(0o077)
    configure_logging("web")
    env = os.environ
    try:
        mode = resolve_run_mode(env)
    except RunModeError as exc:
        logger.error("%s", exc)
        return EXIT_STARTUP_ERROR
    warn_if_mock(logger, mode)

    try:
        config = load_config(runtime.config_path(env))
    except ConfigError as exc:
        logger.error("%s", exc.render())
        return EXIT_STARTUP_ERROR

    tls_dir = runtime.runtime_tls_dir(env)
    cert, key = tls_dir / "cert.pem", tls_dir / "key.pem"
    if not cert.is_file() or not key.is_file():
        logger.error("No TLS certificate in %s; twinraid-init must run first.", tls_dir)
        return EXIT_STARTUP_ERROR

    database = WebDatabase(DataPaths(runtime.data_dir(env)).web_database)
    try:
        database.initialize()
    except (DatabaseError, sqlite3.Error) as exc:
        logger.error("Web database is not usable: %s", exc)
        return EXIT_STARTUP_ERROR

    settings = WebSettings(
        mode=mode,
        config=config,
        frontend_dir=runtime.frontend_dir(env),
        runtime_tls_dir=tls_dir,
    )
    app = create_app(settings, WorkerClient(runtime.socket_path(env)), database)
    uvicorn.run(
        app,
        host=runtime.bind_host(env),
        port=config.web.port,
        interface="asgi3",
        ssl_certfile=str(cert),
        ssl_keyfile=str(key),
        ssl_ciphers=TLS12_CIPHERS,
        # X-Forwarded-For wird ignoriert (Konzept 5.1).
        proxy_headers=False,
        server_header=False,
        log_config=None,
    )
    return 0
