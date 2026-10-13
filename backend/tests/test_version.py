"""Die Version ist überall gleich (Konzept 5.8); der Release-Workflow prüft den Tag dagegen."""

import json
import re
import tomllib

from tests.conftest import REPO_ROOT
from twinraid import __version__

SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?")


_PEP440_PRE = {"alpha": "a", "beta": "b", "rc": "rc"}


def _python_form(version: str) -> str:
    """SemVer-Vorabversionen in der Schreibweise von PEP 440 (0.3.0-rc.1 → 0.3.0rc1)."""
    match = re.fullmatch(r"(.+)-(alpha|beta|rc)\.(\d+)", version)
    if match is None:
        return version
    return f"{match[1]}{_PEP440_PRE[match[2]]}{match[3]}"


def test_python_form() -> None:
    assert _python_form("0.3.0") == "0.3.0"
    assert _python_form("0.3.0-rc.1") == "0.3.0rc1"
    assert _python_form("1.0.0-beta.2") == "1.0.0b2"


def test_version_is_semver() -> None:
    assert SEMVER.fullmatch(__version__)


def test_versions_match() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "backend" / "pyproject.toml").read_text())
    package = json.loads((REPO_ROOT / "frontend" / "package.json").read_text())
    lock = json.loads((REPO_ROOT / "frontend" / "package-lock.json").read_text())
    assert package["version"] == __version__
    assert lock["version"] == __version__
    assert lock["packages"][""]["version"] == __version__
    assert pyproject["project"]["version"] == _python_form(__version__)
