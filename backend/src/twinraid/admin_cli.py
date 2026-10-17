"""Admin-CLI twinraid-admin (Konzept 5.5).

Aufruf im Container als root, z. B. über die Container-Konsole in Unraid:

    twinraid-admin set-password
    twinraid-admin show-cert-fingerprint
    twinraid-admin renew-cert [--new-ca]
    twinraid-admin unlock
    twinraid-admin init-ssh-keys [--key status|power] [--new]
    twinraid-admin trust-host-key target
    twinraid-admin show-action-log [--lines N]
    twinraid-admin mock-job on|off      (nur im Simulationsmodus)
    twinraid-admin mock-tour on|off     (nur im Simulationsmodus)
    twinraid-admin mock-fault <Name> on|off   (nur im Simulationsmodus)

Root darf ohne DAC_OVERRIDE nicht in die Datenbank des Web-Prozesses schreiben.
Diese Zugriffe laufen deshalb in einem Kindprozess als Web-Benutzer.
"""

import argparse
import asyncio
import getpass
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from twinraid.config import ConfigError, TwinRaidConfig, load_config
from twinraid.integrations.mocks.jobs import MockJobSwitch
from twinraid.integrations.mocks.lab import FAULTS, fault_flag, set_flag, tour_flag
from twinraid.integrations.ssh import (
    GUARD_CHECK_PATTERN,
    REQUIRED_GUARD_VERSION,
    GuardClient,
    GuardKey,
    SshError,
    SshTarget,
    authorized_keys_line,
    generate_key,
    key_files,
    read_public_key,
    save_known_hosts,
    scan_host_key,
)
from twinraid.shared import runtime
from twinraid.shared.actions import ActionOutcome
from twinraid.shared.addresses import AddressError, resolve_addresses
from twinraid.shared.datadirs import DataPaths
from twinraid.shared.mode import RunMode, RunModeError, resolve_run_mode
from twinraid.shared.passwords import PasswordPolicyError, PasswordStore
from twinraid.shared.tls import TlsError, activate, certificate_info, read_runtime
from twinraid.web.audit import AuditAction, AuditLog, AuditResult
from twinraid.web.db import WebDatabase
from twinraid.web.ratelimit import LoginLimiter
from twinraid.web.sessions import SessionStore
from twinraid.worker.action_log import read_entries

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
    mode: RunMode
    paths: DataPaths
    runtime_tls_dir: Path
    custom_tls_dir: Path
    mock_job_flag: Path
    mock_flag_dir: Path
    web_uid: int
    web_gid: int

    @classmethod
    def from_env(cls) -> Context:
        env = os.environ
        try:
            config = load_config(runtime.config_path(env))
            return cls(
                config=config,
                mode=resolve_run_mode(env),
                paths=DataPaths(runtime.data_dir(env)),
                runtime_tls_dir=runtime.runtime_tls_dir(env),
                custom_tls_dir=runtime.custom_tls_dir(env),
                mock_job_flag=runtime.mock_job_flag(env),
                mock_flag_dir=runtime.mock_flag_dir(env),
                web_uid=runtime.web_uid(env),
                web_gid=runtime.web_gid(env),
            )
        except ConfigError as exc:
            raise CliError(exc.render()) from None
        except (runtime.RuntimeSettingError, RunModeError) as exc:
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
        state = read_runtime(context.runtime_tls_dir)
    except TlsError as exc:
        raise CliError(str(exc)) from None
    now = datetime.now(UTC)
    if state.ca is not None:
        ca = certificate_info(state.ca)
        say(f"CA certificate: {ca.name} (install this one on your devices)")
        say(f"  SHA-256 fingerprint: {ca.fingerprint}")
        say(f"  SHA-1 fingerprint (shown by Windows): {ca.fingerprint_sha1}")
        say(f"  Valid until: {ca.not_after.date().isoformat()} ({ca.days_left(now)} days left)")
    info = certificate_info(state.leaf)
    label = "TwinRaid CA" if state.source == "ca" else "custom certificate from /config/tls"
    say(f"Server certificate: {label}")
    say(f"  SHA-256 fingerprint: {info.fingerprint}")
    say(f"  Valid until: {info.not_after.date().isoformat()} ({info.days_left(now)} days left)")
    say(f"  Addresses: {', '.join(sorted(info.ip_addresses | info.hostnames)) or 'none'}")


def cmd_show_cert_fingerprint(context: Context, _args: argparse.Namespace) -> int:
    _print_certificate(context)
    return 0


def cmd_renew_cert(context: Context, args: argparse.Namespace) -> int:
    context.require_owner()
    new_ca = bool(args.new_ca)
    if os.geteuid() == 0 and context.web_gid not in os.getgroups():
        # Nötig, damit die Kopie unter /run der Gruppe des Web-Prozesses gehören darf.
        os.setgroups([context.web_gid])
    try:
        active = activate(
            context.paths,
            resolve_addresses(context.config.web.tls),
            context.custom_tls_dir,
            context.runtime_tls_dir,
            context.web_gid,
            renew_server=True,
            new_ca=new_ca,
        )
    except (TlsError, AddressError) as exc:
        raise CliError(str(exc)) from None
    _audit(context, AuditAction.CLI_RENEW_CERT)
    if active.source == "custom":
        warn(f"The custom certificate in {context.custom_tls_dir} stays active.")
    if _s6("-r", "twinraid-web"):
        say("The web process is restarting with the new certificate.")
    else:
        warn("Restart the web process to load the new certificate.")
    _print_certificate(context)
    if new_ca:
        say("A new CA was created. Install the new CA certificate on all devices and remove")
        say("the old one; until then browsers show a warning.")
    else:
        say("Only the server certificate was renewed. Devices that trust the CA need no change.")
    return 0


def cmd_mock_job(context: Context, args: argparse.Namespace) -> int:
    _require_mock(context, "mock-job")
    running = args.state == "on"
    try:
        MockJobSwitch(context.mock_job_flag).set_running(running)
    except OSError as exc:
        raise CliError(f"Cannot switch the simulated copy job: {exc}") from None
    state = "running" if running else "stopped"
    say(f"Simulated copy job is now {state}. The dashboard shows it within a few seconds.")
    return 0


def _require_mock(context: Context, command: str) -> None:
    if context.mode is not RunMode.MOCK:
        raise CliError(f"{command} only works in simulation mode (TWINRAID_MODE=mock).")
    context.require_owner()


def cmd_mock_tour(context: Context, args: argparse.Namespace) -> int:
    _require_mock(context, "mock-tour")
    on = args.state == "on"
    try:
        set_flag(tour_flag(context.mock_flag_dir), on)
    except OSError as exc:
        raise CliError(f"Cannot switch the tour: {exc}") from None
    if on:
        say("The simulation now cycles through all states and reasons. Actions are locked.")
    else:
        say("The interactive simulation is back. Actions are available again.")
    return 0


def cmd_mock_fault(context: Context, args: argparse.Namespace) -> int:
    _require_mock(context, "mock-fault")
    on = args.state == "on"
    try:
        set_flag(fault_flag(context.mock_flag_dir, args.fault), on)
    except OSError as exc:
        raise CliError(f"Cannot switch the simulated fault: {exc}") from None
    say(f"Simulated fault {args.fault!r} is now {'on' if on else 'off'}.")
    return 0


# Verständliche Erklärung je Ursache für den Verbindungstest nach trust-host-key.
_SSH_DIAGNOSIS: dict[ActionOutcome, str] = {
    ActionOutcome.SSH_KEY_MISSING: (
        "There is no SSH key '{key}' yet. Run: twinraid-admin init-ssh-keys --key {key}"
    ),
    ActionOutcome.SSH_HOST_UNTRUSTED: "The host key is not confirmed.",
    ActionOutcome.SSH_HOST_KEY_MISMATCH: (
        "The server presents a different host key than the confirmed one. Check it on the "
        "server, then run this command again."
    ),
    ActionOutcome.SSH_AUTH_FAILED: (
        "The server rejects the key '{key}'. Check the line under Users → root → SSH "
        "authorized keys (it must be the line from twinraid-admin init-ssh-keys --key {key}, "
        "including from= with the address of TwinRaid)."
    ),
    ActionOutcome.SSH_UNREACHABLE: (
        "SSH does not answer. Enable it on the server: Settings → Management Access → Use SSH."
    ),
    ActionOutcome.SSH_DENIED: (
        "guard.sh rejected the test. Copy host-scripts/guard.sh (version 2) to the server "
        "again; version 1 does not know the mode 'status'."
    ),
    ActionOutcome.REMOTE_FAILED: (
        "The key is accepted, but guard.sh did not answer. Check that guard.sh is stored as "
        "/boot/config/twinraid/guard.sh and that the authorized_keys line contains "
        'command="/bin/bash /boot/config/twinraid/guard.sh {key}".'
    ),
    ActionOutcome.SSH_FAILED: "The SSH connection failed.",
}


def _needed_keys(context: Context) -> list[GuardKey]:
    """„status“ immer; „power“ nur für freigeschaltete VM- oder Shutdown-Aktionen (Konzept 5.5)."""
    actions = context.config.actions
    if context.mode is RunMode.MOCK or actions.vm or actions.shutdown:
        return [GuardKey.STATUS, GuardKey.POWER]
    return [GuardKey.STATUS]


def _ensure_key(context: Context, key: GuardKey, replace: bool) -> str:
    """Legt den Key an, falls er fehlt (oder ersetzt ihn); gibt den öffentlichen Teil zurück."""
    private, public_path = key_files(context.paths, key)
    public = read_public_key(public_path, key)
    if not replace and public is not None and private.is_file():
        say(f"The SSH key '{key}' already exists (use --key {key} --new to replace it).")
        return public
    public = generate_key(private, public_path, key)
    _audit(context, AuditAction.CLI_INIT_SSH_KEYS)
    say(f"Created the SSH key '{key}'.")
    if replace:
        target = context.config.servers.target
        warn(f"The old key '{key}' no longer works. Replace its line on {target.name}.")
    return public


def cmd_init_ssh_keys(context: Context, args: argparse.Namespace) -> int:
    context.require_owner()
    if args.new and args.key is None:
        raise CliError("--new replaces one key; name it with --key status or --key power.")
    try:
        addresses = resolve_addresses(context.config.web.tls)
    except AddressError as exc:
        raise CliError(str(exc)) from None
    keys = [GuardKey(args.key)] if args.key is not None else _needed_keys(context)
    lines = [
        authorized_keys_line(_ensure_key(context, key, bool(args.new)), addresses.ip_addresses, key)
        for key in keys
    ]
    target = context.config.servers.target
    say("")
    say(f"On {target.name}: Users → root → SSH authorized keys. Add each line and Save:")
    for line in lines:
        say("")
        say(line)
    say("")
    if args.key is None and GuardKey.POWER not in keys:
        say("The key 'power' is only needed when actions.vm or actions.shutdown is enabled.")
    say("Then store host-scripts/guard.sh (version 2) as /boot/config/twinraid/guard.sh,")
    say("write the UUID of the gaming VM to /boot/config/twinraid/power.conf as 'vm <UUID>'")
    say("and confirm the host key: twinraid-admin trust-host-key target")
    return 0


def _normalize_ssh_fingerprint(value: str) -> str:
    text = value.strip()
    return text if text.startswith("SHA256:") else f"SHA256:{text}"


def guard_client(context: Context, key: GuardKey) -> GuardClient:
    return GuardClient(SshTarget.of(context.config, context.paths, key))


def _test_guard(context: Context, key: GuardKey) -> bool:
    """Verbindungstest mit "check"; verändert nichts auf dem Server."""
    target = context.config.servers.target
    say(f"Testing SSH to {target.name} with the key '{key}' (check, changes nothing) ...")
    try:
        answer = asyncio.run(guard_client(context, key).check())
    except SshError as exc:
        warn(_SSH_DIAGNOSIS.get(exc.outcome, "The SSH connection failed.").format(key=key))
        return False
    match = GUARD_CHECK_PATTERN.fullmatch(answer)
    if match is None:
        warn(
            f"WARNING: the key '{key}' works, but guard.sh did not answer. The authorized_keys "
            "line probably lacks command=… and allows a full shell. Replace it with the line "
            f"from twinraid-admin init-ssh-keys --key {key}."
        )
        return False
    version, mode = int(match.group(1)), match.group(2)
    if mode != key.value:
        warn(
            f"The line for the key '{key}' runs guard.sh in mode '{mode}'. Replace it with the "
            f"line from twinraid-admin init-ssh-keys --key {key}."
        )
        return False
    if version < REQUIRED_GUARD_VERSION:
        warn(
            f"guard.sh on {target.name} is version {version}; TwinRaid needs version "
            f"{REQUIRED_GUARD_VERSION}. Copy host-scripts/guard.sh to the server again."
        )
        return False
    say(f"OK: guard.sh answers ({answer}).")
    return True


def cmd_trust_host_key(context: Context, args: argparse.Namespace) -> int:
    context.require_owner()
    if context.mode is not RunMode.REAL:
        raise CliError("This command contacts the target server and needs TWINRAID_MODE=real.")
    target = context.config.servers.target
    host, port = str(target.host), target.ssh_port
    try:
        host_key = asyncio.run(scan_host_key(host, port))
    except ValueError:
        raise CliError(
            f"Cannot read the SSH host key of {target.name} ({host}, port {port}). Is SSH "
            "enabled there (Settings → Management Access → Use SSH)?"
        ) from None
    say(f"{target.name} ({host}, port {port}) presents the SSH host key")
    say(f"ED25519 {host_key.fingerprint}")
    if args.fingerprint is not None:
        if _normalize_ssh_fingerprint(args.fingerprint) != host_key.fingerprint:
            raise CliError("The fingerprint does not match --fingerprint. Nothing was saved.")
    else:
        say(f"Compare it in the Unraid terminal of {target.name}:")
        say("  ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub")
        answer = input("Do both fingerprints match? Type 'yes' to trust this host key: ")
        if answer.strip().lower() != "yes":
            raise CliError("Not confirmed. Nothing was saved.")
    known_hosts = context.paths.target_known_hosts
    try:
        current = known_hosts.read_text(encoding="ascii").strip()
    except FileNotFoundError, UnicodeDecodeError:
        current = ""
    if current == host_key.known_hosts_line:
        say("This host key was already confirmed.")
    else:
        save_known_hosts(known_hosts, host_key)
        _audit(context, AuditAction.CLI_TRUST_HOST_KEY)
        say("Host key confirmed. From now on TwinRaid only accepts exactly this key.")
    keys = [key for key in GuardKey if key_files(context.paths, key)[0].is_file()]
    if not keys:
        warn("No SSH keys yet. Run: twinraid-admin init-ssh-keys")
        return 0
    results = [_test_guard(context, key) for key in keys]
    return 0 if all(results) else 1


def cmd_show_action_log(context: Context, args: argparse.Namespace) -> int:
    context.require_owner()
    entries = read_entries(context.paths.action_log, args.lines)
    if not entries:
        say("No actions recorded yet.")
        return 0
    for entry in entries:
        via = f" (part of {entry['part_of']})" if "part_of" in entry else ""
        say(
            f"{entry.get('at', '?')}  {entry.get('id', '?')}  {entry.get('action', '?')}"
            f"  {entry.get('trigger', '?')}{via}: {entry.get('result', '?')}"
        )
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
    renew = commands.add_parser("renew-cert", help="renew the TLS server certificate")
    renew.add_argument(
        "--new-ca",
        action="store_true",
        help="also create a new CA (it must then be installed again on all devices)",
    )
    renew.set_defaults(handler=cmd_renew_cert)
    commands.add_parser(
        "show-cert-fingerprint", help="show the fingerprints of the CA and the server certificate"
    ).set_defaults(handler=cmd_show_cert_fingerprint)
    commands.add_parser("unlock", help="clear all login locks and throttling").set_defaults(
        handler=cmd_unlock
    )
    init_keys = commands.add_parser(
        "init-ssh-keys", help="create the SSH keys and print their authorized_keys lines"
    )
    init_keys.add_argument(
        "--key",
        choices=[key.value for key in GuardKey],
        help="only this key (default: 'status', plus 'power' if VM or shutdown actions are on)",
    )
    init_keys.add_argument(
        "--new",
        action="store_true",
        help="replace the key given with --key (update the server too)",
    )
    init_keys.set_defaults(handler=cmd_init_ssh_keys)
    trust_host = commands.add_parser(
        "trust-host-key", help="confirm the SSH host key of a server and test the connection"
    )
    trust_host.add_argument("server", choices=["target"])
    trust_host.add_argument(
        "--fingerprint", help="expected SHA256 fingerprint; confirms without asking (scripts)"
    )
    trust_host.set_defaults(handler=cmd_trust_host_key)
    action_log = commands.add_parser("show-action-log", help="show the action log of the worker")
    action_log.add_argument("--lines", type=int, default=50, help="number of entries (default 50)")
    action_log.set_defaults(handler=cmd_show_action_log)
    mock_job = commands.add_parser(
        "mock-job", help="switch the simulated copy job (simulation mode only)"
    )
    mock_job.add_argument("state", choices=["on", "off"])
    mock_job.set_defaults(handler=cmd_mock_job)
    mock_tour = commands.add_parser(
        "mock-tour", help="cycle through all states and reasons (simulation mode only)"
    )
    mock_tour.add_argument("state", choices=["on", "off"])
    mock_tour.set_defaults(handler=cmd_mock_tour)
    mock_fault = commands.add_parser(
        "mock-fault", help="simulate a fault of the target server (simulation mode only)"
    )
    mock_fault.add_argument("fault", choices=list(FAULTS))
    mock_fault.add_argument("state", choices=["on", "off"])
    mock_fault.set_defaults(handler=cmd_mock_fault)
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
