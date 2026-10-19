"""Die festen Listen in frontend/src/api/types.ts stimmen mit dem Backend überein.

Geprüft werden Hinweise, Ursachen, Aktionen und die Codes des Wächters (Konzept 3.6): Fehlt
ein Code im Frontend, gibt es dort keinen Text; ein Code nur im Frontend wäre toter Text.
"""

import re
from enum import StrEnum

import pytest

from tests.conftest import REPO_ROOT
from twinraid.shared.actions import ActionBlocker, ActionName, ActionOutcome, ActionStep
from twinraid.shared.source_guard import (
    GuardContainerState,
    GuardError,
    GuardErrorDetail,
    GuardRestart,
    GuardRunEnd,
    GuardSelfDataset,
    GuardSetupProblem,
    GuardState,
    GuardStatusProblem,
)
from twinraid.shared.status import StatusNotice, StatusReason

TYPES = REPO_ROOT / "frontend" / "src" / "api" / "types.ts"


def _frontend_list(name: str) -> list[str]:
    match = re.search(
        rf"^export const {name} = \[(.*?)\] as const;", TYPES.read_text(), re.MULTILINE | re.DOTALL
    )
    assert match is not None, name
    return re.findall(r'"([a-z_]+)"', match.group(1))


@pytest.mark.parametrize(
    ("name", "enum"),
    [
        ("STATUS_NOTICES", StatusNotice),
        ("STATUS_REASONS", StatusReason),
        ("ACTION_NAMES", ActionName),
        ("ACTION_BLOCKERS", ActionBlocker),
        ("ACTION_STEPS", ActionStep),
        ("ACTION_OUTCOMES", ActionOutcome),
        ("GUARD_STATES", GuardState),
        ("GUARD_SETUP_PROBLEMS", GuardSetupProblem),
        ("GUARD_STATUS_PROBLEMS", GuardStatusProblem),
        ("GUARD_ERRORS", GuardError),
        ("GUARD_ERROR_DETAILS", GuardErrorDetail),
        ("GUARD_CONTAINER_STATES", GuardContainerState),
        ("GUARD_RUN_ENDS", GuardRunEnd),
        ("GUARD_RESTARTS", GuardRestart),
        ("GUARD_SELF_DATASETS", GuardSelfDataset),
    ],
)
def test_lists_match(name: str, enum: type[StrEnum]) -> None:
    assert _frontend_list(name) == [item.value for item in enum]
