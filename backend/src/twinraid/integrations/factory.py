"""Wählt je nach Betriebsmodus echte Integrationen oder Simulationen."""

from twinraid.integrations.mocks.status import MockStatusProvider
from twinraid.integrations.status import StatusProvider
from twinraid.shared.mode import RunMode, RunModeError


def build_status_provider(mode: RunMode) -> StatusProvider:
    if mode is RunMode.MOCK:
        return MockStatusProvider()
    # Echte Integrationen gibt es noch nicht; resolve_run_mode() lehnt "real"
    # bereits ab. Diese Zeile verhindert, dass später still simuliert wird.
    raise RunModeError(f"no status provider available for mode {mode.value!r}")
