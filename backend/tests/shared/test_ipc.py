"""Befehlsprotokoll: feste Liste, keine freien Parameter (Konzept 3.2)."""

import typing

import pytest

from twinraid.shared import ipc
from twinraid.shared.ipc import (
    COMMAND_NAMES,
    MAX_REQUEST_BYTES,
    ErrorCode,
    ErrorResponse,
    GetStatus,
    OkResponse,
    ProtocolError,
    SyncCancel,
    decode_command,
    decode_response,
    encode_command,
    encode_response,
)

CONCEPT_COMMANDS = {
    "get_status",
    "verify_password",
    "wake_target",
    "shutdown_target",
    "shutdown_target_force_vm",
    "vm_start",
    "vm_stop",
    "vm_force_stop",
    "sync_dry_run",
    "sync_start",
    "sync_cancel",
    "sync_confirm_deletions",
}
JOB_ID = "0123456789abcdef0123456789abcdef"


def _union_command_names() -> set[str]:
    union, _field = typing.get_args(ipc.Command)
    names: set[str] = set()
    for model in typing.get_args(union):
        (literal,) = typing.get_args(model.model_fields["command"].annotation)
        names.add(literal)
    return names


def test_command_list_matches_concept() -> None:
    assert COMMAND_NAMES == CONCEPT_COMMANDS
    assert _union_command_names() == CONCEPT_COMMANDS


def test_commands_have_no_free_parameters() -> None:
    union, _field = typing.get_args(ipc.Command)
    for model in typing.get_args(union):
        extra_fields = set(model.model_fields) - {"command"}
        if model is ipc.VerifyPassword:
            # Einzige Ausnahme laut Konzept 3.2: das zu prüfende Passwort.
            assert extra_fields == {"password"}
            continue
        # Erlaubt sind höchstens Job-IDs, die der Worker selbst vergibt.
        assert extra_fields <= {"job_id"}, model.__name__


def test_password_never_appears_in_repr() -> None:
    command = ipc.VerifyPassword(command="verify_password", password="geheim-123456")
    assert "geheim" not in repr(command)
    assert "geheim" not in str(command)


@pytest.mark.parametrize("name", sorted(CONCEPT_COMMANDS))
def test_every_command_roundtrips(name: str) -> None:
    payload = f'{{"command": "{name}"'
    if name in {"sync_cancel", "sync_confirm_deletions"}:
        payload += f', "job_id": "{JOB_ID}"'
    if name == "verify_password":
        payload += ', "password": "irgendwas-langes"'
    command = decode_command(payload.encode() + b"}\n")
    assert command.command == name
    assert decode_command(encode_command(command)) == command


@pytest.mark.parametrize(
    "line",
    [
        b'{"command": "rm_rf"}\n',
        b'{"command": "get_status", "path": "/etc"}\n',
        b'{"command": "get_status", "host": "10.0.0.1"}\n',
        b'{"command": "sync_cancel"}\n',
        b'{"command": "sync_cancel", "job_id": "../../etc/passwd"}\n',
        b'{"command": "sync_cancel", "job_id": "0123456789ABCDEF0123456789ABCDEF"}\n',
        b'{"command": "sync_start", "shares": ["system"]}\n',
        b'{"command": ["get_status"]}\n',
        b"not json\n",
        b"[]\n",
        b"\n",
    ],
)
def test_invalid_commands_are_rejected(line: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_command(line)


def test_command_must_end_with_newline() -> None:
    with pytest.raises(ProtocolError, match="newline"):
        decode_command(b'{"command": "get_status"}')


def test_command_size_limit() -> None:
    line = b'{"command": "get_status", "x": "' + b"a" * MAX_REQUEST_BYTES + b'"}\n'
    with pytest.raises(ProtocolError, match="too large"):
        decode_command(line)


def test_job_id_format() -> None:
    command = SyncCancel(command="sync_cancel", job_id=JOB_ID)
    assert command.job_id == JOB_ID


def test_responses_roundtrip() -> None:
    ok = OkResponse(result={"a": 1})
    error = ErrorResponse(error=ErrorCode.FORBIDDEN, message="no")
    assert decode_response(encode_response(ok)) == ok
    assert decode_response(encode_response(error)) == error


def test_get_status_encoding_is_compact_json_line() -> None:
    assert encode_command(GetStatus(command="get_status")) == b'{"command":"get_status"}\n'
