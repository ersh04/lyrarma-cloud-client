"""HTTP client for the public Lyrarma Cloud API."""

from __future__ import annotations

import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from ..models import FileEntry, FolderEntry, RemoteSnapshot

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class ApiError(RuntimeError):
    """Normalized server, transport, or local I/O failure."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def normalize_api_url(value: str) -> str:
    """Validate and normalize a server base URL."""
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("The server URL must start with http:// or https://")
    if parsed.username or parsed.password:
        raise ValueError("Credentials are not allowed in the server URL")
    clean_path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, clean_path, "", ""))


def safe_local_name(name: str, identifier: str) -> str:
    """Convert a remote name to a portable single local path component."""
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name).strip().rstrip(". ")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "unnamed"
    stem = cleaned.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    if cleaned != name:
        suffix = identifier[:8] or "remote"
        path = Path(cleaned)
        cleaned = f"{path.stem}~{suffix}{path.suffix}"
    return cleaned


def _environment_timeout(name: str, fallback: float) -> float:
    try:
        value = float(os.getenv(name, str(fallback)))
    except ValueError:
        return fallback
    return value if value > 0 else fallback


class CloudApi:
    """Synchronous client for the authenticated Lyrarma Cloud HTTP API."""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = normalize_api_url(base_url)
        self.token = token
        self._owns_client = client is None
        if client is not None:
            self.client = client
        else:
            connect_timeout = _environment_timeout("CLOUD_CONNECT_TIMEOUT", 30.0)
            transfer_timeout = _environment_timeout("CLOUD_TRANSFER_TIMEOUT", 300.0)
            self.client = httpx.Client(
                timeout=httpx.Timeout(
                    connect_timeout,
                    read=transfer_timeout,
                    write=transfer_timeout,
                    pool=connect_timeout,
                ),
                follow_redirects=False,
            )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.client.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                **kwargs,
            )
        except httpx.HTTPError as error:
            raise ApiError(str(error), code="connection_error") from error
        if response.is_redirect:
            raise ApiError("Unexpected redirect from server", response.status_code, "redirect")
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            message = str(payload.get("message") or response.reason_phrase or "Request failed")
            code = str(payload.get("error") or "request_failed")
            raise ApiError(message, response.status_code, code)
        return response

    def health(self) -> bool:
        return self._request("GET", "/health").json().get("status") == "ok"

    def login(self, username: str, password: str) -> tuple[str, str]:
        response = self._request(
            "POST",
            "/api/auth/login",
            json={"username": username, "password": password},
        ).json()
        self.token = str(response["token"])
        return self.token, str(response.get("expires_at", ""))

    def register(self, username: str, password: str) -> None:
        self._request(
            "POST",
            "/api/auth/register",
            json={"username": username, "password": password},
        )

    def list_files(self, folder_id: str = "root") -> list[FileEntry]:
        response = self._request(
            "GET",
            "/api/files",
            params={"folder_id": folder_id or "root"},
        ).json()
        return [FileEntry.from_dict(value) for value in response.get("files", [])]

    def list_folders(self) -> list[FolderEntry]:
        response = self._request("GET", "/api/folders").json()
        return [FolderEntry.from_dict(value) for value in response.get("folders", [])]

    def create_folder(self, name: str, parent_id: str = "root") -> FolderEntry:
        response = self._request(
            "POST",
            "/api/folders/create",
            data={"name": name, "parent_id": parent_id or "root"},
        ).json()
        return FolderEntry.from_dict(response)

    def delete_folder(self, folder_id: str) -> None:
        self._request("DELETE", f"/api/folders/{quote(folder_id, safe='')}")

    def upload_file(self, source: Path, folder_id: str = "root") -> FileEntry:
        """Stream one file using the server multipart upload contract."""
        try:
            with source.open("rb") as stream:
                response = self._request(
                    "POST",
                    "/api/files/upload",
                    params={"folder_id": folder_id or "root"},
                    files={"file": (source.name, stream)},
                ).json()
        except OSError as error:
            raise ApiError(str(error), code="local_read_error") from error
        return FileEntry.from_dict(response)

    def download_file(self, entry: FileEntry, destination: Path) -> None:
        """
        Download a file to the specified destination

        Args:
            entry (FileEntry): The file entry to download
            destination (Path): The local path to save the downloaded file

        Raises:
            ApiError: If the request fails
        """
        # Ensure the destination directory exists and create a temporary file for downloading
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".lyrarma-download-",
            suffix=".part",
            dir=destination.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            with self.client.stream(
                "GET",
                f"{self.base_url}/api/files/{quote(entry.id, safe='')}/download",
                headers=headers,
            ) as response:
                if response.is_redirect:
                    raise ApiError(
                        "Unexpected redirect from server",
                        response.status_code,
                        "redirect",
                    )
                if response.status_code >= 400:
                    response.read()
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = {}
                    message = str(
                        payload.get("message") or response.reason_phrase or "Download failed"
                    )
                    code = str(payload.get("error") or "request_failed")
                    raise ApiError(message, response.status_code, code)
                with temporary.open("wb") as stream:
                    for chunk in response.iter_bytes(1024 * 1024):
                        stream.write(chunk)
            os.replace(temporary, destination)
        except httpx.HTTPError as error:
            temporary.unlink(missing_ok=True)
            raise ApiError(str(error), code="connection_error") from error
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def delete_file(self, file_id: str) -> None:
        """
        Delete a file by its ID

        Args:
            file_id (str): The ID of the file to delete

        Raises:
            ApiError: If the request fails
        """
        self._request("DELETE", f"/api/files/{quote(file_id, safe='')}")

    def set_file_public(self, file_id: str, is_public: bool) -> None:
        value = "true" if is_public else "false"
        self._request(
            "GET",
            f"/api/files/{quote(file_id, safe='')}/change_permission/{value}",
        )

    def set_folder_public(self, folder_id: str, is_public: bool) -> None:
        """
        Set the public visibility of a folder

        Args:
            folder_id (str): The ID of the folder to update
            is_public (bool): Whether the folder should be public

        Raises:
            ApiError: If the request fails
        """
        value = "true" if is_public else "false"
        self._request(
            "POST",
            f"/api/folders/{quote(folder_id, safe='')}/change_permission/{value}",
        )

    def get_snapshot(self) -> RemoteSnapshot:
        folder_entries = self.list_folders()
        folders_by_id = {folder.id: folder for folder in folder_entries}
        resolved: dict[str, str] = {"root": ""}
        resolving: set[str] = set()

        def resolve(folder_id: str) -> str:
            if folder_id in resolved:
                return resolved[folder_id]
            folder = folders_by_id.get(folder_id)
            if folder is None or folder_id in resolving:
                return f"_orphaned/{folder_id[:8]}"
            resolving.add(folder_id)
            parent_path = resolve(folder.parent_id or "root")
            name = safe_local_name(folder.name, folder.id)
            value = str(PurePosixPath(parent_path) / name) if parent_path else name
            resolving.discard(folder_id)
            resolved[folder_id] = value
            return value

        folders: dict[str, FolderEntry] = {}
        for folder in folder_entries:
            path = resolve(folder.id)
            if path in folders:
                path = f"{path}~{folder.id[:8]}"
            folders[path] = folder

        candidates: dict[str, list[FileEntry]] = defaultdict(list)
        for folder_id in ["root", *folders_by_id]:
            parent_path = resolved.get(folder_id, "")
            for entry in self.list_files(folder_id):
                name = safe_local_name(entry.original_name, entry.id)
                path = str(PurePosixPath(parent_path) / name) if parent_path else name
                candidates[path].append(entry)

        files: dict[str, FileEntry] = {}
        duplicates: dict[str, list[FileEntry]] = {}
        for path, entries in candidates.items():
            ordered = sorted(entries, key=lambda item: (item.uploaded_at, item.id), reverse=True)
            files[path] = ordered[0]
            if len(ordered) > 1:
                duplicates[path] = ordered[1:]
        return RemoteSnapshot(files=files, folders=folders, duplicate_files=duplicates)

    def get_max_upload_size(self) -> int:
        """Return the validated maximum upload size in bytes."""
        response = self._request("GET", "/api/info/max_upload_size")
        try:
            max_upload_size = int(response.json()["max_upload_size"])
        except (KeyError, TypeError, ValueError) as error:
            raise ApiError(
                "The server returned an invalid maximum upload size",
                response.status_code,
                "invalid_response",
            ) from error
        if max_upload_size <= 0:
            raise ApiError(
                "The server returned a non-positive maximum upload size",
                response.status_code,
                "invalid_response",
            )
        return max_upload_size
