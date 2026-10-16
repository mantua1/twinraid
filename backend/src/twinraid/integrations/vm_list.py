"""Weitere laufende VMs auf Unraid Groß (Konzept 3.5 und 4.2, ab v0.4.2).

Läuft außer der Gaming-VM noch eine VM, schaltet TwinRaid Groß nicht aus. Die
Namen stammen aus der Antwort von Unraid; sie werden bereinigt, bevor sie in
den Status gelangen, und nie geloggt.
"""

from collections.abc import Mapping

from twinraid.config.validators import has_control_chars

# Ausgeschaltet ist nur SHUTOFF; alles andere zählt als laufend (auch pausiert,
# im Leerlauf, im Ruhezustand, abgestürzt oder unbekannt).
OFF_STATE = "SHUTOFF"
MAX_NAME_LENGTH = 64
MAX_NAMES = 20


def display_name(name: str) -> str:
    """Steuer- und unsichtbare Zeichen ersetzen, Länge begrenzen."""
    cleaned = "".join("?" if has_control_chars(ch) else ch for ch in name).strip() or "?"
    if len(cleaned) > MAX_NAME_LENGTH:
        cleaned = cleaned[: MAX_NAME_LENGTH - 1] + "…"
    return cleaned


def other_running_vms(states: Mapping[str, str], own_vm: str) -> tuple[str, ...]:
    """Bereinigte, sortierte Namen aller laufenden VMs außer der eigenen (höchstens 20)."""
    names = sorted(
        display_name(name)
        for name, state in states.items()
        if name != own_vm and state != OFF_STATE
    )
    return tuple(names[:MAX_NAMES])
