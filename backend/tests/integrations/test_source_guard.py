"""Status des Wächters lesen und auswerten (Konzept 3.6, „Lesen in TwinRaid“)."""

import copy
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import REPO_ROOT
from tests.support.guard_status import (
    GUID,
    HEARTBEAT,
    NOW,
    OTHER_ID,
    OWN_ID,
    STATUS,
    changed,
    guard_paths,
    mount_line,
    mountinfo,
    raw,
)
from twinraid.integrations.source_guard import (
    MAX_STATUS_BYTES,
    GuardObservation,
    GuardPaths,
    InvalidStatusError,
    RealSourceGuard,
    check_mounts,
    evaluate,
    own_container_id,
    parse_mountinfo,
    parse_status,
    read_observation,
)
from twinraid.shared.source_guard import (
    GuardBlocker,
    GuardCheckResult,
    GuardConfirmReason,
    GuardContainerState,
    GuardDatasetProblem,
    GuardDatasetResult,
    GuardError,
    GuardErrorDetail,
    GuardJobReason,
    GuardJobResult,
    GuardJobState,
    GuardOrder,
    GuardOrderResult,
    GuardRestart,
    GuardRunEnd,
    GuardSelfDataset,
    GuardSetupProblem,
    GuardState,
    GuardStatusProblem,
    GuardZfsState,
)
from twinraid.shared.status import StatusNotice

pytestmark = pytest.mark.anyio

SCRIPT = REPO_ROOT / "host-scripts" / "source-guard.sh"


def test_parse_mountinfo_unescapes_and_takes_the_topmost_mount() -> None:
    text = "\n".join(
        [
            mount_line(30, "/a", "/srv/worker/source-guard/state", "rw"),
            mount_line(31, "/b\\040c", "/srv/worker/source-guard/state", "ro,nosuid"),
            "garbage line",
        ]
    )
    mounts = parse_mountinfo(text)
    state = mounts["/srv/worker/source-guard/state"]
    assert state.root == "/b c"
    assert state.read_only is True


def test_own_container_id() -> None:
    text = mountinfo(Path("/srv/worker/source-guard"))
    assert own_container_id(text) == OWN_ID
    # Ohne Docker-Einbindungen oder mit zwei verschiedenen IDs: unbekannt.
    assert own_container_id(mount_line(20, "/", "/")) is None
    mixed = text + mount_line(24, f"/containers/{OTHER_ID}/hostname", "/etc/hostname") + "\n"
    assert own_container_id(mixed) is None
    # Ein Pfad, der nur so ähnlich aussieht, zählt nicht.
    assert own_container_id(mount_line(21, f"/x/{OWN_ID}/hostname", "/etc/hostname")) is None


@pytest.mark.parametrize(
    ("state", "request_mount", "problem"),
    [
        (None, "rw", GuardSetupProblem.STATE_NOT_MOUNTED),
        ("ro", None, GuardSetupProblem.REQUEST_NOT_MOUNTED),
        ("rw", "rw", GuardSetupProblem.STATE_WRITABLE),
        ("ro", "ro", GuardSetupProblem.REQUEST_READ_ONLY),
        ("ro", "rw", None),
    ],
)
def test_check_mounts(
    tmp_path: Path, state: str | None, request_mount: str | None, problem: GuardSetupProblem | None
) -> None:
    paths = guard_paths(tmp_path)
    mounts = parse_mountinfo(mountinfo(paths.base, state=state, request=request_mount))
    assert check_mounts(paths, mounts) is problem


def test_request_as_directory_or_symlink(tmp_path: Path) -> None:
    paths = guard_paths(tmp_path)
    mounts = parse_mountinfo(mountinfo(paths.base))
    paths.request.unlink()
    paths.request.mkdir()
    assert check_mounts(paths, mounts) is GuardSetupProblem.REQUEST_IS_DIRECTORY
    paths.request.rmdir()
    paths.request.symlink_to(tmp_path / "elsewhere")
    assert check_mounts(paths, mounts) is GuardSetupProblem.REQUEST_NOT_REGULAR
    paths.request.unlink()
    assert check_mounts(paths, mounts) is GuardSetupProblem.REQUEST_NOT_REGULAR


# --- status.json ---------------------------------------------------------------------------


def test_parse_valid_status() -> None:
    file = parse_status(raw(STATUS))
    assert file.heartbeat_at == HEARTBEAT
    assert file.allowed[1].id is None
    assert file.zfs.datasets[0].compression == "on"


def _problem(raw: bytes) -> GuardStatusProblem:
    with pytest.raises(InvalidStatusError) as caught:
        parse_status(raw)
    return caught.value.problem


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        (b"", GuardStatusProblem.BAD_JSON),
        (b"{", GuardStatusProblem.BAD_JSON),
        (b'{"protocol":1,"protocol":1}', GuardStatusProblem.BAD_JSON),
        (b'{"protocol":NaN}', GuardStatusProblem.BAD_JSON),
        ('{"name":"ä"}'.encode(), GuardStatusProblem.BAD_JSON),
        (b"[1]", GuardStatusProblem.SCHEMA),
        # Version 1 des Wächters (TwinRaid 0.5.0) schreibt Protokoll 1.
        (b'{"protocol":1}', GuardStatusProblem.PROTOCOL_MISMATCH),
        (b'{"protocol":3}', GuardStatusProblem.PROTOCOL_MISMATCH),
        (b'{"protocol":true}', GuardStatusProblem.SCHEMA),
        (b'{"protocol":2}', GuardStatusProblem.SCHEMA),
    ],
)
def test_status_syntax(raw: bytes, problem: GuardStatusProblem) -> None:
    assert _problem(raw) is problem


def test_duplicate_key_deep_inside_is_rejected() -> None:
    doubled = raw(STATUS).replace(b'"present":true', b'"present":true,"present":false')
    assert _problem(doubled) is GuardStatusProblem.BAD_JSON


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("version", "2"),
        ("version", 1),
        ("heartbeat_at", True),
        ("heartbeat_at", -1),
        ("heartbeat_at", 1.5),
        ("instance", "XYZ"),
        ("error", "unknown_error"),
        ("error_detail", "rm -rf"),
        ("last.order", "start"),
        ("last.result", "rejected"),
        ("last.container", "bad name"),
        ("host.connect", "maybe"),
        ("limits.max_stop_minutes", 500),
        ("limits.min_run_interval_minutes", 5),
        ("target.port", 0),
        ("allowed.0.name", "-dash"),
        ("allowed.0.name", "x" * 129),
        ("allowed.0.id", "abc"),
        ("allowed.0.state", "paused"),
        ("run.stopped.0.id", None),
        ("last_run.reason", "crash"),
        ("last_run.containers.0.result", "restarted"),
        ("zfs.state", "degraded"),
        ("zfs.datasets.0.name", "a/b"),
        ("zfs.datasets.0.compression", "LZ4"),
        ("zfs.self_dataset", "elsewhere"),
        ("seq_exhausted", 0),
        ("extra", 1),
        ("host.extra", 1),
        # Ab Protokoll 2: Ziel, check, Snapshots und Job.
        ("target.key", None),
        ("target.check.result", "ssh said: hello"),
        ("target.check.at", -1),
        ("target.check.summary.deleg", "id-in-use"),
        ("target.check.summary.reserve_percent", 100),
        ("target.check.mirrors.0.state", "mirrored"),
        ("target.check.mirrors.0.newest_guid", 123),
        ("target.check.mirrors.0.newest_guid", "12345678901234567890123"),
        ("target.check.mirrors.0.name", "../etc"),
        ("target.check.mirrors.0.extra", 1),
        ("zfs.datasets.0.base", {"guid": GUID}),
        ("zfs.datasets.0.base.guid", "abc"),
        ("zfs.datasets.0.bookmark", "yes"),
        ("zfs.datasets.0.snapshots", -1),
        ("job.id", 0),
        ("job.state", "running"),
        ("job.result", "maybe"),
        ("job.reason", "permission denied"),
        ("job.snapshot", "other-20261019T115900Z"),
        ("job.snapshot", "twinraid-20261019T115900Z@x"),
        ("job.confirmed", None),
        ("job.confirm_reasons", ["deletions", "many"]),
        ("job.send_ready", "true"),
        ("job.blockers", ["space_low", "rm -rf"]),
        ("job.datasets.0.mode", "partial"),
        ("job.datasets.0.base_type", "clone"),
        ("job.datasets.0.problem", "target-exists"),
        ("job.datasets.0.result", "done"),
        ("job.datasets.0.sent", -5),
        ("job.datasets.0.extra", None),
        ("job.extra", None),
    ],
)
def test_schema_rejects(path: str, value: Any) -> None:  # noqa: ANN401 - beliebiger JSON-Wert
    assert _problem(raw(changed(path, value))) is GuardStatusProblem.SCHEMA


def test_schema_rejects_missing_fields_and_long_lists() -> None:
    data = copy.deepcopy(STATUS)
    del data["zfs"]["self_dataset"]
    assert _problem(raw(data)) is GuardStatusProblem.SCHEMA
    data = copy.deepcopy(STATUS)
    data["allowed"] = [STATUS["allowed"][0]] * 51
    assert _problem(raw(data)) is GuardStatusProblem.SCHEMA
    data = copy.deepcopy(STATUS)
    data["zfs"]["datasets"] = [STATUS["zfs"]["datasets"][0]] * 21
    assert _problem(raw(data)) is GuardStatusProblem.SCHEMA
    data = copy.deepcopy(STATUS)
    data["job"]["datasets"] = [STATUS["job"]["datasets"][0]] * 21
    assert _problem(raw(data)) is GuardStatusProblem.SCHEMA
    data = copy.deepcopy(STATUS)
    data["target"]["check"]["mirrors"] = [STATUS["target"]["check"]["mirrors"][0]] * 21
    assert _problem(raw(data)) is GuardStatusProblem.SCHEMA
    data = copy.deepcopy(STATUS)
    del data["job"]
    assert _problem(raw(data)) is GuardStatusProblem.SCHEMA


def test_schema_accepts_empty_parts() -> None:
    """Ohne Job, ohne check und ohne Snapshots (gerade gestartet, Groß nie gefragt)."""
    data = changed("job", None)
    data["target"]["check"] = None
    data["zfs"]["datasets"][0].update(
        snapshots=None, oldest_snapshot_at=None, base=None, bookmark=None
    )
    assert parse_status(raw(data)).job is None
    unreachable = changed(
        "target.check", {"at": HEARTBEAT, "result": "unreachable", "summary": None, "mirrors": []}
    )
    file = parse_status(raw(unreachable))
    assert file.target.check is not None
    assert file.target.check.result is GuardCheckResult.UNREACHABLE


# --- Lesen ---------------------------------------------------------------------------------


def test_read_without_mounts(tmp_path: Path) -> None:
    paths = guard_paths(tmp_path, mount_line(20, "/", "/"))
    observation = read_observation(paths)
    assert observation.mounted is False
    assert evaluate(observation, NOW).status.state is GuardState.NOT_MOUNTED


def test_read_valid_status(tmp_path: Path) -> None:
    paths = guard_paths(tmp_path)
    paths.status_file.write_bytes(raw(STATUS))
    observation = read_observation(paths)
    assert observation.file is not None
    assert observation.own_id == OWN_ID
    assert observation.last_heartbeat == HEARTBEAT
    assert evaluate(observation, NOW).status.state is GuardState.ACTIVE


def test_read_missing_status_keeps_the_last_heartbeat(tmp_path: Path) -> None:
    paths = guard_paths(tmp_path)
    observation = read_observation(paths, last_heartbeat=HEARTBEAT - 100)
    report = evaluate(observation, NOW)
    assert report.status.state is GuardState.INACTIVE
    assert report.status.heartbeat_at == datetime.fromtimestamp(HEARTBEAT - 100, UTC)
    # Ohne früheren Herzschlag gibt es auch kein „seit“.
    assert evaluate(read_observation(paths), NOW).status.heartbeat_at is None


def _read_problem(paths: GuardPaths) -> GuardStatusProblem | None:
    return read_observation(paths).status_problem


def test_status_file_attacks(tmp_path: Path) -> None:
    paths = guard_paths(tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_bytes(raw(STATUS))
    paths.status_file.symlink_to(target)
    assert _read_problem(paths) is GuardStatusProblem.NOT_REGULAR
    paths.status_file.unlink()
    # Ein FIFO darf das Lesen nicht blockieren (O_NONBLOCK).
    os.mkfifo(paths.status_file)
    assert _read_problem(paths) is GuardStatusProblem.NOT_REGULAR
    paths.status_file.unlink()
    paths.status_file.mkdir()
    assert _read_problem(paths) is GuardStatusProblem.NOT_REGULAR
    paths.status_file.rmdir()
    paths.status_file.write_bytes(b" " * (MAX_STATUS_BYTES + 1))
    assert _read_problem(paths) is GuardStatusProblem.TOO_LARGE
    paths.status_file.write_bytes(raw(STATUS) + b" " * (MAX_STATUS_BYTES - len(raw(STATUS))))
    assert _read_problem(paths) is None
    paths.status_file.chmod(0)
    if os.geteuid() != 0:
        assert _read_problem(paths) is GuardStatusProblem.UNREADABLE


def test_invalid_status_keeps_the_last_heartbeat(tmp_path: Path) -> None:
    paths = guard_paths(tmp_path)
    paths.status_file.write_bytes(b"{")
    report = evaluate(read_observation(paths, last_heartbeat=HEARTBEAT), NOW)
    assert report.status.state is GuardState.INVALID_STATUS
    assert report.status.status_problem is GuardStatusProblem.BAD_JSON
    assert report.status.heartbeat_at == datetime.fromtimestamp(HEARTBEAT, UTC)
    assert report.status.details is None
    assert report.notices == ()


def test_misconfigured_wins_over_a_valid_status(tmp_path: Path) -> None:
    base = tmp_path / "source-guard"
    paths = guard_paths(tmp_path, mountinfo(base, state="rw"))
    paths.status_file.write_bytes(raw(STATUS))
    report = evaluate(read_observation(paths), NOW)
    assert report.status.state is GuardState.MISCONFIGURED
    assert report.status.setup_problem is GuardSetupProblem.STATE_WRITABLE
    # Die Daten eines frischen Status bleiben sichtbar.
    assert report.status.details is not None
    paths.status_file.unlink()
    assert evaluate(read_observation(paths), NOW).status.state is GuardState.MISCONFIGURED


def test_directory_case(tmp_path: Path) -> None:
    """Docker legte ein Verzeichnis an: Hinweis „Wächter starten, dann TwinRaid neu starten“."""
    paths = guard_paths(tmp_path)
    paths.request.unlink()
    paths.request.mkdir()
    status = evaluate(read_observation(paths), NOW).status
    assert status.state is GuardState.MISCONFIGURED
    assert status.setup_problem is GuardSetupProblem.REQUEST_IS_DIRECTORY


# --- Auswertung ----------------------------------------------------------------------------


def _observation(data: dict[str, Any], own_id: str | None = OWN_ID) -> GuardObservation:
    file = parse_status(raw(data))
    return GuardObservation(
        mounted=True, file=file, last_heartbeat=file.heartbeat_at, own_id=own_id
    )


def test_active_status_details() -> None:
    report = evaluate(_observation(STATUS), NOW)
    status = report.status
    assert status.state is GuardState.ACTIVE
    assert status.heartbeat_age_seconds == 4
    details = status.details
    assert details is not None
    assert [item.name for item in details.allowed] == ["twinraid-testcontainer", "stopped-one"]
    assert details.allowed[1].state is GuardContainerState.MISSING
    assert details.run is not None
    assert details.run.stopped == ("twinraid-testcontainer",)
    assert details.run.max_deadline == datetime.fromtimestamp(HEARTBEAT + 1140, UTC)
    assert details.last_run is not None
    assert details.last_run.reason is GuardRunEnd.DEADMAN_HEARTBEAT
    assert details.zfs.pool_available == 900
    assert details.target_configured is True
    assert details.rejected_count == 2
    assert report.notices == ()
    # Die ID des Containers geht nie an die Oberfläche.
    assert OTHER_ID not in status.model_dump_json()


@pytest.mark.parametrize(("offset", "state"), [(30, GuardState.ACTIVE), (31, GuardState.INACTIVE)])
def test_heartbeat_age_limit(offset: int, state: GuardState) -> None:
    data = changed("heartbeat_at", int(NOW.timestamp()) - offset)
    status = evaluate(_observation(data), NOW).status
    assert status.state is state
    if state is GuardState.INACTIVE:
        # Von einem veralteten Status bleibt nur der Zeitpunkt.
        assert status.details is None
        assert status.heartbeat_age_seconds is None
        assert status.heartbeat_at == datetime.fromtimestamp(int(NOW.timestamp()) - offset, UTC)


def test_heartbeat_in_the_future() -> None:
    soon = evaluate(_observation(changed("heartbeat_at", int(NOW.timestamp()) + 30)), NOW)
    assert soon.status.state is GuardState.ACTIVE
    assert soon.status.heartbeat_age_seconds == 0
    later = evaluate(_observation(changed("heartbeat_at", int(NOW.timestamp()) + 31)), NOW)
    assert later.status.state is GuardState.INVALID_STATUS
    assert later.status.status_problem is GuardStatusProblem.HEARTBEAT_FUTURE


def test_guard_error() -> None:
    data = changed("error", "config_invalid")
    data["error_detail"] = "bad_value"
    data["config_line"] = 7
    status = evaluate(_observation(data), NOW).status
    assert status.state is GuardState.ERROR
    assert status.error is GuardError.CONFIG_INVALID
    assert status.error_detail is GuardErrorDetail.BAD_VALUE
    assert status.config_line == 7
    assert status.details is not None
    # Die Zeile gilt nur für config_invalid.
    other = changed("error", "docker_unavailable")
    other["config_line"] = 3
    assert evaluate(_observation(other), NOW).status.config_line is None


def test_notices_only_from_a_fresh_status() -> None:
    data = changed("host", {"connect": "present", "flash_backup": "active"})
    data["allowed"][0]["id"] = OWN_ID
    fresh = evaluate(_observation(data), NOW)
    assert fresh.notices == (
        StatusNotice.SOURCE_CONNECT_PLUGIN,
        StatusNotice.SOURCE_FLASH_BACKUP,
        StatusNotice.SOURCE_GUARD_ALLOWS_SELF,
    )
    assert evaluate(_observation(data, own_id=None), NOW).notices == fresh.notices[:2]
    data["heartbeat_at"] = int(NOW.timestamp()) - 60
    assert evaluate(_observation(data), NOW).notices == ()


async def test_real_source_guard_logs_only_codes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    paths = guard_paths(tmp_path)
    now = [NOW]
    guard = RealSourceGuard(paths, clock=lambda: now[0])
    assert guard.report().status.state is GuardState.INACTIVE
    caplog.set_level(logging.INFO, logger="twinraid.integrations.source_guard")
    data = changed("error", "config_invalid")
    data["error_detail"] = "allow_contains_twinraid"
    paths.status_file.write_bytes(raw(data))
    await guard.refresh()
    await guard.refresh()
    assert guard.report().status.state is GuardState.ERROR
    paths.status_file.unlink()
    await guard.refresh()
    status = guard.report().status
    assert status.state is GuardState.INACTIVE
    assert status.heartbeat_at == datetime.fromtimestamp(HEARTBEAT, UTC)
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "source guard: error config_invalid allow_contains_twinraid",
        "source guard: inactive",
    ]
    assert "twinraid-testcontainer" not in caplog.text
    assert "twinraidtest" not in caplog.text


async def test_real_source_guard_survives_read_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = RealSourceGuard(guard_paths(tmp_path), clock=lambda: NOW)

    def broken(*_args: object) -> GuardObservation:
        raise RuntimeError("boom")

    monkeypatch.setattr("twinraid.integrations.source_guard.read_observation", broken)
    await guard.refresh()
    assert guard.report().status.state is GuardState.INACTIVE


async def test_evaluated_when_asked() -> None:
    """Das Alter des Herzschlags gilt für den Zeitpunkt von get_status, nicht des Lesens."""
    now = [NOW]
    guard = RealSourceGuard(GuardPaths(), clock=lambda: now[0])
    guard._observation = _observation(STATUS)
    assert guard.report().status.heartbeat_age_seconds == 4
    now[0] = NOW + timedelta(seconds=27)
    assert guard.report().status.state is GuardState.INACTIVE


# --- Feste Listen wie in source-guard.sh ---------------------------------------------------


def _script() -> str:
    return SCRIPT.read_text()


def _values(enum: type[StrEnum]) -> set[str]:
    return {item.value for item in enum}


def test_error_codes_match_the_script() -> None:
    text = _script()
    errors = set(re.findall(r"\b(?:CONFIG_ERROR|REQUEST_ERROR|ERROR)=([a-z_]+)", text))
    errors |= set(re.findall(r"layout_fail ([a-z_]+) [a-z_]+", text))
    assert errors == _values(GuardError)
    details = set(re.findall(r"config_fail ([a-z_]+)", text))
    details |= set(re.findall(r"layout_fail [a-z_]+ ([a-z_]+)", text))
    details |= set(re.findall(r"\b(?:REQUEST_DETAIL|RUNTIME_ERROR)=([a-z_]+)", text))
    assert details == _values(GuardErrorDetail)


def test_order_codes_match_the_script() -> None:
    text = _script()
    match = re.search(r"^readonly ORDER_RE='.*?\(\(([a-z|]+)\)\|stop ", text, re.MULTILINE)
    assert match is not None
    assert {*match.group(1).split("|"), "stop"} == _values(GuardOrder)
    results = set(re.findall(r"\bRESULT=([a-z_]+)", text))
    blocking = re.search(r"\n    ([a-z_ |]+)\)\n      RESULT=\"\$\{blocking\}\"", text)
    assert blocking is not None
    results |= {item.strip() for item in blocking.group(1).split("|")}
    assert results == _values(GuardOrderResult)


def test_run_codes_match_the_script() -> None:
    text = _script()
    assert set(re.findall(r"close_run ([a-z_]+)", text)) == _values(GuardRunEnd)
    assert set(re.findall(r"\bresult=([a-z_]+)", text)) == _values(GuardRestart)
    states = set(re.findall(r"ALLOWED_STATES\[i\]=([a-z_]+)", text))
    states |= set(re.findall(r"ALLOWED_STATES\+=\(([a-z_]+)\)", text))
    assert states == _values(GuardContainerState)


def test_zfs_codes_match_the_script() -> None:
    text = _script()
    assert set(re.findall(r"\bZFS_STATE=([a-z_]+)", text)) == _values(GuardZfsState)
    assert set(re.findall(r"\bSELF_DATASET_STATE=([a-z_]+)", text)) == _values(GuardSelfDataset)


def _function(text: str, name: str) -> str:
    start = text.index(f"\n{name}() {{\n")
    return text[start : text.index("\n}\n", start)]


def test_job_codes_match_the_script() -> None:
    text = _script()
    states = set(re.findall(r'\bJOB_STATE="?([a-z_]+)"?', text))
    assert states == _values(GuardJobState)
    results = set(re.findall(r"\bJOB_RESULT=([a-z_]+)", text))
    results |= set(re.findall(r"\bfallback=([a-z_]+)", text))
    assert results == _values(GuardJobResult)
    ssh_codes = set(re.findall(r"\bSSH_CODE=([a-z_]+)", text))
    refusals = re.search(r"^readonly SEND_REFUSALS=\(([^)]*)\)", text, re.MULTILINE)
    assert refusals is not None
    reasons = set(re.findall(r"\bJOB_REASON=([a-z_]+)", text)) | ssh_codes
    reasons |= {item.replace("-", "_") for item in refusals.group(1).split()}
    assert reasons == _values(GuardJobReason)


def test_check_codes_match_the_script() -> None:
    text = _script()
    results = set(re.findall(r"\bCHECK_RESULT=([a-z_]+)", text))
    results |= set(re.findall(r"\bSSH_CODE=([a-z_]+)", text))
    assert results == _values(GuardCheckResult)


def test_prepare_and_send_codes_match_the_script() -> None:
    text = _script()
    problems = set(re.findall(r"\bJD_PROBLEM\[i\]=([a-z_]+)", text))
    assert problems == _values(GuardDatasetProblem)
    blockers = set(re.findall(r"\bJOB_BLOCKERS\+=\(([a-z_]+)\)", text))
    blockers |= set(re.findall(r"add_unique JOB_BLOCKERS ([a-z_]+)", text))
    assert blockers | problems == _values(GuardBlocker)
    reasons = set(re.findall(r"add_unique JOB_CONFIRM_REASONS ([a-z_]+)", text))
    assert reasons == _values(GuardConfirmReason)
    send = _function(text, "child_send")
    results = set(re.findall(r"\bJD_RESULT\[i\]=([a-z_]+)", send))
    # JD_RESULT[i]="${JOB_RESULT}" übernimmt das Ergebnis des gescheiterten Datasets.
    assert 'JD_RESULT[i]="${JOB_RESULT}"' in send
    results |= set(re.findall(r"\bJOB_RESULT=([a-z_]+)", send)) - {"ok"}
    assert results == _values(GuardDatasetResult)


def test_details_of_protocol_2() -> None:
    """Ziel, check, Job und Snapshots je Dataset kommen aus dem Status; GUIDs nicht."""
    details = evaluate(_observation(STATUS), NOW).status.details
    assert details is not None
    assert details.instance == "0123456789abcdef"
    assert details.last_seq == 7
    assert details.last_order is not None
    assert (details.last_order.seq, details.last_order.result) == (7, GuardOrderResult.STOPPED)
    assert (details.target_key, details.target_known_host) == (True, True)
    dataset = details.zfs.datasets[0]
    assert dataset.snapshots == 4
    assert dataset.oldest_snapshot_at == datetime.fromtimestamp(HEARTBEAT - 500000, UTC)
    assert dataset.base_created_at == datetime.fromtimestamp(HEARTBEAT - 90000, UTC)
    assert dataset.bookmark is True
    check = details.target_check
    assert check is not None
    assert check.result is GuardCheckResult.OK
    assert check.summary is not None
    assert check.summary.available == 10**12
    assert [mirror.state for mirror in check.mirrors] == ["mirror", "absent"]
    assert check.mirrors[0].newest_received == datetime.fromtimestamp(HEARTBEAT - 89000, UTC)
    job = details.job
    assert job is not None
    assert job.open is True
    assert job.state is GuardJobState.AWAITING_CONFIRM
    assert job.confirm_reasons == (GuardConfirmReason.DELETIONS,)
    assert job.datasets[0].deleted == 1200
    assert job.datasets[1].mode == "full"
    # GUIDs braucht nur der Wächter; sie gelangen nie in den Status von TwinRaid.
    assert GUID not in details.model_dump_json()


def test_details_without_job_and_check() -> None:
    data = changed("job", None)
    data["target"]["check"] = None
    details = evaluate(_observation(data), NOW).status.details
    assert details is not None
    assert (details.job, details.target_check) == (None, None)
