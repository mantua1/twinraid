"""Weitere laufende VMs: bereinigte Namen (Konzept 3.5 und 4.2)."""

import pytest

from twinraid.integrations.vm_list import MAX_NAMES, display_name, display_names


def test_names_are_sorted() -> None:
    assert display_names(["Zweite", "Erste", "Ärger"]) == ("Erste", "Zweite", "Ärger")
    assert display_names([]) == ()


def test_at_most_twenty_names() -> None:
    names = display_names(f"VM {i:02d}" for i in range(30))
    assert len(names) == MAX_NAMES
    assert names[0] == "VM 00"


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("Windows 11", "Windows 11"),
        ("Linux\nVM", "Linux?VM"),
        ("Böse\u202eVM", "Böse?VM"),
        ("Null\x00Byte", "Null?Byte"),
        ("Ersatz\ufffdZeichen", "Ersatz\ufffdZeichen"),
        ("   ", "?"),
        ("x" * 80, "x" * 63 + "…"),
    ],
)
def test_names_are_cleaned(raw: str, shown: str) -> None:
    assert display_name(raw) == shown
