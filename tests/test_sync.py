from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from lyrarma_cloud_client.models import FileEntry, FolderEntry, RemoteSnapshot
from lyrarma_cloud_client.state import StateStore
from lyrarma_cloud_client.sync import SyncEngine


class FakeApi:
    def __init__(self) -> None:
        self.files: dict[str, tuple[FileEntry, bytes]] = {}
        self.folders: dict[str, FolderEntry] = {}
        self.sequence = 0
        self.deleted_file_ids: list[str] = []
        self.max_upload_size = 1024 * 1024
        self.upload_attempts = 0
        self.max_upload_size_requests = 0

    def _next_id(self, prefix: str) -> str:
        self.sequence += 1
        return f"{prefix}-{self.sequence}"

    def _folder_path(self, folder_id: str) -> str:
        if folder_id == "root":
            return ""
        for path, entry in self.folders.items():
            if entry.id == folder_id:
                return path
        raise AssertionError(f"Unknown folder: {folder_id}")

    def seed_file(self, path: str, content: bytes) -> FileEntry:
        folder_path = str(PurePosixPath(path).parent)
        folder_path = "" if folder_path == "." else folder_path
        folder_id = "root"
        if folder_path:
            folder_id = self.ensure_folder(folder_path).id
        identifier = self._next_id("file")
        entry = FileEntry(
            id=identifier,
            owner_id="user",
            folder_id=folder_id,
            original_name=PurePosixPath(path).name,
            size=len(content),
            content_type="application/octet-stream",
            is_public=False,
            uploaded_at=f"2026-01-01T00:00:{self.sequence:02d}Z",
        )
        self.files[path] = (entry, content)
        return entry

    def ensure_folder(self, path: str) -> FolderEntry:
        parent_id = "root"
        current_parts: list[str] = []
        result: FolderEntry | None = None
        for part in PurePosixPath(path).parts:
            current_parts.append(part)
            current = "/".join(current_parts)
            result = self.folders.get(current)
            if result is None:
                result = self.create_folder(part, parent_id)
            parent_id = result.id
        assert result is not None
        return result

    def get_snapshot(self) -> RemoteSnapshot:
        return RemoteSnapshot(
            files={path: value[0] for path, value in self.files.items()},
            folders=dict(self.folders),
        )

    def create_folder(self, name: str, parent_id: str = "root") -> FolderEntry:
        parent = self._folder_path(parent_id)
        path = f"{parent}/{name}" if parent else name
        identifier = self._next_id("folder")
        entry = FolderEntry(
            id=identifier,
            owner_id="user",
            parent_id=parent_id,
            name=name,
            is_public=False,
            created_at=f"2026-01-01T00:00:{self.sequence:02d}Z",
        )
        self.folders[path] = entry
        return entry

    def delete_folder(self, folder_id: str) -> None:
        path = self._folder_path(folder_id)
        self.folders = {
            item_path: entry
            for item_path, entry in self.folders.items()
            if not (item_path == path or item_path.startswith(path + "/"))
        }
        self.files = {
            item_path: value
            for item_path, value in self.files.items()
            if not item_path.startswith(path + "/")
        }

    def get_max_upload_size(self) -> int:
        self.max_upload_size_requests += 1
        return self.max_upload_size

    def upload_file(self, source: Path, folder_id: str = "root") -> FileEntry:
        self.upload_attempts += 1
        parent = self._folder_path(folder_id)
        path = f"{parent}/{source.name}" if parent else source.name
        return self.seed_file(path, source.read_bytes())

    def download_file(self, entry: FileEntry, destination: Path) -> None:
        for remote_entry, content in self.files.values():
            if remote_entry.id == entry.id:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                return
        raise AssertionError(f"Unknown file: {entry.id}")

    def delete_file(self, file_id: str) -> None:
        self.deleted_file_ids.append(file_id)
        for path, value in list(self.files.items()):
            if value[0].id == file_id:
                del self.files[path]
                return

    def close(self) -> None:
        pass


def make_engine(tmp_path: Path, api: FakeApi) -> SyncEngine:
    sync_dir = tmp_path / "sync"
    store = StateStore(tmp_path / "state", "https://example.test", "alice", sync_dir)
    return SyncEngine(api, sync_dir, store, interval=60, device_name="test-pc")


def test_initial_sync_uploads_local_and_downloads_remote(tmp_path: Path) -> None:
    api = FakeApi()
    api.seed_file("remote.txt", b"from cloud")
    engine = make_engine(tmp_path, api)
    engine.sync_dir.mkdir()
    (engine.sync_dir / "local.txt").write_bytes(b"from desktop")

    engine.sync_once()

    assert (engine.sync_dir / "remote.txt").read_bytes() == b"from cloud"
    assert api.files["local.txt"][1] == b"from desktop"
    assert engine.view().uploaded == 1
    assert engine.view().downloaded == 1


def test_local_modification_uploads_new_version_before_cleanup(tmp_path: Path) -> None:
    api = FakeApi()
    engine = make_engine(tmp_path, api)
    engine.sync_dir.mkdir()
    local = engine.sync_dir / "notes.txt"
    local.write_bytes(b"version one")
    engine.sync_once()
    old_id = api.files["notes.txt"][0].id

    local.write_bytes(b"version two")
    metadata = local.stat()
    os.utime(local, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000))
    engine.sync_once()

    assert api.files["notes.txt"][1] == b"version two"
    assert api.files["notes.txt"][0].id != old_id
    assert old_id in api.deleted_file_ids


def test_remote_deletion_archives_unchanged_local_file(tmp_path: Path) -> None:
    api = FakeApi()
    api.seed_file("obsolete.txt", b"recoverable")
    engine = make_engine(tmp_path, api)
    engine.sync_once()
    del api.files["obsolete.txt"]

    engine.sync_once()

    assert not (engine.sync_dir / "obsolete.txt").exists()
    archived = list(engine.state_store.history_dir.rglob("obsolete.txt"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == b"recoverable"


def test_simultaneous_changes_create_a_conflict_copy(tmp_path: Path) -> None:
    api = FakeApi()
    api.seed_file("shared.txt", b"initial")
    engine = make_engine(tmp_path, api)
    engine.sync_once()

    local = engine.sync_dir / "shared.txt"
    local.write_bytes(b"local edit")
    metadata = local.stat()
    os.utime(local, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000))
    api.seed_file("shared.txt", b"remote edit")

    engine.sync_once()

    assert local.read_bytes() == b"remote edit"
    conflict_files = list(engine.sync_dir.glob("shared (conflict-test-pc-*).txt"))
    assert len(conflict_files) == 1
    assert conflict_files[0].read_bytes() == b"local edit"
    assert any(
        path.startswith("shared (conflict-test-pc-") and content == b"local edit"
        for path, (_entry, content) in api.files.items()
    )
    assert engine.view().conflicts == 1


def test_local_deletion_is_propagated_to_cloud(tmp_path: Path) -> None:
    api = FakeApi()
    remote = api.seed_file("delete-me.txt", b"content")
    engine = make_engine(tmp_path, api)
    engine.sync_once()

    (engine.sync_dir / "delete-me.txt").unlink()
    engine.sync_once()

    assert "delete-me.txt" not in api.files
    assert remote.id in api.deleted_file_ids


def test_remote_folder_deletion_archives_unchanged_tree(tmp_path: Path) -> None:
    api = FakeApi()
    folder = api.ensure_folder("documents")
    api.seed_file("documents/report.txt", b"report")
    engine = make_engine(tmp_path, api)
    engine.sync_once()

    api.delete_folder(folder.id)
    engine.sync_once()

    assert not (engine.sync_dir / "documents").exists()
    archived = list(engine.state_store.history_dir.rglob("report.txt"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == b"report"


def test_oversized_upload_uses_server_response_and_keeps_local_file(tmp_path: Path) -> None:
    from lyrarma_cloud_client.api import ApiError

    class RejectingApi(FakeApi):
        def upload_file(self, source: Path, folder_id: str = "root") -> FileEntry:
            self.upload_attempts += 1
            raise ApiError("file size exceeds the allowed limit", 413, "too_large")

    api = RejectingApi()
    engine = make_engine(tmp_path, api)
    engine.sync_dir.mkdir()
    local = engine.sync_dir / "large.bin"
    local.write_bytes(b"too large")

    engine.sync_once()
    engine.sync_once()

    assert local.read_bytes() == b"too large"
    assert not api.files
    assert api.upload_attempts == 1
    assert any(event.key == "file_too_large" for event in engine.view().events)


def test_oversized_file_is_persistently_skipped_without_upload_retries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    api = FakeApi()
    api.max_upload_size = 4
    engine = make_engine(tmp_path, api)
    engine.sync_dir.mkdir()
    local = engine.sync_dir / "large.bin"
    local.write_bytes(b"12345")

    engine.sync_once()

    from lyrarma_cloud_client.sync import filesystem

    hash_calls = 0
    original_hash_file = filesystem.hash_file

    def count_hashes(path: Path) -> str:
        nonlocal hash_calls
        hash_calls += 1
        return original_hash_file(path)

    monkeypatch.setattr(filesystem, "hash_file", count_hashes)
    engine.sync_once()

    assert api.upload_attempts == 0
    assert hash_calls == 0
    assert local.read_bytes() == b"12345"
    assert sum(event.key == "file_too_large" for event in engine.view().events) == 1
    skipped = engine.state_store.load().skipped_uploads["large.bin"]
    assert skipped.local_size == 5
    assert skipped.max_upload_size == 4

    restarted_engine = make_engine(tmp_path, api)
    restarted_engine.sync_once()

    assert api.upload_attempts == 0
    assert not any(event.key == "file_too_large" for event in restarted_engine.view().events)


def test_skipped_file_uploads_after_server_limit_increases(tmp_path: Path) -> None:
    api = FakeApi()
    api.max_upload_size = 4
    engine = make_engine(tmp_path, api)
    engine.sync_dir.mkdir()
    local = engine.sync_dir / "large.bin"
    local.write_bytes(b"12345")
    engine.sync_once()

    api.max_upload_size = 5
    engine.sync_once()

    assert api.upload_attempts == 1
    assert api.files["large.bin"][1] == b"12345"
    assert "large.bin" not in engine.state_store.load().skipped_uploads


def test_skipped_file_uploads_after_it_becomes_small_enough(tmp_path: Path) -> None:
    api = FakeApi()
    api.max_upload_size = 4
    engine = make_engine(tmp_path, api)
    engine.sync_dir.mkdir()
    local = engine.sync_dir / "large.bin"
    local.write_bytes(b"12345")
    engine.sync_once()

    local.write_bytes(b"1234")
    engine.sync_once()

    assert api.upload_attempts == 1
    assert api.files["large.bin"][1] == b"1234"
    assert "large.bin" not in engine.state_store.load().skipped_uploads


def test_upload_limit_does_not_block_remote_downloads(tmp_path: Path) -> None:
    api = FakeApi()
    api.max_upload_size = 1
    api.seed_file("remote-large.bin", b"remote content")
    engine = make_engine(tmp_path, api)

    engine.sync_once()

    assert (engine.sync_dir / "remote-large.bin").read_bytes() == b"remote content"
    assert engine.view().downloaded == 1
    assert api.upload_attempts == 0
