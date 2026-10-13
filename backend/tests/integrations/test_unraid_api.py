"""Unraid-API: Abfrage, Fingerabdruck-Prüfung und Fehlerursachen (Konzept 3.5)."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support.fake_unraid import (
    FakeUnraid,
    Reply,
    graphql_error,
    graphql_reply,
    other_certificate_fingerprint,
)
from twinraid.integrations.unraid_api import (
    MAX_RESPONSE_BYTES,
    ApiEndpoint,
    Credentials,
    UnraidApi,
    UnraidApiError,
    normalize_fingerprint,
    presented_fingerprint,
)
from twinraid.shared.status import StatusReason

KEY = "abcDEF0123456789-_key"
DOMAINS = [
    {"name": "Windows 11 Gaming", "state": "RUNNING"},
    {"name": "Andere VM", "state": "SHUTOFF"},
]


@pytest.fixture
def credentials(tmp_path: Path) -> Credentials:
    creds = Credentials(tmp_path / "target-api-key", tmp_path / "target-api-cert.sha256")
    creds.save_api_key(KEY)
    return creds


@pytest.fixture
def plain(tmp_path: Path) -> Iterator[FakeUnraid]:
    server = FakeUnraid(tmp_path)
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def secure(tmp_path: Path) -> Iterator[FakeUnraid]:
    server = FakeUnraid(tmp_path, tls=True)
    try:
        yield server
    finally:
        server.close()


def _api(server: FakeUnraid, credentials: Credentials, encrypted: bool = False) -> UnraidApi:
    return UnraidApi(ApiEndpoint("127.0.0.1", server.port, encrypted), credentials, timeout=2)


def _reason(api: UnraidApi) -> StatusReason:
    with pytest.raises(UnraidApiError) as excinfo:
        api.vm_states()
    return excinfo.value.reason


def test_vm_states_over_http(plain: FakeUnraid, credentials: Credentials) -> None:
    plain.reply = graphql_reply(DOMAINS)
    states = _api(plain, credentials).vm_states()
    assert states == {"Windows 11 Gaming": "RUNNING", "Andere VM": "SHUTOFF"}
    request = plain.received[0]
    assert request.path == "/graphql"
    assert request.headers["x-api-key"] == KEY
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.body) == {
        "query": "query TwinRaidVmStatus { vms { domains { name state } } }"
    }


@pytest.mark.anyio
async def test_vm_state_by_name(plain: FakeUnraid, credentials: Credentials) -> None:
    plain.reply = graphql_reply(DOMAINS)
    api = _api(plain, credentials)
    assert await api.vm_state("Windows 11 Gaming") == "RUNNING"
    assert await api.vm_state("gibt es nicht") is None


def test_missing_key_sends_nothing(plain: FakeUnraid, tmp_path: Path) -> None:
    creds = Credentials(tmp_path / "none", tmp_path / "none.sha256")
    assert _reason(_api(plain, creds)) is StatusReason.API_KEY_MISSING
    assert plain.received == []


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (Reply(status=401), StatusReason.API_KEY_INVALID),
        (Reply(status=403), StatusReason.API_FORBIDDEN),
        (
            graphql_error("UNAUTHENTICATED", "API key validation failed"),
            StatusReason.API_KEY_INVALID,
        ),
        (graphql_error(None, "Unauthorized"), StatusReason.API_KEY_INVALID),
        (graphql_error("FORBIDDEN", "Forbidden resource"), StatusReason.API_FORBIDDEN),
        (graphql_error(None, "Forbidden resource"), StatusReason.API_FORBIDDEN),
        (graphql_error("INTERNAL_SERVER_ERROR", "libvirt down"), StatusReason.API_ERROR),
        (
            Reply(status=302, headers={"Location": "https://tower.local/graphql"}),
            StatusReason.API_REDIRECTED,
        ),
        (Reply(status=500, body=b"<html>"), StatusReason.API_ERROR),
        (Reply(body=b"kein json"), StatusReason.API_ERROR),
        (Reply(body=b"[]"), StatusReason.API_ERROR),
        (Reply(body=b'{"data": {"vms": null}}'), StatusReason.API_ERROR),
        (Reply(body=b"x" * (MAX_RESPONSE_BYTES + 10)), StatusReason.API_ERROR),
    ],
)
def test_error_reasons(
    plain: FakeUnraid, credentials: Credentials, reply: Reply, reason: StatusReason
) -> None:
    plain.reply = reply
    assert _reason(_api(plain, credentials)) is reason


def test_unreachable_api(tmp_path: Path, credentials: Credentials) -> None:
    server = FakeUnraid(tmp_path)
    port = server.port
    server.close()
    api = UnraidApi(ApiEndpoint("127.0.0.1", port, False), credentials, timeout=1)
    assert _reason(api) is StatusReason.API_UNREACHABLE


def test_https_without_confirmed_fingerprint_sends_nothing(
    secure: FakeUnraid, credentials: Credentials
) -> None:
    assert _reason(_api(secure, credentials, encrypted=True)) is StatusReason.API_CERT_UNTRUSTED
    assert secure.received == []


def test_https_with_confirmed_fingerprint(secure: FakeUnraid, credentials: Credentials) -> None:
    assert secure.certificate_fingerprint is not None
    credentials.save_fingerprint(secure.certificate_fingerprint)
    secure.reply = graphql_reply(DOMAINS)
    assert _api(secure, credentials, encrypted=True).vm_states()["Windows 11 Gaming"] == "RUNNING"


def test_changed_certificate_never_receives_the_key(
    secure: FakeUnraid, credentials: Credentials, tmp_path: Path
) -> None:
    """Hat Unraid ein anderes Zertifikat als bestätigt, wird der Key nicht gesendet."""
    credentials.save_fingerprint(other_certificate_fingerprint(tmp_path))
    assert _reason(_api(secure, credentials, encrypted=True)) is StatusReason.API_CERT_CHANGED
    assert secure.received == []


def test_presented_fingerprint_is_read_without_sending_anything(secure: FakeUnraid) -> None:
    endpoint = ApiEndpoint("127.0.0.1", secure.port, True)
    assert presented_fingerprint(endpoint, timeout=2) == secure.certificate_fingerprint
    assert secure.received == []


def test_http_server_behind_https_setting_is_unreachable(
    plain: FakeUnraid, credentials: Credentials
) -> None:
    credentials.save_fingerprint("AB" * 32)
    assert _reason(_api(plain, credentials, encrypted=True)) is StatusReason.API_UNREACHABLE


@pytest.mark.parametrize("key", ["", "mit leerzeichen", "ä" * 3, "x" * 513, "a;b", "a\nb"])
def test_invalid_keys_are_rejected(credentials: Credentials, key: str) -> None:
    with pytest.raises(ValueError, match="only contain letters"):
        credentials.save_api_key(key)


def test_secret_files_are_private(credentials: Credentials, tmp_path: Path) -> None:
    credentials.save_fingerprint("ab" * 32)
    for name in ("target-api-key", "target-api-cert.sha256"):
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600
    assert credentials.pinned_fingerprint() == ":".join(["AB"] * 32)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ab" * 32, ":".join(["AB"] * 32)),
        (":".join(["ab"] * 32), ":".join(["AB"] * 32)),
        ("  " + ":".join(["0F"] * 32) + "\n", ":".join(["0F"] * 32)),
    ],
)
def test_normalize_fingerprint(value: str, expected: str) -> None:
    assert normalize_fingerprint(value) == expected


@pytest.mark.parametrize("value", ["", "AB", "zz" * 32, "ab" * 33])
def test_invalid_fingerprints(value: str) -> None:
    with pytest.raises(ValueError, match="64 hex digits"):
        normalize_fingerprint(value)


def test_endpoint_url() -> None:
    assert ApiEndpoint("192.168.10.11", 80, False).url == "http://192.168.10.11:80/graphql"
    assert ApiEndpoint("fd00::11", 443, True).url == "https://[fd00::11]:443/graphql"
