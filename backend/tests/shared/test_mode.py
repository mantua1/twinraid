"""Betriebsmodus: Pflichtwert ohne Standard (Konzept 6.4)."""

import pytest

from twinraid.shared.mode import (
    REAL_ACTIONS_ENABLED,
    RunMode,
    RunModeError,
    parse_run_mode,
    resolve_run_mode,
)


def test_mock_is_accepted() -> None:
    assert resolve_run_mode({"TWINRAID_MODE": "mock"}) is RunMode.MOCK


@pytest.mark.parametrize("env", [{}, {"TWINRAID_MODE": ""}])
def test_missing_mode_is_an_error(env: dict[str, str]) -> None:
    with pytest.raises(RunModeError, match="TWINRAID_MODE is not set"):
        resolve_run_mode(env)


@pytest.mark.parametrize("value", ["MOCK", "Mock", " mock", "mock ", "simulation", "1", "true"])
def test_invalid_mode_is_an_error(value: str) -> None:
    with pytest.raises(RunModeError, match="invalid value"):
        resolve_run_mode({"TWINRAID_MODE": value})


def test_invalid_mode_message_lists_allowed_values() -> None:
    with pytest.raises(RunModeError) as excinfo:
        resolve_run_mode({"TWINRAID_MODE": "x"})
    assert "'mock', 'real'" in str(excinfo.value)


def test_very_long_value_is_truncated_in_message() -> None:
    with pytest.raises(RunModeError) as excinfo:
        resolve_run_mode({"TWINRAID_MODE": "x" * 500})
    assert len(str(excinfo.value)) < 200


def test_real_is_supported_read_only() -> None:
    """Seit Phase 3 ist real zugelassen, aber Aktionen bleiben gesperrt (Konzept 6.4)."""
    assert parse_run_mode({"TWINRAID_MODE": "real"}) is RunMode.REAL
    assert resolve_run_mode({"TWINRAID_MODE": "real"}) is RunMode.REAL
    assert REAL_ACTIONS_ENABLED is False
