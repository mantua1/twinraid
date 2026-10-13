"""Startdienst twinraid-init: Datenablage prüfen und TLS einrichten (Konzept 5.2, 5.4, 5.9).

Läuft im Container nach der Startprüfung als root mit GID 10001. Schlägt er
fehl, startet der Container nicht.
"""

import logging
import os
from datetime import UTC, datetime

from twinraid.config import ConfigError, load_config
from twinraid.shared import runtime
from twinraid.shared.datadirs import DataDirError, DataPaths, prepare_data_dirs
from twinraid.shared.logs import configure_logging
from twinraid.shared.mode import RunModeError, resolve_run_mode
from twinraid.shared.tls import ActiveCertificate, TlsError, activate, uncovered_hosts

logger = logging.getLogger("twinraid.init")

_NOT_STARTING = "TwinRaid will not start. Fix the problem above and restart the container."


def log_certificate(active: ActiveCertificate, now: datetime) -> None:
    info = active.info
    if active.generated_because is not None:
        logger.warning(
            "Generated a new self-signed TLS certificate because %s. Browsers and devices "
            "that trusted the old certificate must confirm the new one.",
            active.generated_because,
        )
    logger.info("TLS certificate (%s) SHA-256 fingerprint: %s", active.source, info.fingerprint)
    logger.info("Compare this fingerprint with the certificate shown by your browser.")
    addresses = ", ".join(sorted(info.ip_addresses | info.hostnames)) or "none"
    logger.info(
        "Certificate valid until %s (%d days left), addresses: %s",
        info.not_after.date().isoformat(),
        info.days_left(now),
        addresses,
    )
    if info.expires_soon(now):
        logger.warning("The TLS certificate expires in %d days.", info.days_left(now))


def main() -> int:
    # Neue Dateien höchstens für die Gruppe lesbar; Rechte werden ohnehin explizit gesetzt.
    os.umask(0o027)
    configure_logging("init")
    env = os.environ
    now = datetime.now(UTC)
    try:
        resolve_run_mode(env)
        config = load_config(runtime.config_path(env))
        web_gid = runtime.web_gid(env)
        paths = DataPaths(runtime.data_dir(env))
        for correction in prepare_data_dirs(paths, owner_uid=os.geteuid(), web_gid=web_gid):
            logger.info("Data directory: %s", correction)
        active = activate(
            paths,
            config.web.tls,
            custom_dir=runtime.custom_tls_dir(env),
            runtime_dir=runtime.runtime_tls_dir(env),
            web_gid=web_gid,
            now=now,
        )
    except ConfigError as exc:
        logger.error("%s", exc.render())
        logger.error(_NOT_STARTING)
        return 1
    except (RunModeError, runtime.RuntimeSettingError, DataDirError, TlsError) as exc:
        logger.error("%s", exc)
        logger.error(_NOT_STARTING)
        return 1

    log_certificate(active, now)
    for host in uncovered_hosts(active.info, config.web.tls):
        logger.warning("The active certificate does not cover %s from web.tls.", host)
    if not paths.password_file.exists():
        logger.warning("No password is set. Run inside the container: twinraid-admin set-password")
    return 0
