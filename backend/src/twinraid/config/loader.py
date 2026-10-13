"""Lädt twinraid.yaml sicher und meldet Fehler verständlich (Konzept 6.3)."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from twinraid.config.models import TwinRaidConfig

# Die Konfiguration ist klein; größere Dateien sind sicher ein Fehler.
MAX_CONFIG_BYTES = 256 * 1024


class ConfigError(Exception):
    """Die Konfiguration fehlt oder ist ungültig."""

    def __init__(self, path: Path, problems: list[str]) -> None:
        super().__init__(f"invalid configuration in {path}")
        self.path = path
        self.problems = problems

    def render(self) -> str:
        lines = [f"Invalid configuration in {self.path}:"]
        lines.extend(f"  - {problem}" for problem in self.problems)
        return "\n".join(lines)


def load_config(path: Path) -> TwinRaidConfig:
    text = _read_text(path)
    _check_structure(path, text)
    try:
        # Ausschließlich safe_load: keine Python-Objekte aus YAML-Tags.
        data: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(path, [f"YAML error: {exc}"]) from None
    if data is None:
        raise ConfigError(path, ["file is empty"])
    if not isinstance(data, dict):
        raise ConfigError(path, ["top level must be a mapping (key: value)"])
    try:
        return TwinRaidConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(path, _format_validation_errors(exc)) from None


def _read_text(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        raise ConfigError(path, ["file not found"]) from None
    except IsADirectoryError:
        raise ConfigError(path, ["path is a directory, expected a file"]) from None
    except PermissionError:
        raise ConfigError(path, ["file is not readable (permission denied)"]) from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError(path, [f"file is larger than {MAX_CONFIG_BYTES} bytes"])
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(path, ["file is not valid UTF-8"]) from None


def _check_structure(path: Path, text: str) -> None:
    """Lehnt doppelte Schlüssel sowie Anker/Aliase ab.

    PyYAML übernimmt bei doppelten Schlüsseln still den letzten Wert. compose()
    baut nur den Knotenbaum auf und erzeugt keine Python-Objekte.
    """
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(path, [f"YAML error: {exc}"]) from None
    if root is None:
        return
    problems: list[str] = []
    seen: set[int] = set()
    stack: list[yaml.Node] = [root]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            problems.append(
                f"line {node.start_mark.line + 1}: YAML anchors/aliases are not supported"
            )
            continue
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            keys: set[str] = set()
            for key_node, value_node in node.value:
                key = str(key_node.value)
                if key in keys:
                    problems.append(f"line {key_node.start_mark.line + 1}: duplicate key {key!r}")
                keys.add(key)
                stack.extend((key_node, value_node))
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
    if problems:
        raise ConfigError(path, sorted(problems))


def _format_location(loc: tuple[int | str, ...]) -> str:
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            parts.append(f"[{item}]")
        else:
            parts.append(f".{item}" if parts else item)
    return "".join(parts) or "(top level)"


def _format_validation_errors(exc: ValidationError) -> list[str]:
    problems: list[str] = []
    for error in exc.errors(include_url=False, include_input=False):
        location = _format_location(error["loc"])
        if error["type"] == "missing":
            message = "required key is missing"
        elif error["type"] == "extra_forbidden":
            message = "unknown key (check the spelling)"
        else:
            message = error["msg"].removeprefix("Value error, ")
        problems.append(f"{location}: {message}")
    return problems
