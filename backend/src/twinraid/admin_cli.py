"""Admin-CLI twinraid-admin (Konzept 5.5).

Aufruf im Container als root, z. B. über die Container-Konsole in Unraid:

    twinraid-admin set-password
    twinraid-admin show-cert-fingerprint
    twinraid-admin renew-cert [--new-ca]
    twinraid-admin unlock
    twinraid-admin set-api-key target
    twinraid-admin trust-api-cert target
    twinraid-admin init-ssh-keys [--new]
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
from twinraid.integrations.factory import target_api, target_ssh
from twinraid.integrations.mocks.jobs import MockJobSwitch
from twinraid.integrations.mocks.lab import FAULTS, fault_flag, set_flag, tour_flag
from twinraid.integrations.network import tcp_reachable
from twinraid.integrations.ssh import (
    GUARD_CHECK_PATTERN,
    SshError,
    authorized_keys_line,
    generate_power_key,
    read_public_key,
    save_known_hosts,
    scan_host_key,
)
from twinraid.integrations.unraid_api import (
    ApiEndpoint,
    Credentials,
    UnraidApiError,
    normalize_fingerprint,
    presented_fingerprint,
)
from twinraid.shared import runtime
from twinraid.shared.actions import ActionOutcome
from twinraid.shared.addresses import AddressError, resolve_addresses
from twinraid.shared.datadirs import DataPaths
from twinraid.shared.mode import RunMode, RunModeError, resolve_run_mode
from twinraid.shared.passwords import PasswordPolicyError, PasswordStore
from twinraid.shared.status import StatusReason
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

    def target_credentials(self) -> Credentials:
        return Credentials(self.paths.target_api_key, self.paths.target_api_cert)

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


# Verständliche Erklärung je Ursache für die Diagnose nach set-api-key und trust-api-cert.
_DIAGNOSIS: dict[StatusReason, str] = {
    StatusReason.API_UNREACHABLE: (
        "The server answers on {port}, but its API does not. Check webgui.scheme and "
        "webgui.port in twinraid.yaml."
    ),
    StatusReason.API_REDIRECTED: (
        "Unraid redirects to HTTPS. Set webgui.scheme: https (port 443) in twinraid.yaml, "
        "restart the container and run: twinraid-admin trust-api-cert target"
    ),
    StatusReason.API_KEY_MISSING: "No API key is set. Run: twinraid-admin set-api-key target",
    StatusReason.API_KEY_INVALID: (
        "Unraid rejected the API key. Check that you copied the whole key, or create a new one."
    ),
    StatusReason.API_FORBIDDEN: (
        "The API key may not read VMs. In Unraid give it the permission VMS: Read (any)."
    ),
    StatusReason.API_CERT_UNTRUSTED: (
        "The certificate of the server is not confirmed yet. "
        "Run: twinraid-admin trust-api-cert target"
    ),
    StatusReason.API_CERT_CHANGED: (
        "The server presents a different certificate than the confirmed one. Check it in "
        "your browser, then run: twinraid-admin trust-api-cert target"
    ),
    StatusReason.API_ERROR: "The Unraid API gave an unexpected answer.",
}


def _warn_unencrypted(context: Context) -> None:
    target = context.config.servers.target
    if not target.webgui.encrypted:
        warn(
            f"Note: the connection to {target.name} is unencrypted (webgui.scheme: http). "
            "The API key is sent in plain text through the server network."
        )


def _diagnose_target(context: Context) -> bool:
    """Testabfrage an Unraid Groß. Gibt True zurück, wenn der VM-Status lesbar ist."""
    target = context.config.servers.target
    endpoint = ApiEndpoint.of(target)
    say(f"Testing {endpoint.url} ...")
    if not asyncio.run(tcp_reachable(endpoint.host, endpoint.port)):
        warn(f"{target.name} is not reachable on port {endpoint.port}. Is it switched on?")
        return False
    try:
        states = target_api(context.config, context.paths).vm_states()
    except UnraidApiError as exc:
        warn(_DIAGNOSIS[exc.reason].format(port=endpoint.port))
        return False
    vm_name = context.config.gaming_vm.unraid_vm_name
    if vm_name not in states:
        warn(f"The API works, but there is no VM named {vm_name!r} on {target.name}.")
        warn(f"VMs found: {', '.join(sorted(states)) or 'none'}")
        return False
    say(f"OK: the API key works. VM {vm_name!r} is {states[vm_name]}.")
    return True


def _confirm_certificate(context: Context, expected: str | None) -> str:
    """Zeigt den Fingerabdruck von Groß und fragt nach Bestätigung (Konzept 3.5)."""
    target = context.config.servers.target
    endpoint = ApiEndpoint.of(target)
    try:
        presented = presented_fingerprint(endpoint)
    except UnraidApiError:
        raise CliError(
            f"Cannot connect to {endpoint.url}. Is {target.name} on and set to HTTPS?"
        ) from None
    say(f"{target.name} ({endpoint.url}) presents a certificate with the")
    say(f"SHA-256 fingerprint: {presented}")
    if expected is not None:
        try:
            wanted = normalize_fingerprint(expected)
        except ValueError as exc:
            raise CliError(f"--fingerprint: {exc}") from None
        if wanted != presented:
            raise CliError("The fingerprint does not match --fingerprint. Nothing was saved.")
        return presented
    say("Compare it with your browser: open the Unraid web interface via https, click the")
    say("warning or lock symbol and show the certificate (SHA-256 fingerprint).")
    answer = input("Do both fingerprints match? Type 'yes' to trust this certificate: ")
    if answer.strip().lower() != "yes":
        raise CliError("Not confirmed. Nothing was saved.")
    return presented


def _require_target_https(context: Context) -> None:
    if not context.config.servers.target.webgui.encrypted:
        raise CliError(
            "webgui.scheme of the target is http, so there is no certificate to trust. "
            "Switch Unraid and twinraid.yaml to https first (see docs/EINRICHTUNG.md)."
        )


def cmd_set_api_key(context: Context, args: argparse.Namespace) -> int:
    context.require_owner()
    target = context.config.servers.target
    if args.stdin:
        key = sys.stdin.readline().strip()
    else:
        key = getpass.getpass(f"API key of {target.name} (input is hidden): ").strip()
    credentials = context.target_credentials()
    try:
        credentials.save_api_key(key)
    except ValueError as exc:
        raise CliError(f"{exc}. Nothing was saved.") from None
    _audit(context, AuditAction.CLI_SET_API_KEY)
    say(f"API key for {target.name} saved. It is used from the next status check.")
    _warn_unencrypted(context)
    if context.mode is RunMode.MOCK:
        say("Simulation mode: the key was saved but not tested. Switch to TWINRAID_MODE=real.")
        return 0
    if target.webgui.encrypted and credentials.pinned_fingerprint() is None:
        if args.stdin:
            warn("Confirm the certificate next: twinraid-admin trust-api-cert target")
            return 0
        credentials.save_fingerprint(_confirm_certificate(context, None))
        _audit(context, AuditAction.CLI_TRUST_API_CERT)
        say("Certificate confirmed.")
    return 0 if _diagnose_target(context) else 1


def cmd_trust_api_cert(context: Context, args: argparse.Namespace) -> int:
    context.require_owner()
    if context.mode is not RunMode.REAL:
        raise CliError("This command contacts the target server and needs TWINRAID_MODE=real.")
    _require_target_https(context)
    credentials = context.target_credentials()
    fingerprint = _confirm_certificate(context, args.fingerprint)
    if credentials.pinned_fingerprint() == fingerprint:
        say("This certificate was already confirmed.")
    else:
        credentials.save_fingerprint(fingerprint)
        _audit(context, AuditAction.CLI_TRUST_API_CERT)
        say("Certificate confirmed. It is used from the next status check.")
    if credentials.api_key() is None:
        warn("No API key is set yet. Run: twinraid-admin set-api-key target")
        return 0
    return 0 if _diagnose_target(context) else 1


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
        "There is no SSH key 'power' yet. Run: twinraid-admin init-ssh-keys"
    ),
    ActionOutcome.SSH_HOST_UNTRUSTED: "The host key is not confirmed.",
    ActionOutcome.SSH_HOST_KEY_MISMATCH: (
        "The server presents a different host key than the confirmed one. Check it on the "
        "server, then run this command again."
    ),
    ActionOutcome.SSH_AUTH_FAILED: (
        "The server rejects the key. Check the line under Users → root → SSH authorized keys "
        "(it must be the line from twinraid-admin init-ssh-keys, including from= with the "
        "address of TwinRaid)."
    ),
    ActionOutcome.SSH_UNREACHABLE: (
        "SSH does not answer. Enable it on the server: Settings → Management Access → Use SSH."
    ),
    ActionOutcome.SSH_DENIED: "guard.sh rejected the test. Copy the current guard.sh again.",
    ActionOutcome.REMOTE_FAILED: (
        "The key is accepted, but guard.sh did not answer. Check that guard.sh is stored as "
        "/boot/config/twinraid/guard.sh and that the authorized_keys line contains "
        'command="/bin/bash /boot/config/twinraid/guard.sh power".'
    ),
    ActionOutcome.SSH_FAILED: "The SSH connection failed.",
}


def cmd_init_ssh_keys(context: Context, args: argparse.Namespace) -> int:
    context.require_owner()
    paths = context.paths
    target = context.config.servers.target
    public = read_public_key(paths.ssh_power_public_key)
    if args.new or public is None or not paths.ssh_power_key.is_file():
        replaced = public is not None
        public = generate_power_key(paths.ssh_power_key, paths.ssh_power_public_key)
        _audit(context, AuditAction.CLI_INIT_SSH_KEYS)
        say("Created the SSH key 'power'.")
        if replaced:
            warn(f"The old key no longer works. Replace its line on {target.name}.")
    else:
        say("The SSH key 'power' already exists (use --new to replace it).")
    try:
        addresses = resolve_addresses(context.config.web.tls)
    except AddressError as exc:
        raise CliError(str(exc)) from None
    say("")
    say(f"On {target.name}: Users → root → SSH authorized keys. Add this single line and Save:")
    say("")
    say(authorized_keys_line(public, addresses.ip_addresses))
    say("")
    say("Then store host-scripts/guard.sh as /boot/config/twinraid/guard.sh on the server")
    say("and confirm its host key: twinraid-admin trust-host-key target")
    return 0


def _normalize_ssh_fingerprint(value: str) -> str:
    text = value.strip()
    return text if text.startswith("SHA256:") else f"SHA256:{text}"


def _test_guard(context: Context) -> bool:
    """Verbindungstest mit "check"; verändert nichts auf dem Server."""
    target = context.config.servers.target
    say(f"Testing SSH to {target.name} with the key 'power' (check, changes nothing) ...")
    try:
        answer = asyncio.run(target_ssh(context.config, context.paths).check())
    except SshError as exc:
        warn(_SSH_DIAGNOSIS.get(exc.outcome, "The SSH connection failed."))
        return False
    if GUARD_CHECK_PATTERN.fullmatch(answer) is None:
        warn(
            "WARNING: the key works, but guard.sh did not answer. The authorized_keys line "
            "probably lacks command=… and allows a full shell. Replace it with the line from "
            "twinraid-admin init-ssh-keys."
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
    if not context.paths.ssh_power_key.is_file():
        warn("No SSH key 'power' yet. Run: twinraid-admin init-ssh-keys")
        return 0
    return 0 if _test_guard(context) else 1


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
    set_key = commands.add_parser("set-api-key", help="store the Unraid API key of a server")
    # Der Key für Klein folgt in Phase 5.
    set_key.add_argument("server", choices=["target"])
    set_key.add_argument(
        "--stdin", action="store_true", help="read the key from standard input (for scripts)"
    )
    set_key.set_defaults(handler=cmd_set_api_key)
    trust = commands.add_parser(
        "trust-api-cert", help="confirm the HTTPS certificate of a server's Unraid API"
    )
    trust.add_argument("server", choices=["target"])
    trust.add_argument(
        "--fingerprint", help="expected SHA-256 fingerprint; confirms without asking (scripts)"
    )
    trust.set_defaults(handler=cmd_trust_api_cert)
    init_keys = commands.add_parser(
        "init-ssh-keys", help="create the SSH key 'power' and print its authorized_keys line"
    )
    init_keys.add_argument(
        "--new", action="store_true", help="replace an existing key (update the server too)"
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
