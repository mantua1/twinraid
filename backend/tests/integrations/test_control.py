"""Echte Steuerung von Groß: API, SSH und Wake-on-LAN zusammen (Konzept 3.4)."""

from collections.abc import Iterator
from ipaddress import IPv4Address
from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from tests.support.fake_ssh import FakeSsh
from tests.support.fake_unraid import READ, UPDATE, FakeUnraid, FakeVm, FakeVmHost
from twinraid.config import load_config
from twinraid.integrations.control import ActionError, RealControl, api_outcome
from twinraid.integrations.ssh import SshPower, SshTarget
from twinraid.integrations.unraid_api import ApiEndpoint, Credentials, UnraidApi
from twinraid.integrations.wol import WolTarget
from twinraid.shared.actions import ActionOutcome
from twinraid.shared.status import StatusReason

pytestmark = pytest.mark.anyio
CONFIG = load_config(EXAMPLE_CONFIG)
KEY = "updateKey_0123456789"
UUID = "0b5c4e1a-2f3d-4c5b-8a9e-112233445566"


@pytest.fixture
def unraid(tmp_path: Path) -> Iterator[FakeUnraid]:
    server = FakeUnraid(tmp_path)
    server.responder = FakeVmHost(
        [FakeVm(UUID, CONFIG.gaming_vm.unraid_vm_name, "RUNNING")], {KEY: {READ, UPDATE}}
    )
    try:
        yield server
    finally:
        server.close()


class Parts:
    def __init__(self, tmp_path: Path, unraid: FakeUnraid) -> None:
        credentials = Credentials(tmp_path / "key", tmp_path / "pin")
        credentials.save_api_key(KEY)
        self.api = UnraidApi(ApiEndpoint("127.0.0.1", unraid.port, False), credentials, timeout=2)
        self.ssh_fake = FakeSsh.install(tmp_path / "bin")
        (tmp_path / "ssh-power").write_text("key")
        (tmp_path / "known").write_text("x ssh-ed25519 AAAA\n")
        target = SshTarget("192.0.2.11", 22, tmp_path / "ssh-power", tmp_path / "known")
        self.ssh = SshPower(target, self.ssh_fake.binary)
        self.sent: list[WolTarget] = []
        self.reachable = True

    def control(self, wol: WolTarget | None = None) -> RealControl:
        async def tcp_check(host: str, port: int) -> bool:
            assert (host, port) == ("192.168.10.11", 80)
            return self.reachable

        return RealControl(CONFIG, self.api, self.ssh, wol, tcp_check, self.sent.append)


@pytest.fixture
def parts(tmp_path: Path, unraid: FakeUnraid) -> Parts:
    return Parts(tmp_path, unraid)


WOL = WolTarget("AA:BB:CC:DD:EE:FF", IPv4Address("192.168.10.255"), "test")


async def test_find_start_and_force(parts: Parts, unraid: FakeUnraid) -> None:
    control = parts.control()
    vm = await control.find_vm()
    assert (vm.uuid, vm.state) == (UUID, "RUNNING")
    await control.force_stop_vm(vm)
    await control.start_vm(await control.find_vm())
    assert isinstance(unraid.responder, FakeVmHost)
    assert unraid.responder.mutations == [("forceStop", UUID), ("start", UUID)]


async def test_vm_not_found(parts: Parts, unraid: FakeUnraid) -> None:
    assert isinstance(unraid.responder, FakeVmHost)
    unraid.responder.vms[0].name = "Andere VM"
    with pytest.raises(ActionError) as excinfo:
        await parts.control().find_vm()
    assert excinfo.value.outcome is ActionOutcome.VM_NOT_FOUND


async def test_read_only_key_is_reported(parts: Parts, unraid: FakeUnraid) -> None:
    assert isinstance(unraid.responder, FakeVmHost)
    unraid.responder.keys[KEY] = {READ}
    control = parts.control()
    with pytest.raises(ActionError) as excinfo:
        await control.start_vm(await control.find_vm())
    assert excinfo.value.outcome is ActionOutcome.API_FORBIDDEN


async def test_shutdown_vm_goes_through_ssh(parts: Parts) -> None:
    control = parts.control()
    await control.shutdown_vm(await control.find_vm())
    assert parts.ssh_fake.calls()[0][-2:] == ["vm-shutdown", UUID]


async def test_ssh_errors_become_outcomes(parts: Parts) -> None:
    parts.ssh_fake.behave(exit_code=255, stderr="root@x: Permission denied (publickey).\n")
    control = parts.control()
    with pytest.raises(ActionError) as excinfo:
        await control.shutdown_vm(await control.find_vm())
    assert excinfo.value.outcome is ActionOutcome.SSH_AUTH_FAILED


async def test_power_off(parts: Parts) -> None:
    await parts.control().power_off()
    assert parts.ssh_fake.calls()[0][-1] == "poweroff"


async def test_power_off_counts_a_closed_connection_as_sent(parts: Parts) -> None:
    parts.ssh_fake.behave(exit_code=255, stderr="Connection to 192.0.2.11 closed by remote host.\n")
    await parts.control().power_off()


async def test_power_off_denied(parts: Parts) -> None:
    parts.ssh_fake.behave(exit_code=2, stderr="twinraid-guard: denied\n")
    with pytest.raises(ActionError) as excinfo:
        await parts.control().power_off()
    assert excinfo.value.outcome is ActionOutcome.SSH_DENIED


async def test_wake_sends_the_magic_packet(parts: Parts) -> None:
    await parts.control(WOL).wake()
    assert parts.sent == [WOL]


async def test_wake_without_broadcast_fails(parts: Parts) -> None:
    with pytest.raises(ActionError) as excinfo:
        await parts.control(None).wake()
    assert excinfo.value.outcome is ActionOutcome.WOL_FAILED


async def test_wake_send_error(parts: Parts) -> None:
    def broken(_wol: WolTarget) -> None:
        raise OSError("network down")

    control = RealControl(CONFIG, parts.api, parts.ssh, WOL, send_wol=broken)
    with pytest.raises(ActionError) as excinfo:
        await control.wake()
    assert excinfo.value.outcome is ActionOutcome.WOL_FAILED


async def test_target_reachable(parts: Parts) -> None:
    control = parts.control()
    assert await control.target_reachable() is True
    parts.reachable = False
    assert await control.target_reachable() is False


@pytest.mark.parametrize("reason", list(StatusReason))
def test_every_api_reason_has_an_outcome(reason: StatusReason) -> None:
    outcome = api_outcome(reason)
    if reason.value.startswith("api_") or reason is StatusReason.VM_NOT_FOUND:
        assert outcome.value == reason.value
    else:
        assert outcome is ActionOutcome.API_ERROR
