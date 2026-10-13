"""Das Unraid-Template passt zu Konzept 5.4 und 6.1 und zum Container-Starttest."""

import re
import xml.etree.ElementTree as ET

from tests.conftest import REPO_ROOT

TEMPLATE = REPO_ROOT / "docker" / "unraid-template.xml"
SMOKE_TEST = REPO_ROOT / "docker" / "smoke-test.sh"


def _root() -> ET.Element:
    return ET.parse(TEMPLATE).getroot()  # noqa: S314 - eigene Datei im Repository


def _text(tag: str) -> str:
    element = _root().find(tag)
    assert element is not None, tag
    return (element.text or "").strip()


def test_image_and_network() -> None:
    assert _text("Repository") == "ghcr.io/mantua1/twinraid:latest"
    assert _text("Network") == "br0"
    assert _text("Privileged") == "false"
    assert _text("WebUI").startswith("https://")


def test_hardening_matches_the_start_test() -> None:
    params = _text("ExtraParams").split()
    smoke = SMOKE_TEST.read_text(encoding="utf-8")
    caps_line = re.search(r"^CAPS=\((.*)\)$", smoke, re.MULTILINE)
    assert caps_line is not None
    caps = caps_line.group(1).split()
    assert [p for p in params if p.startswith("--cap-add=")] == [f"--cap-add={c}" for c in caps]
    for option in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"):
        assert option in params
    assert "--tmpfs" in params
    assert "/run:rw,exec,nosuid,nodev,mode=0755,size=16m" in params
    assert "/tmp:rw,noexec,nosuid,nodev,size=16m" in params  # noqa: S108 - tmpfs-Option
    for forbidden in ("--privileged", "docker.sock", "--cap-add=ALL", "--cap-add=SYS_ADMIN"):
        assert forbidden not in _text("ExtraParams")


def test_volumes_and_mode() -> None:
    configs = {c.get("Target"): c for c in _root().iter("Config")}
    assert configs["/config"].get("Mode") == "ro"
    assert configs["/data"].get("Mode") == "rw"
    # Shares und Templates erst ab Phase 5 (Konzept 6.1).
    assert not any(target and target.startswith("/srv/worker") for target in configs)
    mode = configs["TWINRAID_MODE"]
    assert mode.get("Required") == "true"
    assert mode.get("Default") == "mock|real"
