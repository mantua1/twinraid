"""Namen weiterer laufender VMs auf Unraid Groß (Konzept 3.5 und 4.2, ab v0.4.2).

Läuft außer der Gaming-VM noch eine VM, schaltet TwinRaid Groß nicht aus. Die
Namen stammen von Groß (seit v0.4.3 hex-kodiert von guard.sh); sie werden
bereinigt, bevor sie in den Status gelangen, und nie geloggt.
"""

from collections.abc import Iterable

from twinraid.config.validators import has_control_chars

MAX_NAME_LENGTH = 64
MAX_NAMES = 20


def display_name(name: str) -> str:
    """Steuer- und unsichtbare Zeichen ersetzen, Länge begrenzen."""
    cleaned = "".join("?" if has_control_chars(ch) else ch for ch in name).strip() or "?"
    if len(cleaned) > MAX_NAME_LENGTH:
        cleaned = cleaned[: MAX_NAME_LENGTH - 1] + "…"
    return cleaned


def display_names(names: Iterable[str]) -> tuple[str, ...]:
    """Bereinigte, sortierte Namen (höchstens 20)."""
    return tuple(sorted(display_name(name) for name in names)[:MAX_NAMES])
