"""host-scripts/source-guard.sh Version 2: Snapshots, Job, Prüfen und Senden (Konzept 3.6).

Die Tests laufen mit einer Kopie des Skripts (tests/support/source_guard_host.py). ZFS,
ssh, ssh-keygen, ssh-keyscan und ip kommen aus tests/support/fake_source_host.py; die
Gegenseite antwortet wie guard.sh 3 mit festen Zeilen. Den Weg mit echtem ZFS und dem
echten guard.sh prüft docker/source-guard-sync-test.sh in der CI.
"""

import contextlib
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.support.fake_source_host import HOST_KEY, _fingerprint
from tests.support.source_guard_host import (
    DEFAULT_CONF,
    SCRIPT,
    Host,
    kill_leftovers,
    make_host,
)

GB = 10**9
TARGET = "192.0.2.11"
DS = "mainpool/twinraidtest"
BASE = "twinraid-20261001T000000Z"
BASE_GUID = "1111111111111111111"
CONF = DEFAULT_CONF + f"target_host {TARGET}\n"
SNAPSHOT_NAME = re.compile(r"^twinraid-[0-9]{8}T[0-9]{6}Z$")
ABSENT = (
    "state=absent props=- count=0 newest_guid=- newest_created=- newest_received=- oldest_created=-"
)
FOREIGN = (
    "state=foreign props=- count=- newest_guid=- newest_created=- newest_received=- "
    "oldest_created=-"
)
EMPTY_DATASET = {
    "mode": None,
    "base_guid": None,
    "base_type": None,
    "problem": None,
    "deleted": None,
    "modified": None,
    "added": None,
    "renamed": None,
    "referenced": None,
    "base_referenced": None,
    "size": None,
    "sent": None,
    "result": None,
}

Status = dict[str, Any]
Job = dict[str, Any]


def mirror(guid: str = BASE_GUID, *, props: str = "ok") -> str:
    return (
        f"state=mirror props={props} count=3 newest_guid={guid} newest_created=1760000000 "
        "newest_received=1760000100 oldest_created=1750000000"
    )


def check_answer(
    mirrors: dict[str, str] | None = None,
    *,
    version: int = 3,
    extra: tuple[str, ...] = (),
    summary: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Antwort von guard.sh sync check (Format in Konzept 3.7)."""
    lines = [f"twinraid-guard {version} sync ok"]
    for name, line in ({"twinraidtest": mirror()} if mirrors is None else mirrors).items():
        lines.append(f"mirror {name} {line}")
    values = {
        "root": "ok",
        "conf": "ok",
        "deleg": "ok",
        "receive": "idle",
        "clones": "0",
        "available": str(1000 * GB),
        "size": str(2000 * GB),
        "reserve_percent": "15",
        "keep_days": "30",
        "min_interval_minutes": "10",
    } | (summary or {})
    lines.append("summary " + " ".join(f"{key}={value}" for key, value in values.items()))
    lines.extend(extra)
    return {"rc": 0, "stdout": "\n".join(lines) + "\n"}


def snapshot(
    guid: str,
    *,
    created: int,
    txg: int,
    holds: tuple[str, ...] = (),
    referenced: int = 6000,
    props: dict[str, list[str]] | None = None,
    clones: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "guid": guid,
        "creation": created,
        "createtxg": txg,
        "props": {"twinraid:created-by": ["source-guard", "local"]} if props is None else props,
        "holds": list(holds),
        "clones": list(clones),
        "referenced": referenced,
        "changes": {},
        "delta": 500,
    }


def dataset(name: str, **values: Any) -> dict[str, Any]:  # noqa: ANN401
    entry: dict[str, Any] = {
        "type": "filesystem",
        "referenced": 5000,
        "compression": "on",
        "mountpoint": f"/mnt/{name}",
        "changes": {"-": 2, "M": 3, "+": 4, "R": 1},
        "delta": 700,
    }
    entry.update(values)
    return entry


def zfs_state() -> dict[str, Any]:
    """Pool mit twinraidtest, einer gehaltenen Basis und ihrem Lesezeichen."""
    now = int(time.time())
    return {
        "pool": "mainpool",
        "used": 100 * GB,
        "available": 900 * GB,
        "txg": 1000,
        "datasets": {
            DS: dataset(DS),
            "mainpool/appdata/twinraid": dataset(
                "mainpool/appdata/twinraid", mountpoint="/mnt/mainpool/appdata/twinraid"
            ),
        },
        "snapshots": {
            f"{DS}@{BASE}": snapshot(
                BASE_GUID, created=now - 3600, txg=900, holds=("twinraid-base",)
            )
        },
        "bookmarks": {f"{DS}#{BASE}": {"guid": BASE_GUID, "createtxg": 900}},
    }


def ssh_args(host: Host, port: str = "22") -> list[str]:
    """Die feste Argumentliste aus Konzept 3.6 („SSH“)."""
    return [
        "-F",
        "/dev/null",
        "-i",
        str(host.private / "ssh-sync"),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={host.known_hosts}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "HostKeyAlgorithms=ssh-ed25519",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=3",
        "-p",
        port,
        "-l",
        "root",
        "--",
        TARGET,
    ]


def setup(host: Host, conf: str = CONF) -> None:
    host.configure(conf)
    host.set_zfs(zfs_state())
    host.sync_key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n")
    host.known_hosts.write_text(f"{TARGET} ssh-ed25519 {HOST_KEY}\n")
    host.answer_ssh(check=check_answer(), receive={"default": {"rc": 0}})


def started(host: Host) -> Host:
    host.start_run()
    host.wait_status(lambda s: s["error"] is None and s["zfs"]["state"] == "ok")
    return host


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Callable[..., Host]]:
    """Host mit eigenen festen Werten (etwa kürzeren Fristen); ohne Start."""
    count = 0

    def build(**constants: str) -> Host:
        nonlocal count
        count += 1
        directory = tmp_path / f"host{count}"
        directory.mkdir()
        instance = make_host(directory, **constants)
        setup(instance)
        return instance

    yield build
    kill_leftovers(tmp_path)


@pytest.fixture
def host(make: Callable[..., Host]) -> Host:
    return make()


@pytest.fixture
def running(host: Host) -> Host:
    return started(host)


def job_of(host: Host, predicate: Callable[[Job], bool], timeout: float = 15) -> Job:
    status = host.wait_status(lambda s: s["job"] is not None and predicate(s["job"]), timeout)
    result: Job = status["job"]
    return result


def prepared(host: Host) -> Job:
    """snapshot und prepare; wartet auf das Ende von prepare."""
    assert host.send("snapshot")["result"] == "created"
    assert host.send("prepare")["result"] == "preparing"
    return job_of(host, lambda j: j["state"] not in ("snapshot", "preparing"))


def sent(host: Host) -> Job:
    assert host.send("send")["result"] == "sending"
    return job_of(host, lambda j: j["state"] not in ("sending",), timeout=30)


def new_name(job: Job, name: str = DS) -> str:
    return f"{name}@{job['snapshot']}"


def guard_processes(host: Host, word: str) -> list[int]:
    """Prozesse dieses Tests, deren Befehlszeile das Wort enthält."""
    marker = str(host.tmp).encode()
    found = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ")
            state = (proc / "stat").read_text().rsplit(") ", 1)[1][0]
        except OSError, IndexError:
            continue
        if marker in cmdline and word.encode() in cmdline and state != "Z":
            found.append(int(proc.name))
    return found


def wait_until(condition: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.05)


# --- snapshot -----------------------------------------------------------------------------


def test_snapshot_opens_a_job(running: Host) -> None:
    last = running.send("snapshot")
    assert last["result"] == "created"
    job = running.status()["job"]
    assert job["id"] == last["seq"]
    assert job["state"] == "snapshot"
    assert SNAPSHOT_NAME.match(job["snapshot"])
    # Name in UTC aus der Zeit der Erzeugung.
    assert job["snapshot"] == time.strftime(
        "twinraid-%Y%m%dT%H%M%SZ", time.gmtime(job["created_at"])
    )
    assert abs(job["created_at"] - time.time()) < 5
    # Ohne prepare oder close schließt der Wächter den Job nach 30 min.
    assert abs(job["deadline"] - (time.time() + 1800)) < 5
    assert job["result"] is None
    assert job["reason"] is None
    assert job["confirmed"] is False
    assert job["confirm_reasons"] == []
    assert job["send_ready"] is None
    assert job["blockers"] == []
    assert job["datasets"] == [{"name": "twinraidtest", **EMPTY_DATASET}]
    snapshots = [call for call in running.calls("zfs") if call[0] == "snapshot"]
    assert snapshots == [
        ["snapshot", "-o", "twinraid:created-by=source-guard", "--", new_name(job)]
    ]
    assert running.zfs()["snapshots"][new_name(job)]["props"] == {
        "twinraid:created-by": ["source-guard", "local"]
    }
    assert f"snapshot {job['snapshot']} created for 1 datasets (job {job['id']})" in (
        running.syslog()
    )


def test_one_snapshot_call_for_all_datasets(host: Host) -> None:
    host.configure(CONF + "dataset dokumente\n")
    host.update_zfs(lambda z: z["datasets"].update({"mainpool/dokumente": dataset("d")}))
    started(host)
    host.send("snapshot")
    job = host.status()["job"]
    assert [d["name"] for d in job["datasets"]] == ["twinraidtest", "dokumente"]
    calls = [call for call in host.calls("zfs") if call[0] == "snapshot"]
    assert calls == [
        [
            "snapshot",
            "-o",
            "twinraid:created-by=source-guard",
            "--",
            new_name(job),
            new_name(job, "mainpool/dokumente"),
        ]
    ]


def test_snapshot_during_a_run_and_prepare_after_it(running: Host) -> None:
    """Ablauf eines Syncs: begin, stop, snapshot, finish, dann prepare."""
    assert running.send("begin")["result"] == "opened"
    assert running.send("stop twinraid-testcontainer")["result"] == "stopped"
    assert running.send("snapshot")["result"] == "created"
    assert running.send("prepare")["result"] == "run_open"
    assert running.send("finish")["result"] == "finished"
    assert running.running("twinraid-testcontainer")
    assert running.send("prepare")["result"] == "preparing"
    job_of(running, lambda j: j["state"] == "prepared")


def test_begin_and_snapshot_are_refused_while_a_job_is_open(running: Host) -> None:
    running.send("snapshot")
    assert running.send("begin")["result"] == "job_open"
    assert running.send("snapshot")["result"] == "job_open"
    assert len([c for c in running.calls("zfs") if c[0] == "snapshot"]) == 1


def test_snapshot_without_zfs(host: Host) -> None:
    host.set_zfs(None)
    host.start_run()
    host.wait_status(lambda s: s["error"] is None)
    assert host.send("snapshot")["result"] == "zfs_unavailable"
    assert host.status()["job"] is None


@pytest.mark.parametrize(
    ("conf", "result"),
    [
        (DEFAULT_CONF.replace("dataset twinraidtest\n", ""), "no_datasets"),
        (CONF + "dataset missing\n", "dataset_missing"),
    ],
)
def test_snapshot_needs_existing_datasets(host: Host, conf: str, result: str) -> None:
    host.configure(conf)
    started(host)
    assert host.send("snapshot")["result"] == result
    assert host.status()["job"] is None
    assert [c for c in host.calls("zfs") if c[0] == "snapshot"] == []


def test_snapshot_needs_the_gap(make: Callable[..., Host]) -> None:
    host = started(make(SNAPSHOT_GAP="600"))
    assert host.send("snapshot")["result"] == "created"
    assert host.send("close")["result"] == "closed"
    assert host.send("snapshot")["result"] == "too_soon"
    # Der Abstand gilt auch nach einem Neustart des Wächters (private/last_snapshot).
    host.stop_run(host.processes[-1])
    host.start_run()
    assert host.send("snapshot")["result"] == "too_soon"


def test_snapshot_needs_the_space_reserve(host: Host) -> None:
    host.update_zfs(lambda z: z.update(used=86 * GB, available=14 * GB))
    started(host)
    assert host.send("snapshot")["result"] == "snapshot_low_space"
    host.update_zfs(lambda z: z.update(used=85 * GB, available=15 * GB))
    assert host.send("snapshot")["result"] == "created"


def test_snapshot_limit_counts_only_own_snapshots(host: Host) -> None:
    now = int(time.time())

    def fill(zfs: dict[str, Any]) -> None:
        for index in range(48):
            name = f"{DS}@twinraid-202610{index // 24 + 3:02d}T{index % 24:02d}0000Z"
            zfs["snapshots"][name] = snapshot(str(10**17 + index), created=now - 60, txg=index)
        # Fremde Snapshots zählen nicht: ohne Eigenschaft, Quelle received, anderer Name.
        zfs["snapshots"][f"{DS}@manual"] = snapshot("5", created=now, txg=950)
        zfs["snapshots"][f"{DS}@twinraid-20260101T000000Z"] = snapshot(
            "6", created=now, txg=951, props={"twinraid:created-by": ["source-guard", "received"]}
        )
        zfs["snapshots"][f"{DS}@twinraid-20260102T000000Z"] = snapshot(
            "7", created=now, txg=952, props={}
        )

    host.update_zfs(fill)
    started(host)
    # 48 eigene und die Basis: der 50. wird angelegt, der 51. nicht.
    assert host.send("snapshot")["result"] == "created"
    assert host.send("close")["result"] == "closed"
    assert host.send("snapshot")["result"] == "snapshot_limit"


def test_failed_snapshot_opens_no_job(host: Host) -> None:
    host.update_zfs(lambda z: z.update(fail=["snapshot"]))
    started(host)
    assert host.send("snapshot")["result"] == "snapshot_failed"
    assert host.status()["job"] is None
    assert not (host.private / "last_snapshot").exists()


# --- prepare ------------------------------------------------------------------------------


def test_prepare_needs_a_job_in_state_snapshot(running: Host) -> None:
    assert running.send("prepare")["result"] == "no_job"
    prepared(running)
    assert running.send("prepare")["result"] == "wrong_state"
    running.send("close")
    assert running.send("prepare")["result"] == "no_job"


def test_prepare_incremental_from_the_held_base(running: Host) -> None:
    job = prepared(running)
    new = new_name(job)
    assert job["state"] == "prepared"
    assert job["send_ready"] is True
    assert job["blockers"] == []
    assert job["confirm_reasons"] == []
    assert job["result"] is None
    assert abs(job["deadline"] - (time.time() + 1800)) < 5
    assert job["datasets"] == [
        {
            "name": "twinraidtest",
            "mode": "incremental",
            "base_guid": BASE_GUID,
            "base_type": "snapshot",
            "problem": None,
            "deleted": 2,
            "modified": 3,
            "added": 4,
            "renamed": 1,
            "referenced": 5000,
            "base_referenced": 6000,
            "size": 700,
            "sent": None,
            "result": None,
        }
    ]
    calls = running.calls("zfs")
    assert ["diff", "-H", "--", f"{DS}@{BASE}", new] in calls
    assert ["send", "-n", "-v", "-P", "-L", "-c", "-e", "-i", f"{DS}@{BASE}", "--", new] in calls
    assert running.calls("ssh") == [[*ssh_args(running), "check"]]
    key = running.private / "ssh-sync"
    assert key.stat().st_mode & 0o777 == 0o600
    check = running.status()["target"]["check"]
    assert check["result"] == "ok"
    assert abs(check["at"] - time.time()) < 10
    assert check["summary"] == {
        "root": "ok",
        "conf": "ok",
        "deleg": "ok",
        "receive": "idle",
        "clones": 0,
        "available": 1000 * GB,
        "size": 2000 * GB,
        "reserve_percent": 15,
        "keep_days": 30,
        "min_interval_minutes": 10,
    }
    assert check["mirrors"] == [
        {
            "name": "twinraidtest",
            "state": "mirror",
            "props": "ok",
            "count": 3,
            "newest_guid": BASE_GUID,
            "newest_created": 1760000000,
            "newest_received": 1760000100,
            "oldest_created": 1750000000,
        }
    ]
    assert running.status()["target"]["key"] is True
    assert running.status()["target"]["known_host"] is True


def test_prepare_first_stream_is_full(host: Host) -> None:
    host.answer_ssh(check=check_answer({"twinraidtest": ABSENT}))
    started(host)
    job = prepared(host)
    assert job["state"] == "prepared"
    assert job["send_ready"] is True
    assert job["datasets"][0] == {
        **EMPTY_DATASET,
        "name": "twinraidtest",
        "mode": "full",
        "referenced": 5000,
        "size": 5000,
    }
    estimates = [c for c in host.calls("zfs") if c[:2] == ["send", "-n"]]
    assert estimates == [["send", "-n", "-v", "-P", "-L", "-c", "-e", "--", new_name(job)]]
    assert [c for c in host.calls("zfs") if c[0] == "diff"] == []
    mirrors = host.status()["target"]["check"]["mirrors"]
    assert mirrors[0]["state"] == "absent"
    assert mirrors[0]["props"] is None
    assert mirrors[0]["count"] == 0
    assert mirrors[0]["newest_guid"] is None


def test_newest_matching_snapshot_is_the_base(host: Host) -> None:
    """Die GUID des neuesten Spiegel-Snapshots entscheidet, nicht der Hold."""
    now = int(time.time())
    later = "twinraid-20261002T000000Z"
    host.update_zfs(
        lambda z: z["snapshots"].update(
            {f"{DS}@{later}": snapshot("2222", created=now - 60, txg=950, referenced=5500)}
        )
    )
    host.answer_ssh(check=check_answer({"twinraidtest": mirror("2222")}))
    started(host)
    item = prepared(host)["datasets"][0]
    assert item["base_guid"] == "2222"
    assert item["base_type"] == "snapshot"
    assert item["base_referenced"] == 5500
    assert ["diff", "-H", "--", f"{DS}@{later}", new_name(host.status()["job"])] in (
        host.calls("zfs")
    )


@pytest.mark.parametrize(
    ("change", "reasons"),
    [
        ("deletions", ["deletions"]),
        ("at_the_limit", []),
        ("shrunk", ["shrunk"]),
        ("half", []),
        ("bookmark", ["no_diff"]),
    ],
)
def test_prepare_asks_for_confirmation(host: Host, change: str, reasons: list[str]) -> None:
    def adjust(zfs: dict[str, Any]) -> None:
        if change == "deletions":
            zfs["datasets"][DS]["changes"] = {"-": 6}
        elif change == "at_the_limit":
            zfs["datasets"][DS]["changes"] = {"-": 5}
        elif change == "shrunk":
            zfs["datasets"][DS]["referenced"] = 2999
        elif change == "half":
            zfs["datasets"][DS]["referenced"] = 3000
        else:
            # Basis-Snapshot fehlt, das Lesezeichen ist noch da.
            del zfs["snapshots"][f"{DS}@{BASE}"]

    host.configure(CONF + "max_deletions 5\n")
    host.update_zfs(adjust)
    started(host)
    job = prepared(host)
    assert job["confirm_reasons"] == reasons
    assert job["state"] == ("awaiting_confirm" if reasons else "prepared")
    assert job["send_ready"] is True
    if change == "bookmark":
        item = job["datasets"][0]
        assert item["base_type"] == "bookmark"
        assert item["base_guid"] == BASE_GUID
        assert item["deleted"] is None
        assert item["base_referenced"] is None
        assert item["size"] == 700
        assert [
            "send",
            "-n",
            "-v",
            "-P",
            "-L",
            "-c",
            "-e",
            "-i",
            f"{DS}#{BASE}",
            "--",
            new_name(job),
        ] in host.calls("zfs")


def test_deletions_are_summed_over_datasets(host: Host) -> None:
    host.configure(CONF + "dataset dokumente\nmax_deletions 5\n")
    now = int(time.time())

    def adjust(zfs: dict[str, Any]) -> None:
        zfs["datasets"][DS]["changes"] = {"-": 3}
        zfs["datasets"]["mainpool/dokumente"] = dataset("d", changes={"-": 3})
        zfs["snapshots"][f"mainpool/dokumente@{BASE}"] = snapshot(
            "3333", created=now - 60, txg=901, holds=("twinraid-base",)
        )

    host.update_zfs(adjust)
    host.answer_ssh(check=check_answer({"twinraidtest": mirror(), "dokumente": mirror("3333")}))
    started(host)
    job = prepared(host)
    assert [d["deleted"] for d in job["datasets"]] == [3, 3]
    assert job["confirm_reasons"] == ["deletions"]


def test_prepare_reports_problems_per_dataset(host: Host) -> None:
    host.configure(CONF + "dataset dokumente\ndataset medien\ndataset neu\n")
    host.update_zfs(
        lambda z: z["datasets"].update(
            {
                "mainpool/dokumente": dataset("d"),
                "mainpool/medien": dataset("m"),
                "mainpool/neu": dataset("n", referenced=42),
            }
        )
    )
    host.answer_ssh(
        check=check_answer({"twinraidtest": mirror("999"), "dokumente": FOREIGN, "neu": ABSENT})
    )
    started(host)
    job = prepared(host)
    assert job["state"] == "prepared"
    assert job["send_ready"] is False
    assert job["blockers"] == ["base_missing", "target_exists", "not_in_mirror_conf"]
    problems = {d["name"]: (d["mode"], d["problem"], d["size"]) for d in job["datasets"]}
    assert problems == {
        "twinraidtest": (None, "base_missing", None),
        "dokumente": (None, "target_exists", None),
        "medien": (None, "not_in_mirror_conf", None),
        "neu": ("full", None, 42),
    }


@pytest.mark.parametrize(
    ("summary", "mirror_line", "blockers"),
    [
        ({"root": "missing"}, mirror(), ["mirror_root_missing"]),
        ({"deleg": "mismatch"}, mirror(), ["delegation_mismatch"]),
        ({"deleg": "id-in-use"}, mirror(), ["delegation_mismatch"]),
        ({"receive": "busy"}, mirror(), ["receive_busy"]),
        ({"clones": "2"}, mirror(), ["clone_active"]),
        ({"clones": "?"}, mirror(), ["clone_active"]),
        ({"available": str(300 * GB)}, mirror(), ["space_low"]),
        ({"available": "?"}, mirror(), ["space_low"]),
        ({"reserve_percent": "-"}, mirror(), ["space_low"]),
        ({}, mirror(props="mismatch"), ["props_mismatch"]),
    ],
)
def test_prepare_blockers_from_check(
    host: Host, summary: dict[str, str], mirror_line: str, blockers: list[str]
) -> None:
    host.answer_ssh(check=check_answer({"twinraidtest": mirror_line}, summary=summary))
    started(host)
    job = prepared(host)
    assert job["state"] == "prepared"
    assert job["send_ready"] is False
    assert job["blockers"] == blockers
    if summary.get("deleg") == "id-in-use":
        assert host.status()["target"]["check"]["summary"]["deleg"] == "id_in_use"


def test_invalid_mirror_conf_on_gross(host: Host) -> None:
    """Bei conf=invalid gibt guard.sh keine Zeilen mirror aus."""
    answer = check_answer({}, summary={"conf": "invalid", "reserve_percent": "-", "keep_days": "-"})
    answer["stdout"] = answer["stdout"].replace("min_interval_minutes=10", "min_interval_minutes=-")
    host.answer_ssh(check=answer)
    started(host)
    job = prepared(host)
    assert job["blockers"] == ["mirror_conf_invalid", "not_in_mirror_conf", "space_low"]
    summary = host.status()["target"]["check"]["summary"]
    assert summary["keep_days"] is None
    assert summary["min_interval_minutes"] is None


@pytest.mark.parametrize(
    ("answer", "result"),
    [
        (
            {"rc": 255, "stderr": f"ssh: connect to host {TARGET} port 22: Connection refused\r\n"},
            "unreachable",
        ),
        (
            {"rc": 255, "stderr": f"ssh: connect to host {TARGET} port 22: No route to host\r\n"},
            "unreachable",
        ),
        (
            {
                "rc": 255,
                "stderr": "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\r\n",
            },
            "host_key_mismatch",
        ),
        (
            {
                "rc": 255,
                "stderr": f"No ED25519 host key is known for {TARGET} and you have requested "
                "strict checking.\r\nHost key verification failed.\r\n",
            },
            "host_untrusted",
        ),
        (
            {"rc": 255, "stderr": f"root@{TARGET}: Permission denied (publickey).\r\n"},
            "auth_failed",
        ),
        ({"rc": 2, "stderr": "twinraid-guard: denied\n"}, "denied"),
        ({"rc": 1, "stderr": "secret detail"}, "ssh_failed"),
        ({"rc": 255, "stderr": "something else"}, "ssh_failed"),
        (check_answer(version=2), "bad_answer"),
        ({"rc": 0, "stdout": "twinraid-guard 3 sync ok\n"}, "bad_answer"),
        (check_answer(extra=("mirror late " + ABSENT,)), "bad_answer"),
        (check_answer({"twinraidtest": mirror("1;touch /tmp/x")}), "bad_answer"),
        (check_answer({"twinraid test": ABSENT}), "bad_answer"),
        (check_answer({f"m{i}": ABSENT for i in range(21)}), "bad_answer"),
        (check_answer(summary={"clones": "1 extra=1"}), "bad_answer"),
        ({"rc": 0, "stdout": "evil\n"}, "bad_answer"),
    ],
)
def test_check_failures_block_sending(host: Host, answer: dict[str, Any], result: str) -> None:
    host.answer_ssh(check=answer)
    started(host)
    job = prepared(host)
    assert job["state"] == "prepared"
    assert job["send_ready"] is False
    assert job["blockers"] == ["target_unreachable"]
    assert job["datasets"][0] == {
        **EMPTY_DATASET,
        "name": "twinraidtest",
        "referenced": 5000,
    }
    check = host.status()["target"]["check"]
    assert check == {"at": check["at"], "result": result, "summary": None, "mirrors": []}
    text = host.status_file.read_text()
    for word in ("Permission", "secret", "touch", "evil", "WARNING"):
        assert word not in text


def test_check_timeout_is_unreachable(make: Callable[..., Host]) -> None:
    host = make(CHECK_TIMEOUT="1")
    host.answer_ssh(check={**check_answer(), "sleep": 5})
    started(host)
    started_at = time.monotonic()
    job = prepared(host)
    assert time.monotonic() - started_at < 5
    assert job["blockers"] == ["target_unreachable"]
    assert host.status()["target"]["check"]["result"] == "unreachable"
    wait_until(lambda: guard_processes(host, "/bin/ssh") == [])


@pytest.mark.parametrize("missing", ["key", "known_host", "target"])
def test_check_without_key_host_key_or_target(host: Host, missing: str) -> None:
    if missing == "key":
        host.sync_key.unlink()
    elif missing == "known_host":
        host.known_hosts.unlink()
    else:
        host.configure(DEFAULT_CONF)
    started(host)
    job = prepared(host)
    assert job["blockers"] == ["target_unreachable"]
    result = {
        "key": "key_missing",
        "known_host": "host_untrusted",
        "target": "target_not_configured",
    }
    assert host.status()["target"]["check"]["result"] == result[missing]
    assert host.calls("ssh") == []
    target = host.status()["target"]
    assert target["key"] is (missing != "key")
    assert target["known_host"] is (missing != "known_host")
    assert target["configured"] is (missing != "target")


def test_symlinked_key_is_not_used(host: Host) -> None:
    real = host.tmp / "elsewhere-key"
    real.write_text("key")
    host.sync_key.unlink()
    host.sync_key.symlink_to(real)
    started(host)
    prepared(host)
    assert host.status()["target"]["check"]["result"] == "key_missing"
    assert host.status()["target"]["key"] is False
    assert host.calls("ssh") == []


@pytest.mark.parametrize("failing", ["diff", "estimate", "referenced"])
def test_prepare_fails_on_klein(host: Host, failing: str) -> None:
    host.update_zfs(lambda z: z.update(fail=[failing]))
    started(host)
    job = prepared(host)
    assert job["state"] == "failed"
    assert job["result"] == "prepare_failed"
    assert job["deadline"] is None
    assert host.send("send")["result"] == "no_job"
    assert host.send("snapshot")["result"] == "created"


def test_target_port(host: Host) -> None:
    host.configure(CONF + "target_port 2222\n")
    started(host)
    prepared(host)
    assert host.calls("ssh") == [[*ssh_args(host, "2222"), "check"]]
    assert host.status()["target"]["port"] == 2222


# --- appdata und das eigene Dataset von TwinRaid (E18) ------------------------------------


def _with_appdata(host: Host) -> None:
    host.configure(CONF.replace("dataset twinraidtest", "dataset appdata"))
    host.update_zfs(lambda z: z["datasets"].update({"mainpool/appdata": dataset("a")}))
    host.answer_ssh(check=check_answer({"appdata": ABSENT}))


def test_appdata_with_own_dataset(host: Host) -> None:
    _with_appdata(host)
    started(host)
    job = prepared(host)
    assert job["datasets"][0]["problem"] is None
    assert job["send_ready"] is True


@pytest.mark.parametrize("change", ["outside", "no_container", "no_dataset"])
def test_appdata_without_own_dataset_is_blocked(host: Host, change: str) -> None:
    _with_appdata(host)
    if change == "outside":
        host.change_container(
            "TwinRaid",
            mounts=[
                {"Source": "/mnt/user/appdata/twinraid/config", "Destination": "/config"},
                {"Source": "/mnt/user/appdata/data", "Destination": "/data"},
            ],
        )
    elif change == "no_container":
        host.set_containers(*[c for c in host.containers() if c["name"] != "TwinRaid"])
    else:
        host.update_zfs(lambda z: z["datasets"].pop("mainpool/appdata/twinraid"))
    started(host)
    job = prepared(host)
    assert job["datasets"][0]["problem"] == "self_dataset_missing"
    assert job["blockers"] == ["self_dataset_missing"]
    assert job["send_ready"] is False


def test_appdata_is_checked_again_before_sending(host: Host) -> None:
    _with_appdata(host)
    started(host)
    assert prepared(host)["send_ready"] is True
    host.change_container(
        "TwinRaid",
        mounts=[
            {"Source": "/mnt/user/appdata/config", "Destination": "/config"},
            {"Source": "/mnt/user/appdata/twinraid/data", "Destination": "/data"},
        ],
    )
    job = sent(host)
    assert job["state"] == "failed"
    assert job["result"] == "send_failed"
    assert job["reason"] == "self_dataset_missing"
    assert job["datasets"][0]["result"] == "not_ready"
    assert host.received() == []


def test_other_datasets_do_not_need_the_own_dataset(host: Host) -> None:
    host.set_containers(*[c for c in host.containers() if c["name"] != "TwinRaid"])
    started(host)
    assert prepared(host)["send_ready"] is True


# --- confirm ------------------------------------------------------------------------------


def test_confirm(host: Host) -> None:
    host.configure(CONF + "max_deletions 1\n")
    started(host)
    assert host.send("confirm")["result"] == "no_job"
    host.send("snapshot")
    assert host.send("confirm")["result"] == "not_needed"
    host.send("prepare")
    job = job_of(host, lambda j: j["state"] == "awaiting_confirm")
    assert job["send_ready"] is True
    assert host.send("send")["result"] == "wrong_state"
    assert host.send("confirm")["result"] == "confirmed"
    job = host.status()["job"]
    assert job["state"] == "prepared"
    assert job["confirmed"] is True
    assert job["confirm_reasons"] == ["deletions"]
    assert host.send("confirm")["result"] == "not_needed"
    assert sent(host)["result"] == "ok"


# --- send ---------------------------------------------------------------------------------


def test_send_needs_a_prepared_job(host: Host) -> None:
    host.answer_ssh(check=check_answer({"twinraidtest": mirror("999")}))
    started(host)
    assert host.send("send")["result"] == "no_job"
    host.send("snapshot")
    assert host.send("send")["result"] == "wrong_state"
    host.send("prepare")
    job_of(host, lambda j: j["state"] == "prepared")
    assert host.send("send")["result"] == "not_ready"
    assert host.received() == []


def test_send_incremental_and_full(host: Host) -> None:
    host.configure(CONF + "dataset dokumente\n")
    host.update_zfs(
        lambda z: z["datasets"].update({"mainpool/dokumente": dataset("d", referenced=900)})
    )
    host.answer_ssh(check=check_answer({"twinraidtest": mirror(), "dokumente": ABSENT}))
    started(host)
    prepared(host)
    job = sent(host)
    new, other = new_name(job), new_name(job, "mainpool/dokumente")
    assert job["state"] == "done"
    assert job["result"] == "ok"
    assert job["reason"] is None
    assert job["deadline"] is None
    assert [(d["result"], d["sent"], d["size"]) for d in job["datasets"]] == [
        ("ok", 700, 700),
        ("ok", 900, 900),
    ]
    zfs = host.zfs()
    received = host.received()
    assert [(r["name"], r["header"]) for r in received] == [
        (
            "twinraidtest",
            f"FAKESTREAM {new} {DS}@{BASE} {zfs['snapshots'][new]['guid']} 700",
        ),
        ("dokumente", f"FAKESTREAM {other} - {zfs['snapshots'][other]['guid']} 900"),
    ]
    sends = [c for c in host.calls("zfs") if c[:2] == ["send", "-L"]]
    assert sends == [
        ["send", "-L", "-c", "-e", "-v", "-P", "-i", f"{DS}@{BASE}", "--", new],
        ["send", "-L", "-c", "-e", "-v", "-P", "--", other],
    ]
    args = ssh_args(host)
    assert host.calls("ssh") == [
        [*args, "check"],
        [*args, "receive twinraidtest"],
        [*args, "receive dokumente"],
    ]
    # Neue Basis: Hold und Lesezeichen am neuen Snapshot, der alte Hold ist frei.
    assert zfs["snapshots"][new]["holds"] == ["twinraid-base"]
    assert zfs["snapshots"][other]["holds"] == ["twinraid-base"]
    assert zfs["snapshots"][f"{DS}@{BASE}"]["holds"] == []
    assert zfs["bookmarks"][f"{DS}#{job['snapshot']}"]["guid"] == zfs["snapshots"][new]["guid"]
    assert ["hold", "--", "twinraid-base", new] in host.calls("zfs")
    assert ["bookmark", "--", new, f"{DS}#{job['snapshot']}"] in host.calls("zfs")
    assert ["release", "--", "twinraid-base", f"{DS}@{BASE}"] in host.calls("zfs")
    new_guid = zfs["snapshots"][new]["guid"]
    host.wait_status(lambda s: (s["zfs"]["datasets"][0]["base"] or {}).get("guid") == new_guid)
    log = host.syslog()
    assert "sending twinraidtest (incremental)" in log
    assert "sending dokumente (full)" in log
    assert "sent dokumente" in log
    assert f"job {job['id']}: done (ok)" in log


def test_send_shows_progress(host: Host) -> None:
    host.update_zfs(lambda z: z.update(send_seconds=4))
    started(host)
    prepared(host)
    assert host.send("send")["result"] == "sending"
    job = job_of(host, lambda j: j["state"] == "sending" and (j["datasets"][0]["sent"] or 0) > 0)
    assert 0 < job["datasets"][0]["sent"] < 700
    assert abs(job["deadline"] - (time.time() + 43200)) < 10
    assert job_of(host, lambda j: j["state"] == "done", timeout=20)["datasets"][0]["sent"] == 700


@pytest.mark.parametrize(
    ("answer", "result", "reason"),
    [
        ({"rc": 3, "stderr": "twinraid-guard: refused space-low\n"}, "target_refused", "space_low"),
        (
            {"rc": 3, "stderr": "twinraid-guard: refused props-not-applied\n"},
            "target_refused",
            "props_not_applied",
        ),
        (
            {"rc": 3, "stderr": "twinraid-guard: refused received-properties\n"},
            "target_refused",
            "received_properties",
        ),
        (
            {"rc": 3, "stderr": "twinraid-guard: refused target-modified\n"},
            "target_modified",
            None,
        ),
        (
            {"rc": 3, "stderr": "twinraid-guard: refused something-new\n"},
            "target_refused",
            "refused_other",
        ),
        (
            {"rc": 255, "stderr": f"ssh: connect to host {TARGET} port 22: Connection refused\r\n"},
            "send_failed",
            "unreachable",
        ),
        ({"rc": 2, "stderr": "twinraid-guard: denied\n"}, "send_failed", "denied"),
        ({"rc": 1, "stderr": "cannot receive: secret detail\n"}, "send_failed", "ssh_failed"),
    ],
)
def test_send_refused_or_failed(
    host: Host, answer: dict[str, Any], result: str, reason: str | None
) -> None:
    host.configure(CONF + "dataset dokumente\n")
    host.update_zfs(lambda z: z["datasets"].update({"mainpool/dokumente": dataset("d")}))
    host.answer_ssh(
        check=check_answer({"twinraidtest": mirror(), "dokumente": ABSENT}),
        receive={"twinraidtest": answer},
    )
    started(host)
    prepared(host)
    job = sent(host)
    assert job["state"] == "failed"
    assert job["result"] == result
    assert job["reason"] == reason
    assert [d["result"] for d in job["datasets"]] == [result, "skipped"]
    assert [r["name"] for r in host.received()] == ["twinraidtest"]
    # Keine neue Basis: der alte Hold bleibt, kein Lesezeichen für den neuen Snapshot.
    zfs = host.zfs()
    assert zfs["snapshots"][f"{DS}@{BASE}"]["holds"] == ["twinraid-base"]
    assert zfs["snapshots"][new_name(job)]["holds"] == []
    assert f"{DS}#{job['snapshot']}" not in zfs["bookmarks"]
    assert "secret" not in host.status_file.read_text()
    assert not any("secret" in line for line in host.syslog())


def test_second_dataset_fails_after_the_first_was_sent(host: Host) -> None:
    host.configure(CONF + "dataset dokumente\n")
    host.update_zfs(lambda z: z["datasets"].update({"mainpool/dokumente": dataset("d")}))
    host.answer_ssh(
        check=check_answer({"twinraidtest": mirror(), "dokumente": ABSENT}),
        receive={"dokumente": {"rc": 3, "stderr": "twinraid-guard: refused clone-active\n"}},
    )
    started(host)
    prepared(host)
    job = sent(host)
    assert [d["result"] for d in job["datasets"]] == ["ok", "target_refused"]
    assert job["reason"] == "clone_active"
    # Das gesendete Dataset behält seine neue Basis.
    assert host.zfs()["snapshots"][new_name(job)]["holds"] == ["twinraid-base"]


def test_send_error_on_klein(host: Host) -> None:
    host.update_zfs(lambda z: z.update(fail=["send"], send_seconds=0.4))
    started(host)
    prepared(host)
    job = sent(host)
    assert (job["result"], job["reason"]) == ("send_failed", "send_error")
    assert job["datasets"][0]["result"] == "send_failed"


def test_base_gone_before_sending(host: Host) -> None:
    started(host)
    prepared(host)
    host.update_zfs(lambda z: z["snapshots"][f"{DS}@{BASE}"].update(guid="4444"))
    job = sent(host)
    assert (job["result"], job["reason"]) == ("send_failed", "base_missing")
    assert job["datasets"][0]["result"] == "base_missing"
    assert host.received() == []


def test_key_gone_before_sending(host: Host) -> None:
    started(host)
    prepared(host)
    host.sync_key.unlink()
    job = sent(host)
    assert (job["result"], job["reason"]) == ("send_failed", "key_missing")
    assert host.received() == []


# --- close, Fristen und Kindprozess -------------------------------------------------------


def test_close(running: Host) -> None:
    assert running.send("close")["result"] == "no_job"
    running.send("snapshot")
    assert running.send("close")["result"] == "closed"
    job = running.status()["job"]
    assert (job["state"], job["result"], job["deadline"]) == ("closed", "closed", None)
    # Der Snapshot bleibt und läuft nach snapshot_keep_days ab.
    assert new_name(job) in running.zfs()["snapshots"]
    assert running.send("close")["result"] == "no_job"
    assert running.send("snapshot")["result"] == "created"


def test_close_ends_a_transfer(host: Host) -> None:
    host.update_zfs(lambda z: z.update(send_seconds=60))
    started(host)
    prepared(host)
    host.send("send")
    job_of(host, lambda j: (j["datasets"][0]["sent"] or 0) > 0)
    assert guard_processes(host, " child send ") != []
    assert host.send("close")["result"] == "closed"
    wait_until(lambda: guard_processes(host, " child ") == [], timeout=5)
    wait_until(lambda: guard_processes(host, "bin/zfs") == [], timeout=5)
    wait_until(lambda: guard_processes(host, "bin/ssh") == [], timeout=5)
    time.sleep(1.5)
    job = host.status()["job"]
    assert (job["state"], job["result"]) == ("closed", "closed")
    assert host.zfs()["snapshots"][f"{DS}@{BASE}"]["holds"] == ["twinraid-base"]


def test_job_timeout_without_prepare(make: Callable[..., Host]) -> None:
    host = started(make(CONFIRM_LIMIT="2"))
    host.send("snapshot")
    job = job_of(host, lambda j: j["state"] == "closed", timeout=8)
    assert (job["result"], job["deadline"]) == ("job_timeout", None)
    assert f"job {job['id']}: closed (job_timeout)" in host.syslog()


def test_job_timeout_while_preparing(make: Callable[..., Host]) -> None:
    host = make(PREPARE_LIMIT="2", CHECK_TIMEOUT="60")
    host.answer_ssh(check={**check_answer(), "sleep": 30})
    started(host)
    host.send("snapshot")
    host.send("prepare")
    job = job_of(host, lambda j: j["state"] == "closed", timeout=10)
    assert job["result"] == "job_timeout"
    wait_until(lambda: guard_processes(host, " child ") == [], timeout=5)
    wait_until(lambda: guard_processes(host, "bin/ssh") == [], timeout=5)


def test_job_timeout_while_waiting_for_confirm(make: Callable[..., Host]) -> None:
    host = make(CONFIRM_LIMIT="3")
    host.configure(CONF + "max_deletions 1\n")
    started(host)
    host.send("snapshot")
    host.send("prepare")
    job_of(host, lambda j: j["state"] == "awaiting_confirm")
    job = job_of(host, lambda j: j["state"] == "closed", timeout=10)
    assert job["result"] == "job_timeout"


def test_job_timeout_while_sending(make: Callable[..., Host]) -> None:
    host = make(SEND_LIMIT="2")
    host.update_zfs(lambda z: z.update(send_seconds=60))
    started(host)
    prepared(host)
    host.send("send")
    job = job_of(host, lambda j: j["state"] == "closed", timeout=10)
    assert job["result"] == "job_timeout"
    wait_until(lambda: guard_processes(host, "bin/zfs") == [], timeout=5)


def test_restart_while_sending_interrupts_the_job(host: Host) -> None:
    host.update_zfs(lambda z: z.update(send_seconds=60))
    started(host)
    prepared(host)
    host.send("send")
    job_of(host, lambda j: (j["datasets"][0]["sent"] or 0) > 0)
    host.stop_run(host.processes[-1])
    job = host.status()["job"]
    assert (job["state"], job["result"]) == ("failed", "interrupted")
    assert guard_processes(host, " child ") == []
    wait_until(lambda: guard_processes(host, "bin/zfs") == [], timeout=5)
    host.start_run()
    job = host.status()["job"]
    assert (job["state"], job["result"]) == ("failed", "interrupted")
    assert host.zfs()["snapshots"][f"{DS}@{BASE}"]["holds"] == ["twinraid-base"]


def test_restart_keeps_a_prepared_job(host: Host) -> None:
    started(host)
    before = prepared(host)
    host.stop_run(host.processes[-1])
    host.start_run()
    job = host.status()["job"]
    # Die Frist läuft nach Laufzeit weiter; als Uhrzeit kann sie um eine Sekunde abweichen.
    assert abs(job.pop("deadline") - before.pop("deadline")) <= 1
    assert job == before
    assert sent(host)["result"] == "ok"


def test_killed_child_fails_the_job(host: Host) -> None:
    host.update_zfs(lambda z: z.update(send_seconds=60))
    started(host)
    prepared(host)
    host.send("send")
    job_of(host, lambda j: (j["datasets"][0]["sent"] or 0) > 0)
    (child,) = [pid for pid in guard_processes(host, " child send ") if _is_group_leader(pid)]
    os.kill(child, signal.SIGKILL)
    job = job_of(host, lambda j: j["state"] == "failed")
    assert job["result"] == "send_failed"
    # Reste der Prozessgruppe enden mit ihm.
    wait_until(lambda: guard_processes(host, "bin/zfs") == [], timeout=5)
    wait_until(lambda: guard_processes(host, "bin/ssh") == [], timeout=5)


def _is_group_leader(pid: int) -> bool:
    return os.getpgid(pid) == pid


@pytest.mark.parametrize("phase", ["sending", "preparing"])
def test_child_ends_when_the_guard_is_killed(make: Callable[..., Host], phase: str) -> None:
    """Auch ssh unter timeout (eigene Prozessgruppe) endet mit dem Kindprozess."""
    host = make(CHECK_TIMEOUT="60")
    if phase == "sending":
        host.update_zfs(lambda z: z.update(send_seconds=60))
        started(host)
        prepared(host)
        host.send("send")
        job_of(host, lambda j: (j["datasets"][0]["sent"] or 0) > 0)
    else:
        host.answer_ssh(check={**check_answer(), "sleep": 60})
        started(host)
        host.send("snapshot")
        host.send("prepare")
        wait_until(lambda: guard_processes(host, "bin/ssh") != [])
    guard = host.processes[-1]
    guard.kill()
    guard.wait(timeout=5)
    wait_until(lambda: guard_processes(host, " child ") == [], timeout=6)
    wait_until(lambda: guard_processes(host, "bin/zfs") == [], timeout=6)
    wait_until(lambda: guard_processes(host, "bin/ssh") == [], timeout=6)
    host.start_run()
    job = host.wait_status(lambda s: s["job"]["state"] == "failed")["job"]
    assert job["result"] == "interrupted"


def test_stale_child_is_stopped_at_start(host: Host) -> None:
    """Nur ein Prozess mit " child " in der Befehlszeile; sonst wäre die PID wiederverwendet."""
    host.private.mkdir(parents=True)
    stale = subprocess.Popen(  # noqa: S603 - feste Argumente, nur im Test
        ["/bin/bash", "-c", f'exec -a "{host.tmp}/x child send" sleep 60'],
        start_new_session=True,
    )
    other = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    try:
        (host.private / "child.pid").write_text(str(stale.pid))
        host.start_run()
        assert stale.wait(timeout=5) == -signal.SIGKILL
        assert not (host.private / "child.pid").exists()
        host.stop_run(host.processes[-1])
        (host.private / "child.pid").write_text(str(other.pid))
        host.start_run()
        time.sleep(0.5)
        assert other.poll() is None
    finally:
        for process in (stale, other):
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            process.wait()


# --- Aufbewahrung auf Klein ---------------------------------------------------------------


def _old_snapshots(now: int) -> dict[str, dict[str, Any]]:
    old = now - 8 * 86400
    return {
        f"{DS}@twinraid-20260901T000000Z": snapshot("71", created=old, txg=801),
        f"{DS}@twinraid-20260902T000000Z": snapshot("72", created=old, txg=802, holds=("keep",)),
        f"{DS}@twinraid-20260903T000000Z": snapshot(
            "73", created=old, txg=803, clones=("mainpool/klon",)
        ),
        f"{DS}@twinraid-20260904T000000Z": snapshot("74", created=old, txg=804, props={}),
        f"{DS}@twinraid-20260905T000000Z": snapshot(
            "75", created=old, txg=805, props={"twinraid:created-by": ["source-guard", "inherited"]}
        ),
        f"{DS}@manual-20260906": snapshot("76", created=old, txg=806),
        f"{DS}@twinraid-20260920T000000Z": snapshot("77", created=now - 6 * 86400, txg=807),
    }


def test_retention_at_start(host: Host) -> None:
    now = int(time.time())
    host.update_zfs(lambda z: z["snapshots"].update(_old_snapshots(now)))
    started(host)
    wait_until(lambda: f"{DS}@twinraid-20260901T000000Z" not in host.zfs()["snapshots"])
    destroys = [c for c in host.calls("zfs") if c[0] == "destroy"]
    assert destroys == [["destroy", "--", f"{DS}@twinraid-20260901T000000Z"]]
    remaining = set(host.zfs()["snapshots"])
    assert set(_old_snapshots(now)) - remaining == {f"{DS}@twinraid-20260901T000000Z"}
    assert f"{DS}@{BASE}" in remaining
    assert "retention: 1 own snapshots destroyed, 0 failed" in host.syslog()


def test_retention_after_sending(host: Host) -> None:
    started(host)
    now = int(time.time())
    host.update_zfs(lambda z: z["snapshots"].update(_old_snapshots(now)))
    prepared(host)
    assert f"{DS}@twinraid-20260901T000000Z" in host.zfs()["snapshots"]
    sent(host)
    snapshots = host.zfs()["snapshots"]
    assert f"{DS}@twinraid-20260901T000000Z" not in snapshots
    # Die alte Basis ist jetzt ohne Hold, aber jünger als snapshot_keep_days.
    assert f"{DS}@{BASE}" in snapshots


def test_daily_retention_waits_for_the_job(make: Callable[..., Host]) -> None:
    host = started(make(RETENTION_INTERVAL="1"))
    host.send("snapshot")
    now = int(time.time())
    host.update_zfs(lambda z: z["snapshots"].update(_old_snapshots(now)))
    time.sleep(2.5)
    assert f"{DS}@twinraid-20260901T000000Z" in host.zfs()["snapshots"]
    host.send("close")
    wait_until(lambda: f"{DS}@twinraid-20260901T000000Z" not in host.zfs()["snapshots"])


def test_failed_destroy_is_counted(host: Host) -> None:
    def change(zfs: dict[str, Any]) -> None:
        zfs["snapshots"].update(_old_snapshots(int(time.time())))
        zfs["fail"] = ["destroy"]

    host.update_zfs(change)
    started(host)
    wait_until(lambda: "retention: 0 own snapshots destroyed, 1 failed" in host.syslog())


# --- Status: Snapshots, Basis und Lesezeichen ---------------------------------------------


def test_status_shows_snapshots_base_and_bookmark(host: Host) -> None:
    now = int(time.time())
    created = host.zfs()["snapshots"][f"{DS}@{BASE}"]["creation"]
    host.update_zfs(
        lambda z: z["snapshots"].update(
            {
                f"{DS}@twinraid-20261002T000000Z": snapshot("8", created=now - 60, txg=950),
                f"{DS}@manual": snapshot("9", created=now - 9000, txg=899, props={}),
            }
        )
    )
    started(host)
    item = host.status()["zfs"]["datasets"][0]
    assert item["snapshots"] == 2
    assert item["oldest_snapshot_at"] == created
    assert item["base"] == {"guid": BASE_GUID, "created_at": created}
    assert item["bookmark"] is True


def test_status_without_hold_has_no_base(host: Host) -> None:
    def change(zfs: dict[str, Any]) -> None:
        zfs["snapshots"][f"{DS}@{BASE}"]["holds"] = ["other"]
        zfs["bookmarks"].clear()

    host.update_zfs(change)
    started(host)
    item = host.status()["zfs"]["datasets"][0]
    assert item["base"] is None
    assert item["bookmark"] is False
    assert item["snapshots"] == 1


# --- init-key und trust-target ------------------------------------------------------------


def test_init_key(host: Host) -> None:
    host.sync_key.unlink()
    result = host.command("init-key")
    assert result.returncode == 0, result.stderr
    assert "Der Key sync wurde angelegt" in result.stdout
    public = host.sync_key.with_name("ssh-sync.pub").read_text().strip()
    line = (
        'restrict,from="192.168.10.10",command="/bin/bash /boot/config/twinraid/guard.sh sync" '
        f"{public}"
    )
    assert result.stdout.splitlines()[-1] == line
    assert public.endswith(" twinraid-sync")
    assert host.calls("ssh-keygen") == [
        ["-q", "-t", "ed25519", "-N", "", "-C", "twinraid-sync", "-f", str(host.sync_key)]
    ]
    assert host.calls("ip") == [["-4", "route", "get", TARGET]]
    again = host.command("init-key")
    assert "schon vorhanden" in again.stdout
    assert again.stdout.splitlines()[-1] == line
    assert len(host.calls("ssh-keygen")) == 1
    renewed = host.command("init-key", "--new")
    assert renewed.returncode == 0
    assert renewed.stdout.splitlines()[-1] != line
    assert len(host.calls("ssh-keygen")) == 2


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("no_target", "target_host fehlt"),
        ("unresolvable", "lässt sich nicht auflösen"),
        ("no_route", "die eigene Adresse"),
        ("keygen_fails", "ssh-keygen ist gescheitert"),
        ("bad_argument", "Aufruf"),
    ],
)
def test_init_key_errors(host: Host, change: str, message: str) -> None:
    host.sync_key.unlink()
    arguments = ["init-key"]
    if change == "no_target":
        host.configure(DEFAULT_CONF)
    elif change == "unresolvable":
        host.configure(DEFAULT_CONF + "target_host gross.invalid\n")
    elif change == "no_route":
        host.save("ip.json", None)
    elif change == "keygen_fails":
        host.flag("keygen-fails")
    else:
        arguments.append("--old")
    result = host.command(*arguments)
    assert result.returncode != 0
    assert message in result.stderr + result.stdout
    assert not host.sync_key.exists()


def test_trust_target_with_fingerprint(host: Host) -> None:
    host.known_hosts.unlink()
    fingerprint = _fingerprint(HOST_KEY)
    result = host.command("trust-target", "--fingerprint", fingerprint)
    assert result.returncode == 0, result.stderr
    assert f"Hostkey von Groß: ED25519 {fingerprint}" in result.stdout
    assert "OK: guard.sh antwortet (twinraid-guard 3 sync ok)." in result.stdout
    assert host.known_hosts.read_text() == f"{TARGET} ssh-ed25519 {HOST_KEY}\n"
    assert host.known_hosts.stat().st_mode & 0o777 == 0o600
    assert host.calls("ssh-keyscan") == [["-t", "ed25519", "-p", "22", "--", TARGET]]
    assert host.calls("ssh") == [[*ssh_args(host), "check"]]


@pytest.mark.parametrize(("answer", "saved"), [("yes\n", True), ("no\n", False), ("", False)])
def test_trust_target_asks(host: Host, answer: str, saved: bool) -> None:
    host.known_hosts.unlink()
    result = host.command("trust-target", stdin=answer)
    assert (result.returncode == 0) is saved
    assert host.known_hosts.exists() is saved
    if not saved:
        assert "nichts gespeichert" in result.stderr + result.stdout
        assert host.calls("ssh") == []


def test_trust_target_refuses_a_wrong_fingerprint(host: Host) -> None:
    host.known_hosts.unlink()
    result = host.command("trust-target", "--fingerprint", "SHA256:" + "A" * 43)
    assert result.returncode != 0
    assert "stimmt nicht überein" in result.stderr + result.stdout
    assert not host.known_hosts.exists()
    bad = host.command("trust-target", "--fingerprint", "MD5:00")
    assert bad.returncode != 0
    assert not host.known_hosts.exists()


def test_trust_target_on_another_port(host: Host) -> None:
    host.known_hosts.unlink()
    host.configure(CONF + "target_port 2222\n")
    result = host.command("trust-target", "--fingerprint", _fingerprint(HOST_KEY))
    assert result.returncode == 0, result.stderr
    assert host.known_hosts.read_text() == f"[{TARGET}]:2222 ssh-ed25519 {HOST_KEY}\n"
    assert host.calls("ssh-keyscan") == [["-t", "ed25519", "-p", "2222", "--", TARGET]]


@pytest.mark.parametrize(
    "scan",
    [
        "evil; rm -rf / ssh-ed25519 AAAA",
        f"{TARGET} ssh-rsa {HOST_KEY}",
        f"{TARGET} ssh-ed25519 {HOST_KEY} extra",
        "",
    ],
)
def test_trust_target_needs_a_clean_host_key(host: Host, scan: str) -> None:
    host.known_hosts.unlink()
    host.save("keyscan.json", scan)
    result = host.command("trust-target", "--fingerprint", _fingerprint(HOST_KEY))
    assert result.returncode != 0
    assert "ed25519-Hostkey" in result.stderr + result.stdout
    assert not host.known_hosts.exists()


def test_trust_target_reports_a_failed_check(host: Host) -> None:
    host.known_hosts.unlink()
    host.answer_ssh(check={"rc": 255, "stderr": f"root@{TARGET}: Permission denied (publickey)."})
    result = host.command("trust-target", "--fingerprint", _fingerprint(HOST_KEY))
    assert result.returncode == 1
    assert "Test mit check gescheitert: auth_failed" in result.stdout
    assert "Permission" not in result.stdout
    # Der Hostkey ist trotzdem gespeichert; nur der Key fehlt auf Groß.
    assert host.known_hosts.exists()


# --- Start (D2) ---------------------------------------------------------------------------


def test_status_is_right_directly_after_start(host: Host) -> None:
    result = host.command("start")
    assert result.stdout.strip() == "Der Wächter wurde gestartet."
    assert "Der Wächter läuft (PID" in host.command("status").stdout
    host.command("stop")


# --- Feste Befehlszeilen ------------------------------------------------------------------


def _zfs_lines() -> list[str]:
    """Jeder Aufruf von zfs und zpool, Fortsetzungszeilen zusammengefügt."""
    joined: list[str] = []
    pending = ""
    for line in SCRIPT.read_text().splitlines():
        stripped = line.strip()
        if stripped.endswith("\\"):
            pending += stripped[:-1]
            continue
        joined.append(pending + stripped)
        pending = ""
    skip = ("#", "readonly", "zfs_call() {", "timeout --kill-after")
    return [
        line
        for line in joined
        if ('"${ZFS}"' in line or '"${ZPOOL}"' in line)
        and not line.startswith(skip)
        and '-x "${Z' not in line
    ]


def test_zfs_is_called_only_with_fixed_arguments() -> None:
    """Namen folgen immer nach "--"; nie rekursiv (außer get über Snapshots), nie -F."""
    lines = _zfs_lines()
    assert len(lines) > 15
    for line in lines:
        assert " -- " in line, line
        assert " -R " not in line, line
        assert " -F " not in line, line
        assert " -r " not in line or (line.count(" -r ") == 1 and " get " in line), line
    sends = [line for line in lines if " send " in line]
    assert [line.split('"${ZFS}" ', 1)[1].split(" --", 1)[0] for line in sends] == [
        'send -n -v -P -L -c -e "${increment[@]+"${increment[@]}"}"',
        'send -L -c -e -v -P "${increment[@]+"${increment[@]}"}"',
    ]
    destroys = [line for line in lines if " destroy " in line]
    assert destroys == ['if zfs_call "${ZFS}" destroy -- "${snap}" >/dev/null 2>&1; then']
    recursive = [line for line in lines if " -r " in line]
    assert len(recursive) == 1
    assert " get -H -p -r -t snapshot " in recursive[0]


def test_ssh_is_called_only_with_the_fixed_list() -> None:
    text = SCRIPT.read_text()
    calls = [line.strip() for line in text.splitlines() if '"${SSH}"' in line]
    assert calls == [
        'out="$(timeout --kill-after=5 "${CHECK_TIMEOUT}" "${SSH}" "${SSH_ARGS[@]}" check \\',
        '"${SSH}" "${SSH_ARGS[@]}" "receive ${name}" >/dev/null '
        '2>"${PRIVATE_DIR}/child/ssh.err"; then',
    ]
