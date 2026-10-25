"""Aktionen über die Weboberfläche: feste Route, CSRF, Audit-Log (Konzept 4.2 und 5.3)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.web.helpers import CLIENT_IP, FakeWorker, login, make_app, make_client
from twinraid.shared.actions import ActionBlocker, ActionName
from twinraid.shared.ipc import ErrorCode
from twinraid.web.worker_client import WorkerCommandError


def _client(tmp_path: Path, worker: FakeWorker) -> tuple[TestClient, str]:
    client = make_client(make_app(tmp_path, worker))
    return client, login(client)


def _audit(client: TestClient) -> list[tuple[str, str, str | None]]:
    entries = client.get("/api/audit").json()["entries"]
    return [(e["action"], e["result"], e["client_ip"]) for e in entries]


@pytest.mark.parametrize("action", list(ActionName))
def test_action_is_requested(tmp_path: Path, action: ActionName) -> None:
    worker = FakeWorker()
    client, csrf = _client(tmp_path, worker)
    response = client.post(f"/api/actions/{action.value}", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 202
    assert response.json() == {"detail": "accepted"}
    assert worker.actions == [action]
    assert _audit(client)[0] == (action.value, "requested", CLIENT_IP)


def test_csrf_token_is_required(tmp_path: Path) -> None:
    worker = FakeWorker()
    client, _ = _client(tmp_path, worker)
    assert client.post("/api/actions/vm_start").status_code == 403
    assert client.post("/api/actions/vm_start", headers={"X-CSRF-Token": "x"}).status_code == 403
    assert worker.actions == []


def test_login_is_required(tmp_path: Path) -> None:
    worker = FakeWorker()
    client = make_client(make_app(tmp_path, worker))
    assert client.post("/api/actions/vm_start").status_code == 401
    assert worker.actions == []


@pytest.mark.parametrize(
    "name", ["vm_reboot", "sync_cancel", "sync_confirm_deletions", "VM_START", "vm_start;rm"]
)
def test_only_fixed_actions_exist(tmp_path: Path, name: str) -> None:
    worker = FakeWorker()
    client, csrf = _client(tmp_path, worker)
    response = client.post(f"/api/actions/{name}", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_request"}
    assert worker.calls.count("get_status") == worker.status_calls
    assert worker.actions == []


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (WorkerCommandError(ErrorCode.ACTION_BLOCKED, "x", ActionBlocker.BUSY), "busy"),
        (
            WorkerCommandError(ErrorCode.NOT_AVAILABLE, "x", ActionBlocker.NOT_ENABLED),
            "not_enabled",
        ),
        (WorkerCommandError(ErrorCode.NOT_AVAILABLE, "x"), "not_enabled"),
        (WorkerCommandError(ErrorCode.INTERNAL_ERROR, "x"), "rejected"),
    ],
)
def test_rejected_action(tmp_path: Path, error: WorkerCommandError, detail: str) -> None:
    worker = FakeWorker()
    worker.action_error = error
    client, csrf = _client(tmp_path, worker)
    response = client.post("/api/actions/vm_stop", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 409
    assert response.json() == {"detail": detail}
    assert _audit(client)[0] == ("vm_stop", "rejected", CLIENT_IP)


def test_worker_down(tmp_path: Path) -> None:
    worker = FakeWorker()
    client, csrf = _client(tmp_path, worker)
    worker.available = False
    response = client.post("/api/actions/shutdown_target", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 503
    assert response.json() == {"detail": "worker_unavailable"}


def test_request_body_is_ignored(tmp_path: Path) -> None:
    """Die Route nimmt keine Daten entgegen; ein mitgeschickter Inhalt ändert nichts."""
    worker = FakeWorker()
    client, csrf = _client(tmp_path, worker)
    response = client.post(
        "/api/actions/vm_start",
        headers={"X-CSRF-Token": csrf},
        json={"vm": "Andere VM", "host": "192.0.2.1"},
    )
    assert response.status_code == 202
    assert worker.actions == [ActionName.VM_START]


# --- Probelauf abbrechen (Konzept 4.3, ab v0.5.1) ---------------------------------------

JOB_ID = "0123456789abcdef0123456789abcdef"


def test_cancel_dry_run(tmp_path: Path) -> None:
    worker = FakeWorker()
    client, csrf = _client(tmp_path, worker)
    response = client.post(f"/api/sync/{JOB_ID}/cancel", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 202
    assert response.json() == {"detail": "accepted"}
    assert worker.cancelled == [JOB_ID]
    assert _audit(client)[0] == ("sync_cancel", "requested", CLIENT_IP)


def test_cancel_needs_login_and_csrf(tmp_path: Path) -> None:
    worker = FakeWorker()
    (tmp_path / "anonymous").mkdir()
    anonymous = make_client(make_app(tmp_path / "anonymous", worker))
    assert anonymous.post(f"/api/sync/{JOB_ID}/cancel").status_code == 401
    client, _ = _client(tmp_path, worker)
    assert client.post(f"/api/sync/{JOB_ID}/cancel").status_code == 403
    assert worker.cancelled == []


@pytest.mark.parametrize(
    "job_id",
    [
        JOB_ID.upper(),
        JOB_ID[:-1],
        JOB_ID + "0",
        "../" + JOB_ID[3:],
        "0123456789abcdef0123456789abcdeg",
    ],
)
def test_cancel_takes_only_a_job_id(tmp_path: Path, job_id: str) -> None:
    """Der einzige Parameter ist die vom Worker vergebene Kennung (Konzept 3.2)."""
    worker = FakeWorker()
    client, csrf = _client(tmp_path, worker)
    response = client.post(f"/api/sync/{job_id}/cancel", headers={"X-CSRF-Token": csrf})
    assert response.status_code in {404, 422}
    assert worker.cancelled == []
    assert "sync_cancel" not in worker.calls


def test_cancel_rejected_by_the_worker(tmp_path: Path) -> None:
    worker = FakeWorker()
    worker.action_error = WorkerCommandError(ErrorCode.INVALID_REQUEST, "no such job")
    client, csrf = _client(tmp_path, worker)
    response = client.post(f"/api/sync/{JOB_ID}/cancel", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 409
    assert response.json() == {"detail": "rejected"}
    assert _audit(client)[0] == ("sync_cancel", "rejected", CLIENT_IP)


def test_cancel_with_the_worker_down(tmp_path: Path) -> None:
    worker = FakeWorker()
    client, csrf = _client(tmp_path, worker)
    worker.available = False
    response = client.post(f"/api/sync/{JOB_ID}/cancel", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 503
