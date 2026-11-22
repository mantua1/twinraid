"""source-guard.sh Version 5: vorläufige Snapshots (Konzept 3.6, „Vorläufige Snapshots“, E28).

Der Snapshot eines Jobs, der nie gesendet wurde, wird am Ende des Jobs verworfen und zählt
nicht für den Abstand zwischen Snapshots; danach gilt eine Pause (too_soon_probe). Gelöscht
wird nur genau der Snapshot des Jobs und nur unter allen Bedingungen; sonst bleibt er.
Die Kopie des Skripts und das nachgebaute zfs kommen aus tests/support wie in
test_source_guard_jobs.py; den Weg mit echtem ZFS prüft docker/source-guard-sync-test.sh.
"""

import re
import time
from collections.abc import Callable
from typing import Any

import pytest

from tests.support.source_guard_host import DEFAULT_CONF, Host
from tests.test_source_guard_jobs import (
    BASE,
    BASE_GUID,
    CONF,
    DS,
    check_answer,
    dataset,
    job_of,
    make,  # noqa: F401 - Fixture
    mirror,
    new_name,
    prepared,
    sent,
    started,
    wait_until,
)

DOKUMENTE = "mainpool/dokumente"


def destroys(host: Host) -> list[list[str]]:
    """Jeder Aufruf von zfs destroy; immer genau "destroy -- <Dataset>@<Name>"."""
    calls = [call for call in host.calls("zfs") if call[0] == "destroy"]
    for call in calls:
        assert len(call) == 3, call
        assert call[1] == "--", call
        assert "@" in call[2], call
        assert "#" not in call[2], call
    return calls


def discarded(host: Host, job: dict[str, Any]) -> bool:
    """Der Wächter hat das Ende verarbeitet (verworfen oder behalten)."""
    prefix = f"job {job['id']}: provisional snapshot {job['snapshot']} discarded on "
    return any(line.startswith(prefix) for line in host.syslog())


def closed_job(host: Host) -> dict[str, Any]:
    """snapshot, dann close; wartet, bis der Wächter das Ende verarbeitet hat."""
    assert host.send("snapshot")["result"] == "created"
    job: dict[str, Any] = host.status()["job"]
    assert host.send("close")["result"] == "closed"
    wait_until(lambda: discarded(host, job))
    return job


# --- Verwerfen nach jedem Ende ohne Senden --------------------------------------------------


def test_dry_run_discards_its_snapshot(make: Callable[..., Host]) -> None:  # noqa: F811
    host = started(make(SNAPSHOT_MINUTE="20"))
    before = set(host.zfs()["snapshots"])
    job = prepared(host)
    assert host.send("close")["result"] == "closed"
    wait_until(lambda: new_name(job) not in host.zfs()["snapshots"])
    assert destroys(host) == [["destroy", "--", new_name(job)]]
    assert set(host.zfs()["snapshots"]) == before
    # Basis, Hold und Lesezeichen bleiben.
    assert host.zfs()["snapshots"][f"{DS}@{BASE}"]["holds"] == ["twinraid-base"]
    assert f"{DS}#{BASE}" in host.zfs()["bookmarks"]
    assert (
        f"job {job['id']}: provisional snapshot {job['snapshot']} discarded on 1 datasets, "
        "kept on 0"
    ) in host.syslog()
    # Der verworfene Snapshot zählt nicht für den Abstand (30 min, im Test 600 s).
    status = host.wait_status(lambda s: s["zfs"]["datasets"][0]["snapshots"] == 1)
    assert status["snapshot_allowed_at"] is None
    assert host.send("snapshot")["result"] == "created"


@pytest.mark.parametrize("end", ["snapshot", "prepared", "awaiting_confirm"])
def test_close_before_sending_discards(make: Callable[..., Host], end: str) -> None:  # noqa: F811
    host = make()
    if end == "awaiting_confirm":
        host.configure(CONF + "max_deletions 1\n")
    started(host)
    if end == "snapshot":
        host.send("snapshot")
        job = host.status()["job"]
    else:
        job = prepared(host)
        assert job["state"] == end
    assert host.send("close")["result"] == "closed"
    wait_until(lambda: new_name(job) not in host.zfs()["snapshots"])
    assert destroys(host) == [["destroy", "--", new_name(job)]]


def test_close_while_preparing_discards(make: Callable[..., Host]) -> None:  # noqa: F811
    host = make(CHECK_TIMEOUT="60")
    host.answer_ssh(check={**check_answer(), "sleep": 30})
    started(host)
    host.send("snapshot")
    job = host.status()["job"]
    host.send("prepare")
    job_of(host, lambda j: j["state"] == "preparing")
    assert host.send("close")["result"] == "closed"
    wait_until(lambda: new_name(job) not in host.zfs()["snapshots"])


def test_prepare_failed_discards(make: Callable[..., Host]) -> None:  # noqa: F811
    host = make()
    host.update_zfs(lambda z: z.update(fail=["referenced"]))
    started(host)
    host.send("snapshot")
    host.send("prepare")
    job = job_of(host, lambda j: j["state"] == "failed")
    assert job["result"] == "prepare_failed"
    wait_until(lambda: new_name(job) not in host.zfs()["snapshots"])


@pytest.mark.parametrize("phase", ["snapshot", "awaiting_confirm"])
def test_job_timeout_discards(make: Callable[..., Host], phase: str) -> None:  # noqa: F811
    host = make(CONFIRM_LIMIT="2")
    if phase == "awaiting_confirm":
        host.configure(CONF + "max_deletions 1\n")
    started(host)
    host.send("snapshot")
    if phase == "awaiting_confirm":
        host.send("prepare")
        job_of(host, lambda j: j["state"] == "awaiting_confirm")
    job = job_of(host, lambda j: j["state"] == "closed", timeout=10)
    assert job["result"] == "job_timeout"
    wait_until(lambda: new_name(job) not in host.zfs()["snapshots"])


def test_interrupted_prepare_is_discarded_after_the_restart(
    make: Callable[..., Host],  # noqa: F811
) -> None:
    host = make(CHECK_TIMEOUT="60")
    host.answer_ssh(check={**check_answer(), "sleep": 30})
    started(host)
    host.send("snapshot")
    host.send("prepare")
    job = job_of(host, lambda j: j["state"] == "preparing")
    host.stop_run(host.processes[-1])
    assert new_name(job) in host.zfs()["snapshots"]
    host.answer_ssh(check=check_answer(), receive={"default": {"rc": 0}})
    host.start_run()
    wait_until(lambda: new_name(job) not in host.zfs()["snapshots"])
    status = host.status()
    assert (status["job"]["state"], status["job"]["result"]) == ("failed", "interrupted")


# --- Nach angenommenem send nie ------------------------------------------------------------


def test_sent_snapshot_stays_and_counts(make: Callable[..., Host]) -> None:  # noqa: F811
    host = started(make(SNAPSHOT_MINUTE="20"))
    prepared(host)
    job = sent(host)
    assert job["result"] == "ok"
    assert host.send("snapshot")["result"] == "too_soon"
    status = host.status()
    # Abstand 30 min (im Test 600 s) ab dem Anlegen des gesendeten Snapshots.
    assert abs(status["snapshot_allowed_at"] - (job["created_at"] + 600)) <= 2
    assert status["probe_allowed_at"] is None
    assert destroys(host) == []
    # Der Abstand gilt auch nach einem Neustart des Wächters (private/last_snapshot).
    host.stop_run(host.processes[-1])
    host.start_run()
    assert host.send("snapshot")["result"] == "too_soon"


@pytest.mark.parametrize(
    "receive",
    [
        {"rc": 3, "stderr": "twinraid-guard: refused space-low\n"},
        {"rc": 255, "stderr": "ssh: connect to host 192.0.2.11 port 22: Connection refused\n"},
    ],
)
def test_failed_send_keeps_the_snapshot(
    make: Callable[..., Host],  # noqa: F811
    receive: dict[str, Any],
) -> None:
    host = make(SNAPSHOT_MINUTE="20")
    host.answer_ssh(check=check_answer(), receive={"default": receive})
    started(host)
    prepared(host)
    job = sent(host)
    assert job["state"] == "failed"
    time.sleep(1)
    assert new_name(job) in host.zfs()["snapshots"]
    assert destroys(host) == []
    assert host.send("snapshot")["result"] == "too_soon"


def test_close_while_sending_keeps_the_snapshot(make: Callable[..., Host]) -> None:  # noqa: F811
    host = make()
    host.update_zfs(lambda z: z.update(send_seconds=60))
    started(host)
    prepared(host)
    host.send("send")
    job = job_of(host, lambda j: (j["datasets"][0]["sent"] or 0) > 0)
    assert host.send("close")["result"] == "closed"
    time.sleep(1.5)
    assert new_name(job) in host.zfs()["snapshots"]
    assert destroys(host) == []


def test_job_timeout_while_sending_keeps_the_snapshot(
    make: Callable[..., Host],  # noqa: F811
) -> None:
    host = make(SEND_LIMIT="2")
    host.update_zfs(lambda z: z.update(send_seconds=60))
    started(host)
    prepared(host)
    host.send("send")
    job = job_of(host, lambda j: j["state"] == "closed", timeout=10)
    time.sleep(1)
    assert new_name(job) in host.zfs()["snapshots"]
    assert destroys(host) == []


# --- Pause nach dem Verwerfen ----------------------------------------------------------------


def test_pause_after_a_discarded_snapshot(make: Callable[..., Host]) -> None:  # noqa: F811
    host = started(make(PROBE_PAUSE="3"))
    job = closed_job(host)
    ended = time.time()
    status = host.status()
    assert status["probe_allowed_at"] is not None
    assert abs(status["probe_allowed_at"] - (ended + 3)) <= 2
    snapshots = len([c for c in host.calls("zfs") if c[0] == "snapshot"])
    last = host.send("snapshot")
    assert last["result"] == "too_soon_probe"
    # Ein abgelehnter Auftrag legt nichts an und ändert den Job nicht.
    assert len([c for c in host.calls("zfs") if c[0] == "snapshot"]) == snapshots
    assert host.status()["job"]["id"] == job["id"]
    # reset-run-interval hebt die Pause nicht auf.
    assert host.command("reset-run-interval").returncode == 0
    assert host.send("snapshot")["result"] == "too_soon_probe"
    wait_until(lambda: host.status()["probe_allowed_at"] is None, timeout=6)
    assert host.send("snapshot")["result"] == "created"


def test_pause_survives_a_restart(make: Callable[..., Host]) -> None:  # noqa: F811
    host = started(make(PROBE_PAUSE="6"))
    closed_job(host)
    host.stop_run(host.processes[-1])
    host.start_run()
    assert host.send("snapshot")["result"] == "too_soon_probe"


def test_too_soon_comes_before_the_pause(make: Callable[..., Host]) -> None:  # noqa: F811
    host = started(make(SNAPSHOT_MINUTE="20", PROBE_PAUSE="30"))
    prepared(host)
    sent(host)
    assert host.send("snapshot")["result"] == "too_soon"


# --- Löschen verweigert ----------------------------------------------------------------------


def _change_snapshot(host: Host, job: dict[str, Any], **values: Any) -> None:  # noqa: ANN401
    def change(zfs: dict[str, Any]) -> None:
        zfs["snapshots"][new_name(job)].update(values)

    host.update_zfs(change)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"holds": ["somebody"]}, "hold"),
        ({"holds": ["twinraid-base"]}, "base"),
        ({"clones": ["mainpool/klon"]}, "clone"),
        ({"guid": "4242"}, "guid"),
        ({"props": {}}, "foreign"),
        ({"props": {"twinraid:created-by": ["source-guard", "received"]}}, "foreign"),
    ],
)
def test_discard_refused(
    make: Callable[..., Host],  # noqa: F811
    change: dict[str, Any],
    reason: str,
) -> None:
    host = started(make(SNAPSHOT_MINUTE="20", PROBE_PAUSE="3"))
    assert host.send("snapshot")["result"] == "created"
    job = host.status()["job"]
    _change_snapshot(host, job, **change)
    assert host.send("close")["result"] == "closed"
    wait_until(lambda: discarded(host, job))
    assert new_name(job) in host.zfs()["snapshots"]
    assert destroys(host) == []
    assert (
        f"dataset twinraidtest: provisional snapshot {job['snapshot']} kept ({reason})"
        in host.syslog()
    )
    # Der behaltene Snapshot zählt für den Abstand.
    assert host.send("snapshot")["result"] == "too_soon"


def test_snapshot_on_the_target_is_never_discarded(make: Callable[..., Host]) -> None:  # noqa: F811
    host = started(make())
    assert host.send("snapshot")["result"] == "created"
    job = host.status()["job"]
    guid = host.zfs()["snapshots"][new_name(job)]["guid"]
    host.answer_ssh(
        check=check_answer({"twinraidtest": mirror(guid)}), receive={"default": {"rc": 0}}
    )
    host.send("prepare")
    job_of(host, lambda j: j["state"] not in ("snapshot", "preparing"))
    assert host.send("close")["result"] == "closed"
    wait_until(lambda: discarded(host, job))
    assert new_name(job) in host.zfs()["snapshots"]
    line = f"dataset twinraidtest: provisional snapshot {job['snapshot']} kept (on_target)"
    assert line in host.syslog()


def test_failed_destroy_keeps_the_snapshot(make: Callable[..., Host]) -> None:  # noqa: F811
    host = started(make(SNAPSHOT_MINUTE="20"))
    assert host.send("snapshot")["result"] == "created"
    job = host.status()["job"]
    host.update_zfs(lambda z: z.update(fail=["destroy"]))
    assert host.send("close")["result"] == "closed"
    wait_until(lambda: discarded(host, job))
    assert new_name(job) in host.zfs()["snapshots"]
    assert f"provisional snapshot {job['snapshot']} kept (destroy_failed)" in "\n".join(
        host.syslog()
    )
    assert host.send("snapshot")["result"] == "too_soon"


def test_gone_snapshot_counts_as_discarded(make: Callable[..., Host]) -> None:  # noqa: F811
    host = started(make(SNAPSHOT_MINUTE="20"))
    assert host.send("snapshot")["result"] == "created"
    job = host.status()["job"]
    host.update_zfs(lambda z: z["snapshots"].pop(new_name(job)))
    assert host.send("close")["result"] == "closed"
    wait_until(lambda: discarded(host, job))
    assert destroys(host) == []
    assert host.send("snapshot")["result"] == "created"


def test_unreadable_list_is_tried_again(make: Callable[..., Host]) -> None:  # noqa: F811
    host = started(make())
    assert host.send("snapshot")["result"] == "created"
    job = host.status()["job"]
    host.update_zfs(lambda z: z.update(fail=["get_snapshots"]))
    assert host.send("close")["result"] == "closed"
    time.sleep(1.5)
    assert new_name(job) in host.zfs()["snapshots"]
    assert not discarded(host, job)
    assert f"job {job['id']}: provisional snapshot not readable, trying again" in host.syslog()
    host.update_zfs(lambda z: z.update(fail=[]))
    wait_until(lambda: new_name(job) not in host.zfs()["snapshots"])


def test_only_the_allowed_dataset_is_discarded(make: Callable[..., Host]) -> None:  # noqa: F811
    host = make(SNAPSHOT_MINUTE="20")
    host.configure(CONF + "dataset dokumente\n")
    host.update_zfs(lambda z: z["datasets"].update({DOKUMENTE: dataset(DOKUMENTE)}))
    started(host)
    assert host.send("snapshot")["result"] == "created"
    job = host.status()["job"]
    _change_snapshot(host, job, holds=["somebody"])
    assert host.send("close")["result"] == "closed"
    wait_until(lambda: discarded(host, job))
    assert destroys(host) == [["destroy", "--", new_name(job, DOKUMENTE)]]
    assert new_name(job) in host.zfs()["snapshots"]
    assert (
        f"job {job['id']}: provisional snapshot {job['snapshot']} discarded on 1 datasets, "
        "kept on 1"
    ) in host.syslog()
    assert host.send("snapshot")["result"] == "too_soon"


def test_job_of_version_4_is_never_discarded(make: Callable[..., Host]) -> None:  # noqa: F811
    host = started(make())
    assert host.send("snapshot")["result"] == "created"
    job = host.status()["job"]
    host.stop_run(host.processes[-1])
    saved = host.private / "job"
    lines = [
        line
        for line in saved.read_text().splitlines()
        if not line.startswith(("created_up ", "guids ", "snapshot_state "))
    ]
    saved.write_text("\n".join(lines) + "\n")
    host.start_run()
    assert host.send("close")["result"] == "closed"
    time.sleep(1.5)
    assert new_name(job) in host.zfs()["snapshots"]
    assert destroys(host) == []


def test_snapshot_name_and_guid_come_from_the_job(make: Callable[..., Host]) -> None:  # noqa: F811
    host = started(make())
    assert host.send("snapshot")["result"] == "created"
    job = host.status()["job"]
    text = (host.private / "job").read_text()
    guid = host.zfs()["snapshots"][new_name(job)]["guid"]
    assert f"guids {guid}\n" in text
    assert "snapshot_state pending\n" in text
    assert re.search(r"^created_up [0-9]+$", text, re.MULTILINE)


# --- Status (Protokoll 4) ---------------------------------------------------------------------


def test_status_protocol_4(make: Callable[..., Host]) -> None:  # noqa: F811
    host = started(make())
    status = host.status()
    assert (status["protocol"], status["version"]) == (4, 5)
    assert status["snapshot_allowed_at"] is None
    assert status["probe_allowed_at"] is None
    base_created = host.zfs()["snapshots"][f"{DS}@{BASE}"]["creation"]
    item = status["zfs"]["datasets"][0]
    assert item["newest_snapshot_at"] == base_created
    assert item["base"] == {"guid": BASE_GUID, "created_at": base_created}
    assert host.send("snapshot")["result"] == "created"
    job = host.status()["job"]
    status = host.wait_status(
        lambda s: s["zfs"]["datasets"][0]["newest_snapshot_at"] == job["created_at"]
    )
    assert host.send("close")["result"] == "closed"
    # Nach dem Verwerfen sofort wieder die Basis als neuester Snapshot.
    host.wait_status(lambda s: s["zfs"]["datasets"][0]["newest_snapshot_at"] == base_created, 3)


def test_default_conf_still_works(make: Callable[..., Host]) -> None:  # noqa: F811
    """Ohne Ziel und Key: verworfen wird trotzdem (check ist dann nicht ok)."""
    host = make()
    host.configure(DEFAULT_CONF)
    started(host)
    job = closed_job(host)
    assert new_name(job) not in host.zfs()["snapshots"]
