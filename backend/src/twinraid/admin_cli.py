"""Admin-CLI twinraid-admin (Konzept 5.5).

Aufruf im Container als root, z. B. über die Container-Konsole in Unraid:

    twinraid-admin set-password
    twinraid-admin show-cert-fingerprint
    twinraid-admin renew-cert
    twinraid-admin unlock

Root darf ohne DAC_OVERRIDE nicht in die Datenbank des Web-Prozesses schreiben.
Diese Zugriffe laufen deshalb in einem Kindprozess als Web-Benutzer.
"""

import argparse
import getpass
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from twinraid.config import ConfigError, TwinRaidConfig, load_config
from twinraid.shared import runtime
from twinraid.shared.datadirs import DataPaths
from twinraid.shared.passwords import PasswordPolicyError, PasswordStore
from twinraid.shared.tls import TlsError, activate, certificate_info, read_runtime
from twinraid.web.audit import AuditAction, AuditLog, AuditResult
from twinraid.web.db import WebDatabase
from twinraid.web.ratelimit import LoginLimiter
from twinraid.web.sessions import SessionStore

S6_SVC = Path("/command/s6-svc")
SERVICE_DIR = Path("/run/service")


class CliError(Exception):
    """Fehler, der dem Admin verständlich gemeldet wird."""


def say(message: str) -> None:
    sys.stdout.write(message + "\n")


def warn(message: str) -> None:
    sys.stderr.write(message + "\n")


@dataclass(frozen=True)
class Context:
    config: TwinRaidConfig
    paths: DataPaths
    runtime_tls_dir: Path
    custom_tls_dir: Path
    web_uid: int
    web_gid: int

    @classmethod
    def from_env(cls) -> Context:
        env = os.environ
        try:
            config = load_config(runtime.config_path(env))
            return cls(
                config=config,
                paths=DataPaths(runtime.data_dir(env)),
                runtime_tls_dir=runtime.runtime_tls_dir(env),
                custom_tls_dir=runtime.custom_tls_dir(env),
                web_uid=runtime.web_uid(env),
                web_gid=runtime.web_gid(env),
            )
        except ConfigError as exc:
            raise CliError(exc.render()) from None
        except runtime.RuntimeSettingError as exc:
            raise CliError(str(exc)) from None

    def require_owner(self) -> None:
        """Nur der Besitzer der Datenablage (im Container root) darf ändern."""
        try:
            owner = self.paths.root.stat().st_uid
        except FileNotFoundError:
            raise CliError(f"{self.paths.root} does not exist; start the container once.") from None
        if os.geteuid() != owner:
            raise CliError(
                "Run this command as root inside the container, e.g. "
                "docker exec -it twinraid twinraid-admin <command>"
            )
        if not self.paths.secrets.is_dir() or not self.paths.web.is_dir():
            raise CliError("The data directory is not prepared yet; start the container once.")

    def as_web_user(self, operation: Callable[[WebDatabase], None]) -> None:
        """Führt einen Datenbankzugriff als Web-Benutzer aus."""
        database = WebDatabase(self.paths.web_database)

        def run() -> None:
            os.umask(0o077)
            database.initialize()
            operation(database)

        if os.geteuid() == self.web_uid:
            run()
            return
        sys.stdout.flush()
        sys.stderr.flush()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - läuft nur als root im Container
            code = 1
            try:
                os.setgroups([])
                os.setresgid(self.web_gid, self.web_gid, self.web_gid)
                os.setresuid(self.web_uid, self.web_uid, self.web_uid)
                run()
                code = 0
            except BaseException as exc:  # noqa: BLE001 - Kindprozess meldet jeden Fehler
                warn(f"Error: {exc}")
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        if os.waitstatus_to_exitcode(status) != 0:
            raise CliError("the database operation as web user failed")


def _audit(context: Context, action: AuditAction) -> None:
    context.as_web_user(lambda db: AuditLog(db).record(action, AuditResult.SUCCESS, None))


def _s6(flag: str, service: str) -> bool:
    """Signal an einen s6-Dienst. False außerhalb des Containers."""
    service_dir = SERVICE_DIR / service
    if not S6_SVC.exists() or not service_dir.exists():
        return False
    # Feste Pfade und Argumente, keine Shell (CLAUDE.md, Regel 3).
    subprocess.run([str(S6_SVC), flag, str(service_dir)], check=True)  # noqa: S603
    return True


def _read_new_password(from_stdin: bool) -> str:
    if from_stdin:
        line = sys.stdin.readline()
        return line.removesuffix("\n").removesuffix("\r")
    first = getpass.getpass("New password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        raise CliError("The passwords do not match.")
    return first


def cmd_set_password(context: Context, args: argparse.Namespace) -> int:
    context.require_owner()
    password = _read_new_password(bool(args.stdin))
    try:
        PasswordStore(context.paths.password_file).set(password)
    except PasswordPolicyError as exc:
        raise CliError(f"{exc}.") from None
    idle = context.config.web.session_idle_minutes * 60

    def end_sessions(db: WebDatabase) -> None:
        SessionStore(db, idle).delete_all()
        AuditLog(db).record(AuditAction.CLI_SET_PASSWORD, AuditResult.SUCCESS, None)

    context.as_web_user(end_sessions)
    say("Password set. All existing sessions have been ended.")
    return 0


def cmd_unlock(context: Context, _args: argparse.Namespace) -> int:
    context.require_owner()

    def unlock(db: WebDatabase) -> None:
        LoginLimiter(db).unlock_all()
        AuditLog(db).record(AuditAction.CLI_UNLOCK, AuditResult.SUCCESS, None)

    context.as_web_user(unlock)
    say("All login locks and failure counters have been cleared.")
    if _s6("-1", "twinraid-worker"):
        say("Login throttling in the worker has been reset.")
    else:
        warn("The worker is not managed by s6 here; restart it to reset its throttling.")
    return 0


def _print_certificate(context: Context) -> None:
    try:
        certificate, source = read_runtime(context.runtime_tls_dir)
    except TlsError as exc:
        raise CliError(str(exc)) from None
    info = certificate_info(certificate)
    now = datetime.now(UTC)
    say(f"Active certificate: {source}")
    say(f"SHA-256 fingerprint: {info.fingerprint}")
    say(f"Valid until: {info.not_after.date().isoformat()} ({info.days_left(now)} days left)")
    say(f"Addresses: {', '.join(sorted(info.ip_addresses | info.hostnames)) or 'none'}")


def cmd_show_cert_fingerprint(context: Context, _args: argparse.Namespace) -> int:
    _print_certificate(context)
    return 0


def cmd_renew_cert(context: Context, _args: argparse.Namespace) -> int:
    context.require_owner()
    if os.geteuid() == 0 and context.web_gid not in os.getgroups():
        # Nötig, damit die Kopie unter /run der Gruppe des Web-Prozesses gehören darf.
        os.setgroups([context.web_gid])
    try:
        active = activate(
            context.paths,
            context.config.web.tls,
            context.custom_tls_dir,
            context.runtime_tls_dir,
            context.web_gid,
            force_new_self_signed=True,
        )
    except TlsError as exc:
        raise CliError(str(exc)) from None
    _audit(context, AuditAction.CLI_RENEW_CERT)
    if active.source == "custom":
        warn("A new self-signed certificate was created, but the custom certificate in ")
        warn(f"{context.custom_tls_dir} stays active.")
    if _s6("-r", "twinraid-web"):
        say("The web process is restarting with the new certificate.")
    else:
        warn("Restart the web process to load the new certificate.")
    _print_certificate(context)
    say("Devices that trusted the old certificate must confirm or install the new one.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="twinraid-admin", description="TwinRaid administration inside the container."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    set_password = commands.add_parser("set-password", help="set the login password")
    set_password.add_argument(
        "--stdin",
        action="store_true",
        help="read the password from standard input instead of prompting (for scripts)",
    )
    set_password.set_defaults(handler=cmd_set_password)
    commands.add_parser("renew-cert", help="create a new self-signed TLS certificate").set_defaults(
        handler=cmd_renew_cert
    )
    commands.add_parser(
        "show-cert-fingerprint", help="show the fingerprint of the active certificate"
    ).set_defaults(handler=cmd_show_cert_fingerprint)
    commands.add_parser("unlock", help="clear all login locks and throttling").set_defaults(
        handler=cmd_unlock
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Callable[[Context, argparse.Namespace], int] = args.handler
    try:
        return handler(Context.from_env(), args)
    except CliError as exc:
        warn(f"Error: {exc}")
        return 1
    except subprocess.CalledProcessError as exc:
        warn(f"Error: {exc}")
        return 1
