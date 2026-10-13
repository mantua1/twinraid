"""Betriebsmodus (Konzept 6.4).

TWINRAID_MODE ist Pflicht. Es gibt bewusst keinen Standardwert: Fehlt der
Wert oder ist er ungültig, startet TwinRaid nicht.
"""

from collections.abc import Mapping
from enum import StrEnum

MODE_ENV_VAR = "TWINRAID_MODE"


class RunMode(StrEnum):
    MOCK = "mock"
    REAL = "real"


# Modi, die diese Version tatsächlich unterstützt. "real" kommt erst, wenn es
# echte Integrationen gibt (Phase 4).
SUPPORTED_MODES: frozenset[RunMode] = frozenset({RunMode.MOCK})

_ALLOWED_TEXT = ", ".join(f"'{mode.value}'" for mode in RunMode)
_MAX_ECHO_LENGTH = 32


class RunModeError(Exception):
    """Der Betriebsmodus fehlt, ist ungültig oder wird nicht unterstützt."""


def parse_run_mode(env: Mapping[str, str]) -> RunMode:
    """Liest den Modus streng aus der Umgebung: exakter Wert, kein Standard."""
    raw = env.get(MODE_ENV_VAR)
    if raw is None or raw == "":
        raise RunModeError(
            f"{MODE_ENV_VAR} is not set. It must be set explicitly to one of: "
            f"{_ALLOWED_TEXT}. Use 'mock' for simulation without real servers."
        )
    try:
        return RunMode(raw)
    except ValueError:
        shown = raw if len(raw) <= _MAX_ECHO_LENGTH else raw[:_MAX_ECHO_LENGTH] + "…"
        raise RunModeError(
            f"{MODE_ENV_VAR} has the invalid value {shown!r}. Allowed values (exact, "
            f"lowercase): {_ALLOWED_TEXT}."
        ) from None


def ensure_supported(mode: RunMode) -> None:
    """Lehnt Modi ab, die diese Version noch nicht umsetzt."""
    if mode not in SUPPORTED_MODES:
        raise RunModeError(
            f"{MODE_ENV_VAR}={mode.value} is not available in this version because real "
            f"integrations are not implemented yet. Use {MODE_ENV_VAR}=mock."
        )


def resolve_run_mode(env: Mapping[str, str]) -> RunMode:
    """Liest und prüft den Modus. Wirft RunModeError mit klarer Meldung."""
    mode = parse_run_mode(env)
    ensure_supported(mode)
    return mode
