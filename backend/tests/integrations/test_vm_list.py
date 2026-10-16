"""Weitere laufende VMs: Auswahl und bereinigte Namen (Konzept 3.5 und 4.2)."""

import pytest

from twinraid.integrations.vm_list import MAX_NAMES, display_name, other_running_vms


def test_only_shutoff_counts_as_off() -> None:
    states = {"Gaming": "RUNNING", "B": "SHUTOFF", "A": "PAUSED", "C": "CRASHED"}
    assert other_running_vms(states, "Gaming") == ("A", "C")


def test_own_vm_is_never_listed() -> None:
    assert other_running_vms({"Gaming": "RUNNING"}, "Gaming") == ()
    assert other_running_vms({}, "Gaming") == ()


def test_at_most_twenty_names_but_still_running() -> None:
    states = {f"VM {i:02d}": "RUNNING" for i in range(30)}
    names = other_running_vms(states, "Gaming")
    assert len(names) == MAX_NAMES
    assert names[0] == "VM 00"


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("Windows 11", "Windows 11"),
        ("Linux\nVM", "Linux?VM"),
        ("Böse\u202eVM", "Böse?VM"),
        ("Null\x00Byte", "Null?Byte"),
        ("   ", "?"),
        ("x" * 80, "x" * 63 + "…"),
    ],
)
def test_names_are_cleaned(raw: str, shown: str) -> None:
    assert display_name(raw) == shown
