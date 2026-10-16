"""Echte Steuerung von Groß: vm-watch, SSH über guard.sh und Wake-on-LAN (Konzept 3.4 und 3.5)."""

from ipaddress import IPv4Address
from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from tests.support.fake_ssh import FakeSsh
from tests.support.watch import StubWatch, report
from twinraid.config import load_config
from twinraid.integrations.control import ActionError, RealControl
from twinraid.integrations.ssh import SshPower, SshTarget
from twinraid.integrations.wol import WolTarget
from twinraid.shared.actions import ActionOutcome
from twinraid.shared.status import StatusReason

pytestmark = pytest.mark.anyio
CONFIG = load_config(EXAMPLE_CONFIG)
WOL = WolTarget("AA:BB:CC:DD:EE:FF", IPv4Address("192.168.10.255"), "test")


class Parts:
    def __init__(self, tmp_path: Path) -> None:
        self.watch = StubWatch()
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

        return RealControl(CONFIG, self.watch, self.ssh, wol, tcp_check, self.sent.append)

    def requests(self) -> list[str]:
        return [call[-1] for call in self.ssh_fake.calls()]


@pytest.fixture
def parts(tmp_path: Path) -> Parts:
    return Parts(tmp_path)


async def test_vm_state_comes_from_the_session_without_ssh(parts: Parts) -> None:
    control = parts.control()
    assert await control.vm_state() == "RUNNING"
    parts.watch.show(report("SHUTOFF"))
    assert await control.vm_state() == "SHUTOFF"
    # Abläufe lesen nur den Stand der Sitzung; es gibt keine weitere Anmeldung.
    assert parts.ssh_fake.calls() == []


@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        ("NOT_CONFIGURED", ActionOutcome.VM_NOT_CONFIGURED),
        ("NOT_FOUND", ActionOutcome.VM_NOT_FOUND),
        ("LIBVIRT_DOWN", ActionOutcome.VM_SERVICE_UNAVAILABLE),
        ("UNKNOWN", ActionOutcome.VM_SERVICE_UNAVAILABLE),
    ],
)
async def test_guard_reports_stop_a_sequence(
    parts: Parts, state: str, outcome: ActionOutcome
) -> None:
    parts.watch.show(report(state, others=None, name=None))
    with pytest.raises(ActionError) as excinfo:
        await parts.control().vm_state()
    assert excinfo.value.outcome is outcome


@pytest.mark.parametrize(
    ("reason", "outcome"),
    [
        (StatusReason.WATCH_CONNECTING, ActionOutcome.WATCH_INTERRUPTED),
        (StatusReason.WATCH_INTERRUPTED, ActionOutcome.WATCH_INTERRUPTED),
        (StatusReason.SSH_AUTH_FAILED, ActionOutcome.SSH_AUTH_FAILED),
        (StatusReason.SSH_KEY_MISSING, ActionOutcome.SSH_KEY_MISSING),
        (StatusReason.GUARD_OUTDATED, ActionOutcome.GUARD_OUTDATED),
        (StatusReason.SSH_UNREACHABLE, ActionOutcome.SSH_UNREACHABLE),
    ],
)
async def test_session_without_a_state(
    parts: Parts, reason: StatusReason, outcome: ActionOutcome
) -> None:
    parts.watch.show(reason)
    control = parts.control()
    for request in (control.vm_state, control.other_running_vms):
        with pytest.raises(ActionError) as excinfo:
            await request()
        assert excinfo.value.outcome is outcome


@pytest.mark.parametrize(
    ("method", "request_text"),
    [
        ("start_vm", "vm-start"),
        ("shutdown_vm", "vm-shutdown"),
        ("force_stop_vm", "vm-force-stop"),
        ("power_off", "poweroff"),
    ],
)
async def test_actions_go_through_ssh_without_parameters(
    parts: Parts, method: str, request_text: str
) -> None:
    await getattr(parts.control(), method)()
    assert parts.requests() == [request_text]


@pytest.mark.parametrize("method", ["start_vm", "shutdown_vm", "force_stop_vm", "power_off"])
async def test_ssh_errors_become_outcomes(parts: Parts, method: str) -> None:
    parts.ssh_fake.behave(exit_code=255, stderr="root@x: Permission denied (publickey).\n")
    with pytest.raises(ActionError) as excinfo:
        await getattr(parts.control(), method)()
    assert excinfo.value.outcome is ActionOutcome.SSH_AUTH_FAILED


@pytest.mark.parametrize(
    ("refusal", "outcome"),
    [
        ("vm-running", ActionOutcome.VM_RUNNING),
        ("other-vms-running", ActionOutcome.OTHER_VMS_RUNNING),
        ("vm-state-unknown", ActionOutcome.VM_SERVICE_UNAVAILABLE),
    ],
)
async def test_guard_refuses_poweroff(parts: Parts, refusal: str, outcome: ActionOutcome) -> None:
    parts.ssh_fake.behave(exit_code=3, stderr=f"twinraid-guard: refused {refusal}\n")
    with pytest.raises(ActionError) as excinfo:
        await parts.control().power_off()
    assert excinfo.value.outcome is outcome


async def test_vm_command_without_power_conf(parts: Parts) -> None:
    parts.ssh_fake.behave(exit_code=3, stderr="twinraid-guard: refused vm-not-configured\n")
    with pytest.raises(ActionError) as excinfo:
        await parts.control().start_vm()
    assert excinfo.value.outcome is ActionOutcome.VM_NOT_CONFIGURED


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

    control = RealControl(CONFIG, parts.watch, parts.ssh, WOL, send_wol=broken)
    with pytest.raises(ActionError) as excinfo:
        await control.wake()
    assert excinfo.value.outcome is ActionOutcome.WOL_FAILED


async def test_target_reachable_informs_the_session(parts: Parts) -> None:
    control = parts.control()
    assert await control.target_reachable() is True
    parts.reachable = False
    assert await control.target_reachable() is False
    assert parts.watch.online_calls == [True, False]


async def test_other_running_vms(parts: Parts) -> None:
    control = parts.control()
    assert await control.other_running_vms() == ()
    parts.watch.show(report(others=("Test-VM",)))
    assert await control.other_running_vms() == ("Test-VM",)
    assert parts.ssh_fake.calls() == []


@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        ("LIBVIRT_DOWN", ActionOutcome.VM_SERVICE_UNAVAILABLE),
        ("UNKNOWN", ActionOutcome.VM_SERVICE_UNAVAILABLE),
        ("NOT_CONFIGURED", ActionOutcome.VM_NOT_CONFIGURED),
    ],
)
async def test_other_running_vms_needs_a_readable_list(
    parts: Parts, state: str, outcome: ActionOutcome
) -> None:
    parts.watch.show(report(state, others=None, name=None))
    with pytest.raises(ActionError) as excinfo:
        await parts.control().other_running_vms()
    assert excinfo.value.outcome is outcome
