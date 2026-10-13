"""Startdienst twinraid-init: Datenablage prüfen und TLS einrichten (Konzept 5.2, 5.4, 5.9).

Läuft im Container nach der Startprüfung als root mit GID 10001. Schlägt er
fehl, startet der Container nicht.

Mit --refresh-tls ruft ihn das Startskript des Web-Dienstes vor jedem Start
des Web-Prozesses auf: Er erneuert nur ein fälliges Serverzertifikat, meldet
sich nur bei Änderungen und vermerkt einen Fehlschlag für das Dashboard. Der
Web-Prozess startet dann mit dem bisherigen Zertifikat.
"""

import argparse
import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime

from twinraid.config import ConfigError, TwinRaidConfig, load_config
from twinraid.shared import runtime
from twinraid.shared.addresses import (
    AddressError,
    TlsAddresses,
    bridge_warning,
    ipv6_addresses,
    resolve_addresses,
)
from twinraid.shared.datadirs import DataDirError, DataPaths, prepare_data_dirs
from twinraid.shared.logs import configure_logging
from twinraid.shared.mode import RunMode, RunModeError, resolve_run_mode
from twinraid.shared.tls import (
    CA_EXPIRY_WARNING,
    FIRST_CA_REASON,
    SERVER_EXPIRY_WARNING,
    ActiveCertificate,
    TlsError,
    activate,
    record_refresh_failure,
    uncovered_hosts,
)

logger = logging.getLogger("twinraid.init")

_NOT_STARTING = "TwinRaid will not start. Fix the problem above and restart the container."


def log_addresses(addresses: TlsAddresses) -> None:
    """Verwendete Adresse, Warnung bei Bridge-Netz, ignorierte IPv6-Adressen (Konzept 5.1)."""
    logger.info("TLS address: %s", addresses.describe())
    warning = bridge_warning(addresses)
    if warning is not None:
        line = "=" * 72
        logger.warning(line)
        logger.warning("  PROBABLY THE WRONG NETWORK TYPE")
        logger.warning("  %s", warning)
        logger.warning(line)
    if addresses.origin == "detected":
        for entry in ipv6_addresses():
            logger.info(
                "IPv6 address %s on %s is not used; add it to web.tls.ip_addresses if "
                "clients connect through it.",
                entry.address,
                entry.interface,
            )


def _log_ca_change(active: ActiveCertificate) -> None:
    """Auffälliger Block: Eine bestehende CA wurde ersetzt (Konzept 5.2)."""
    line = "=" * 72
    logger.warning(line)
    logger.warning("  A NEW TWINRAID CA WAS CREATED")
    logger.warning("  Reason: %s.", active.ca_generated_because)
    logger.warning("  Addresses: %s", active.addresses.describe())
    logger.warning("  Install the new CA certificate on ALL devices (Settings → download CA")
    logger.warning("  certificate) and remove the old one. Until then browsers show a warning.")
    logger.warning(line)


def log_certificate(active: ActiveCertificate, now: datetime, quiet: bool = False) -> None:
    """Schreibt Fingerabdrücke und Hinweise ins Log. quiet: nur bei Änderungen."""
    ca, server = active.ca, active.info
    if active.legacy_removed:
        logger.warning(
            "Replaced the self-signed certificate of an earlier version with the TwinRaid CA. "
            "On your devices remove the old certificate or exception and install the CA "
            "certificate once (Settings → download CA certificate)."
        )
    elif active.ca_generated_because == FIRST_CA_REASON:
        logger.info("Generated the TwinRaid CA. Install its certificate once on your devices.")
    elif active.ca_change is not None:
        _log_ca_change(active)
    if active.server_generated_because is not None and active.ca_generated_because is None:
        logger.info(
            "Renewed the TLS server certificate because %s. Devices that trust the CA need "
            "no change.",
            active.server_generated_because,
        )
    changed = active.ca_generated_because is not None or active.server_generated_because
    if quiet and not changed:
        return
    logger.info("TLS CA certificate %r", ca.name)
    logger.info("TLS CA SHA-256 fingerprint: %s", ca.fingerprint)
    logger.info("TLS CA SHA-1 fingerprint (shown by Windows): %s", ca.fingerprint_sha1)
    logger.info(
        "TLS CA valid until %s (%d days left)", ca.not_after.date().isoformat(), ca.days_left(now)
    )
    logger.info(
        "TLS server certificate (%s) SHA-256 fingerprint: %s", active.source, server.fingerprint
    )
    logger.info("Compare the fingerprints with the certificate shown by your browser or device.")
    addresses = ", ".join(sorted(server.ip_addresses | server.hostnames)) or "none"
    logger.info(
        "Server certificate valid until %s (%d days left), addresses: %s",
        server.not_after.date().isoformat(),
        server.days_left(now),
        addresses,
    )
    if ca.expires_within(now, CA_EXPIRY_WARNING):
        logger.warning(
            "The TwinRaid CA expires in %d days. Create a new one with "
            "'twinraid-admin renew-cert --new-ca' and install it on your devices.",
            ca.days_left(now),
        )
    if server.expires_within(now, SERVER_EXPIRY_WARNING):
        logger.warning("The TLS server certificate expires in %d days.", server.days_left(now))


def log_unencrypted_api(config: TwinRaidConfig, mode: RunMode) -> None:
    """Deutlicher Hinweis, solange die Unraid-API von Groß per http angesprochen wird."""
    target = config.servers.target
    if mode is RunMode.REAL and not target.webgui.encrypted:
        logger.warning(
            "The connection to %s is unencrypted (webgui.scheme: http): the API key is sent "
            "in plain text through the server network.",
            target.name,
        )


def _refresh(config: TwinRaidConfig, now: datetime) -> int:
    env = os.environ
    web_gid = runtime.web_gid(env)
    runtime_dir = runtime.runtime_tls_dir(env)
    try:
        active = activate(
            DataPaths(runtime.data_dir(env)),
            resolve_addresses(config.web.tls),
            custom_dir=runtime.custom_tls_dir(env),
            runtime_dir=runtime_dir,
            web_gid=web_gid,
            now=now,
        )
    except (TlsError, AddressError, OSError) as exc:
        logger.error("Automatic TLS renewal failed: %s", exc)
        logger.error("The web process keeps the current certificate.")
        try:
            record_refresh_failure(runtime_dir, now, web_gid)
        except (TlsError, OSError) as record_exc:
            logger.error("Could not record the failed renewal: %s", record_exc)
        return 1
    log_certificate(active, now, quiet=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="twinraid-init")
    parser.add_argument(
        "--refresh-tls",
        action="store_true",
        help="only renew a due server certificate (run before each start of the web process)",
    )
    args = parser.parse_args(argv)
    # Neue Dateien höchstens für die Gruppe lesbar; Rechte werden ohnehin explizit gesetzt.
    os.umask(0o027)
    configure_logging("init")
    env = os.environ
    now = datetime.now(UTC)
    try:
        mode = resolve_run_mode(env)
        config = load_config(runtime.config_path(env))
        if args.refresh_tls:
            return _refresh(config, now)
        web_gid = runtime.web_gid(env)
        paths = DataPaths(runtime.data_dir(env))
        for correction in prepare_data_dirs(paths, owner_uid=os.geteuid(), web_gid=web_gid):
            logger.info("Data directory: %s", correction)
        addresses = resolve_addresses(config.web.tls)
        log_addresses(addresses)
        active = activate(
            paths,
            addresses,
            custom_dir=runtime.custom_tls_dir(env),
            runtime_dir=runtime.runtime_tls_dir(env),
            web_gid=web_gid,
            now=now,
        )
    except ConfigError as exc:
        logger.error("%s", exc.render())
        logger.error(_NOT_STARTING)
        return 1
    except (RunModeError, runtime.RuntimeSettingError, DataDirError, AddressError, TlsError) as exc:
        logger.error("%s", exc)
        logger.error(_NOT_STARTING)
        return 1

    log_certificate(active, now)
    for host in uncovered_hosts(active.info, active.addresses):
        logger.warning("The active certificate does not cover %s.", host)
    log_unencrypted_api(config, mode)
    if not paths.password_file.exists():
        logger.warning("No password is set. Run inside the container: twinraid-admin set-password")
    return 0
