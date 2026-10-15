"""SSH mit dem Key „power“: Argumente, Fehlerursachen, Hostkey und Schlüssel (Konzept 5.6)."""

import shutil
import stat
import subprocess
from ipaddress import ip_address
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_ssh_private_key,
)

from tests.conftest import EXAMPLE_CONFIG
from tests.support.fake_ssh import FakeSsh
from twinraid.config import load_config
from twinraid.integrations.ssh import (
    SshError,
    SshPower,
    SshTarget,
    authorized_keys_line,
    classify,
    generate_power_key,
    key_fingerprint,
    parse_host_key,
    read_public_key,
    save_known_hosts,
    scan_host_key,
    ssh_arguments,
)
from twinraid.shared.actions import ActionOutcome
from twinraid.shared.datadirs import DataPaths

UUID = "0b5c4e1a-2f3d-4c5b-8a9e-112233445566"


@pytest.fixture
def target(tmp_path: Path) -> SshTarget:
    key, known = tmp_path / "ssh-power", tmp_path / "known"
    key.write_text("key")
    known.write_text("host ssh-ed25519 AAAA\n")
    return SshTarget("192.0.2.11", 22, key, known)


@pytest.fixture
def fake(tmp_path: Path) -> FakeSsh:
    return FakeSsh.install(tmp_path / "bin")


def test_target_from_config(tmp_path: Path) -> None:
    paths = DataPaths(tmp_path)
    target = SshTarget.of(load_config(EXAMPLE_CONFIG), paths)
    assert target == SshTarget("192.168.10.11", 22, paths.ssh_power_key, paths.target_known_hosts)


def test_fixed_argument_list(target: SshTarget) -> None:
    arguments = ssh_arguments(target, ["vm-shutdown", UUID], Path("/usr/bin/ssh"))
    assert arguments[:8] == [
        "/usr/bin/ssh", "-F", "/dev/null", "-T", "-i", str(target.key), "-p", "22",
    ]  # fmt: skip
    options = [arguments[i + 1] for i, value in enumerate(arguments) if value == "-o"]
    assert "StrictHostKeyChecking=yes" in options
    assert f"UserKnownHostsFile={target.known_hosts}" in options
    assert "GlobalKnownHostsFile=/dev/null" in options
    assert "BatchMode=yes" in options
    assert "IdentitiesOnly=yes" in options
    assert "PasswordAuthentication=no" in options
    assert "HostKeyAlgorithms=ssh-ed25519" in options
    assert arguments[-4:] == ["--", "root@192.0.2.11", "vm-shutdown", UUID]


@pytest.mark.parametrize(
    ("code", "stderr", "outcome"),
    [
        (2, "twinraid-guard: denied", ActionOutcome.SSH_DENIED),
        (1, "error: failed to get domain", ActionOutcome.REMOTE_FAILED),
        (127, "bash: check: command not found", ActionOutcome.REMOTE_FAILED),
        (255, "Host key verification failed.", ActionOutcome.SSH_HOST_KEY_MISMATCH),
        (
            255,
            "@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @",
            ActionOutcome.SSH_HOST_KEY_MISMATCH,
        ),
        (255, "root@x: Permission denied (publickey).", ActionOutcome.SSH_AUTH_FAILED),
        (255, "ssh: connect to host x port 22: Connection refused", ActionOutcome.SSH_UNREACHABLE),
        (
            255,
            "ssh: connect to host x port 22: Connection timed out",
            ActionOutcome.SSH_UNREACHABLE,
        ),
        (255, "kex_exchange_identification: read: Connection reset", ActionOutcome.SSH_FAILED),
    ],
)
def test_classify(code: int, stderr: str, outcome: ActionOutcome) -> None:
    error = classify(code, stderr)
    assert error is not None
    assert error.outcome is outcome


def test_classify_success_and_closed_connection() -> None:
    assert classify(0, "") is None
    error = classify(255, "Connection to 192.0.2.11 closed by remote host.")
    assert error is not None
    assert error.connection_closed is True


@pytest.mark.anyio
async def test_requests_reach_the_guard(target: SshTarget, fake: FakeSsh) -> None:
    power = SshPower(target, fake.binary)
    fake.behave(stdout="twinraid-guard 1 power ok\n")
    assert await power.check() == "twinraid-guard 1 power ok"
    fake.behave()
    await power.poweroff()
    await power.vm_shutdown(UUID)
    assert [call[-3:] for call in fake.calls()] == [
        ["--", "root@192.0.2.11", "check"],
        ["--", "root@192.0.2.11", "poweroff"],
        ["root@192.0.2.11", "vm-shutdown", UUID],
    ]


@pytest.mark.anyio
async def test_guard_rejection(target: SshTarget, fake: FakeSsh) -> None:
    fake.behave(exit_code=2, stderr="twinraid-guard: denied\n")
    with pytest.raises(SshError) as excinfo:
        await SshPower(target, fake.binary).poweroff()
    assert excinfo.value.outcome is ActionOutcome.SSH_DENIED


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("missing", "outcome"),
    [("key", ActionOutcome.SSH_KEY_MISSING), ("known", ActionOutcome.SSH_HOST_UNTRUSTED)],
)
async def test_missing_files_send_nothing(
    tmp_path: Path, target: SshTarget, fake: FakeSsh, missing: str, outcome: ActionOutcome
) -> None:
    (tmp_path / ("ssh-power" if missing == "key" else "known")).unlink()
    with pytest.raises(SshError) as excinfo:
        await SshPower(target, fake.binary).poweroff()
    assert excinfo.value.outcome is outcome
    assert fake.calls() == []


@pytest.mark.anyio
@pytest.mark.parametrize("uuid", ["", "x", UUID.upper(), UUID + " ; poweroff", "-" + UUID])
async def test_invalid_uuid_is_never_sent(target: SshTarget, fake: FakeSsh, uuid: str) -> None:
    with pytest.raises(SshError):
        await SshPower(target, fake.binary).vm_shutdown(uuid)
    assert fake.calls() == []


@pytest.mark.anyio
async def test_timeout_means_unreachable(target: SshTarget, fake: FakeSsh) -> None:
    fake.behave(sleep=5)
    with pytest.raises(SshError) as excinfo:
        await SshPower(target, fake.binary, timeout=0.3).check()
    assert excinfo.value.outcome is ActionOutcome.SSH_UNREACHABLE


def _public_key() -> str:
    raw = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH)
    )
    return raw.decode().split()[1]


def test_parse_host_key_from_keyscan_output() -> None:
    key = _public_key()
    output = f"192.0.2.11 ssh-ed25519 {key}\n# comment\n192.0.2.11 ssh-rsa AAAAB3Nza\n"
    host_key = parse_host_key(output, "192.0.2.11", 22)
    assert host_key.known_hosts_line == f"192.0.2.11 ssh-ed25519 {key}"
    assert host_key.fingerprint.startswith("SHA256:")
    other_port = parse_host_key(output, "192.0.2.11", 2222)
    assert other_port.known_hosts_line.startswith("[192.0.2.11]:2222 ssh-ed25519 ")


@pytest.mark.parametrize("output", ["", "host ssh-ed25519 kein-base64", "host ssh-rsa AAAA"])
def test_parse_host_key_rejects_garbage(output: str) -> None:
    with pytest.raises(ValueError, match="exactly one ed25519"):
        parse_host_key(output, "192.0.2.11", 22)


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen not installed")
def test_fingerprint_matches_ssh_keygen(tmp_path: Path) -> None:
    key = _public_key()
    (tmp_path / "key.pub").write_text(f"ssh-ed25519 {key} test\n")
    output = subprocess.run(  # noqa: S603 - feste Argumente, nur im Test
        ["ssh-keygen", "-lf", str(tmp_path / "key.pub")],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert output.split()[1] == key_fingerprint(key)


@pytest.mark.anyio
async def test_scan_host_key(fake: FakeSsh, tmp_path: Path) -> None:
    key = _public_key()
    fake.behave(stdout=f"192.0.2.11 ssh-ed25519 {key}\n")
    host_key = await scan_host_key("192.0.2.11", 22, fake.binary)
    assert fake.calls() == [["-T", "5", "-t", "ed25519", "-p", "22", "--", "192.0.2.11"]]
    save_known_hosts(tmp_path / "known_hosts", host_key)
    assert stat.S_IMODE((tmp_path / "known_hosts").stat().st_mode) == 0o600
    assert (tmp_path / "known_hosts").read_text() == host_key.known_hosts_line + "\n"


def test_generate_power_key(tmp_path: Path) -> None:
    private, public = tmp_path / "ssh-power", tmp_path / "ssh-power.pub"
    line = generate_power_key(private, public)
    assert line.startswith("ssh-ed25519 ")
    assert line.endswith(" twinraid-power")
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(public.stat().st_mode) == 0o600
    loaded = load_ssh_private_key(private.read_bytes(), password=None)
    assert loaded.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode() in line
    assert read_public_key(public) == line
    assert read_public_key(tmp_path / "missing") is None


def test_authorized_keys_line() -> None:
    line = authorized_keys_line(
        "ssh-ed25519 AAAAC3Nza twinraid-power", [ip_address("192.168.10.20")]
    )
    assert line == (
        'restrict,from="192.168.10.20",command="/bin/bash /boot/config/twinraid/guard.sh power" '
        "ssh-ed25519 AAAAC3Nza twinraid-power"
    )
