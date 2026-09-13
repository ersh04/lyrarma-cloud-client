import json

import httpx
import pytest

from lyrarma_cloud_client.api.client import (
    ApiError,
    CloudApi,
    _environment_timeout,
    normalize_api_url,
    safe_local_name,
)


def test_normalize_api_url_rejects_unsafe_values() -> None:
    assert normalize_api_url(" https://cloud.example.com/base/ ") == (
        "https://cloud.example.com/base"
    )
    with pytest.raises(ValueError):
        normalize_api_url("cloud.example.com")
    with pytest.raises(ValueError):
        normalize_api_url("https://user:secret@cloud.example.com")


def test_environment_timeout_uses_new_names_and_safe_fallback(monkeypatch) -> None:
    monkeypatch.setenv("CLOUD_CONNECT_TIMEOUT", "12.5")
    assert _environment_timeout("CLOUD_CONNECT_TIMEOUT", 30.0) == 12.5

    monkeypatch.setenv("CLOUD_CONNECT_TIMEOUT", "invalid")
    assert _environment_timeout("CLOUD_CONNECT_TIMEOUT", 30.0) == 30.0

    monkeypatch.setenv("CLOUD_CONNECT_TIMEOUT", "0")
    assert _environment_timeout("CLOUD_CONNECT_TIMEOUT", 30.0) == 30.0


def test_login_and_authorized_request() -> None:
    seen_authorization = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_authorization
        if request.url.path == "/api/auth/login":
            return httpx.Response(
                200,
                json={"token": "jwt-token", "expires_at": "2099-01-01T00:00:00Z"},
            )
        seen_authorization = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"folders": [], "total": 0})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    api = CloudApi("https://cloud.example.com", client=client)

    token, _expires_at = api.login("alice", "long-password")
    assert token == "jwt-token"
    assert api.list_folders() == []
    assert seen_authorization == "Bearer jwt-token"


def test_api_error_uses_server_error_shape() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            content=json.dumps(
                {"error": "invalid_credentials", "message": "invalid username or password"}
            ),
            headers={"content-type": "application/json"},
        )

    api = CloudApi(
        "https://cloud.example.com",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ApiError) as raised:
        api.login("alice", "wrong-password")

    assert raised.value.status_code == 401
    assert raised.value.code == "invalid_credentials"


def test_safe_local_name_blocks_path_traversal_and_reserved_names() -> None:
    assert "/" not in safe_local_name("../secret.txt", "123456789")
    assert safe_local_name("CON", "123456789").startswith("_CON")


def test_upload_file_uses_server_multipart_contract(tmp_path) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(b"upload payload")
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["folder_id"] = request.url.params.get("folder_id")
        observed["authorization"] = request.headers.get("Authorization")
        observed["content_type"] = request.headers.get("Content-Type")
        observed["body"] = request.read()
        return httpx.Response(
            201,
            json={
                "id": "file-1",
                "owner_id": "user-1",
                "folder_id": "folder-1",
                "original_name": "report.txt",
                "size": 14,
                "content_type": "text/plain",
                "is_public": False,
                "uploaded_at": "2026-01-01T00:00:00Z",
            },
        )

    api = CloudApi(
        "https://cloud.example.com",
        token="jwt-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    uploaded = api.upload_file(source, "folder-1")

    assert uploaded.id == "file-1"
    assert observed["path"] == "/api/files/upload"
    assert observed["folder_id"] == "folder-1"
    assert observed["authorization"] == "Bearer jwt-token"
    assert str(observed["content_type"]).startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="report.txt"' in observed["body"]
    assert b"upload payload" in observed["body"]


def test_get_max_upload_size_validates_server_response() -> None:
    def valid_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"max_upload_size": "1048576"})

    api = CloudApi(
        "https://cloud.example.com",
        client=httpx.Client(transport=httpx.MockTransport(valid_handler)),
    )
    assert api.get_max_upload_size() == 1048576

    def invalid_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"max_upload_size": 0})

    invalid_api = CloudApi(
        "https://cloud.example.com",
        client=httpx.Client(transport=httpx.MockTransport(invalid_handler)),
    )
    with pytest.raises(ApiError) as raised:
        invalid_api.get_max_upload_size()
    assert raised.value.code == "invalid_response"
