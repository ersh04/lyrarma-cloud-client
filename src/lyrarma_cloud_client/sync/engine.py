"""Two-way synchronization orchestration and conflict resolution."""

from __future__ import annotations

import os
import platform
import re
import shutil
import threading
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path, PurePosixPath

from watchdog.observers import Observer

from ..api import ApiError, CloudApi
from ..models import (
    FileEntry,
    FileState,
    LocalFile,
    RemoteSnapshot,
    SkippedUpload,
    SyncEvent,
    SyncPhase,
    SyncState,
    SyncView,
)
from ..state import StateStore
from .filesystem import ChangeHandler, hash_file, is_below, safe_join, scan_local


class SyncEngine:
    """Synchronize one local directory with one authenticated cloud account."""

    def __init__(
        self,
        api: CloudApi,
        sync_dir: Path,
        state_store: StateStore,
        interval: int = 15,
        device_name: str | None = None,
    ) -> None:
        self.api = api
        self.sync_dir = sync_dir.expanduser().resolve()
        self.state_store = state_store
        self.interval = max(3, interval)
        self.device_name = self._clean_device_name(device_name or platform.node() or "device")
        self._status_lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._observer: Observer | None = None
        self._phase = SyncPhase.STOPPED
        self._current_action = ""
        self._last_sync: datetime | None = None
        self._last_error = ""
        self._generation = 0
        self._uploaded = 0
        self._downloaded = 0
        self._deleted = 0
        self._conflicts = 0
        self._events: deque[SyncEvent] = deque(maxlen=150)
        self._remote = RemoteSnapshot()

    @staticmethod
    def _clean_device_name(value: str) -> str:
        cleaned = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip("-.")
        return cleaned[:32] or "device"

    def start(self) -> None:
        """Start periodic synchronization and live filesystem monitoring."""
        if self._thread and self._thread.is_alive():
            return
        self.sync_dir.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._set_status(phase=SyncPhase.IDLE)
        self._thread = threading.Thread(target=self._run, name="lyrarma-sync", daemon=True)
        self._thread.start()
        try:
            handler = ChangeHandler(self.request_sync)
            observer = Observer()
            observer.schedule(handler, str(self.sync_dir), recursive=True)
            observer.start()
            self._observer = observer
        except OSError as error:
            self._event("watcher_failed", detail=str(error), level="warning")

    def stop(self) -> None:
        """Stop background workers and expose the stopped state."""
        self._stop.set()
        self._wake.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=3)
            self._observer = None
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._set_status(phase=SyncPhase.STOPPED, current_action="")

    def request_sync(self) -> None:
        """Wake the worker for an early synchronization pass."""
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sync_once()
            self._wake.wait(self.interval)
            self._wake.clear()

    def _set_status(
        self,
        *,
        phase: SyncPhase | None = None,
        current_action: str | None = None,
        last_error: str | None = None,
    ) -> None:
        with self._status_lock:
            if phase is not None:
                self._phase = phase
            if current_action is not None:
                self._current_action = current_action
            if last_error is not None:
                self._last_error = last_error
            self._generation += 1

    def _event(self, key: str, path: str = "", detail: str = "", level: str = "info") -> None:
        with self._status_lock:
            self._events.appendleft(SyncEvent(key=key, path=path, detail=detail, level=level))
            self._generation += 1

    def view(self) -> SyncView:
        """Return an immutable snapshot of status, files, and recent events."""
        with self._status_lock:
            return SyncView(
                phase=self._phase,
                current_action=self._current_action,
                last_sync=self._last_sync,
                last_error=self._last_error,
                uploaded=self._uploaded,
                downloaded=self._downloaded,
                deleted=self._deleted,
                conflicts=self._conflicts,
                generation=self._generation,
                files=dict(self._remote.files),
                folders=dict(self._remote.folders),
                events=tuple(self._events),
            )

    def _local_changed(self, record: FileState, local: LocalFile) -> bool:
        return local.digest != record.local_hash

    @staticmethod
    def _remote_changed(record: FileState, remote: FileEntry) -> bool:
        return (
            remote.id != record.remote_id
            or remote.uploaded_at != record.remote_uploaded_at
            or remote.size != record.remote_size
        )

    @staticmethod
    def _file_state(remote: FileEntry, local: LocalFile) -> FileState:
        return FileState(
            remote_id=remote.id,
            remote_uploaded_at=remote.uploaded_at,
            remote_size=remote.size,
            local_hash=local.digest,
            local_size=local.size,
            local_mtime_ns=local.mtime_ns,
        )

    def _read_local(self, relative_path: str) -> LocalFile:
        absolute = safe_join(self.sync_dir, relative_path)
        metadata = absolute.stat()
        return LocalFile(
            absolute_path=absolute,
            relative_path=relative_path,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            digest=hash_file(absolute),
        )

    def _ensure_remote_folder(
        self,
        relative_folder: str,
        remote: RemoteSnapshot,
        state: SyncState,
    ) -> str:
        if not relative_folder or relative_folder == ".":
            return "root"
        parent_id = "root"
        accumulated: list[str] = []
        for part in PurePosixPath(relative_folder).parts:
            accumulated.append(part)
            path = "/".join(accumulated)
            existing = remote.folders.get(path)
            if existing:
                parent_id = existing.id
                state.folders[path] = existing.id
                continue
            created = self.api.create_folder(part, parent_id)
            remote.folders[path] = created
            state.folders[path] = created.id
            parent_id = created.id
            self._event("folder_uploaded", path)
        return parent_id

    def _upload(
        self,
        path: str,
        local: LocalFile,
        remote: RemoteSnapshot,
        state: SyncState,
        replace: FileEntry | None = None,
    ) -> FileEntry:
        self._set_status(current_action=path)
        parent = str(PurePosixPath(path).parent)
        folder_id = self._ensure_remote_folder(parent, remote, state)
        uploaded = self.api.upload_file(local.absolute_path, folder_id)
        remote.files[path] = uploaded
        if replace and replace.id != uploaded.id:
            try:
                self.api.delete_file(replace.id)
            except ApiError as error:
                self._event("old_version_cleanup_failed", path, str(error), "warning")
        state.files[path] = self._file_state(uploaded, local)
        self._uploaded += 1
        self._event("file_uploaded", path)
        return uploaded

    @staticmethod
    def _skipped_upload(local: LocalFile, max_upload_size: int) -> SkippedUpload:
        return SkippedUpload(
            local_hash=local.digest,
            local_size=local.size,
            local_mtime_ns=local.mtime_ns,
            max_upload_size=max_upload_size,
        )

    def _remember_skipped_upload(
        self,
        path: str,
        local: LocalFile,
        max_upload_size: int,
        state: SyncState,
        *,
        detail: str = "",
        level: str = "warning",
    ) -> None:
        """Persist a rejected fingerprint and emit only its first activity event."""
        skipped = self._skipped_upload(local, max_upload_size)
        if state.skipped_uploads.get(path) != skipped:
            self._event(
                "file_too_large",
                path,
                detail or f"{local.size} bytes > {max_upload_size} bytes",
                level,
            )
        state.skipped_uploads[path] = skipped

    def _upload_is_blocked(
        self,
        path: str,
        local: LocalFile,
        max_upload_size: int,
        state: SyncState,
    ) -> bool:
        """Suppress a known rejection or a file that exceeds the current limit."""
        skipped = self._skipped_upload(local, max_upload_size)
        if state.skipped_uploads.get(path) == skipped:
            return True
        if local.size <= max_upload_size:
            state.skipped_uploads.pop(path, None)
            return False
        self._remember_skipped_upload(path, local, max_upload_size, state)
        return True

    def _download(
        self,
        path: str,
        remote_entry: FileEntry,
        remote: RemoteSnapshot,
        state: SyncState,
    ) -> LocalFile:
        self._set_status(current_action=path)
        destination = safe_join(self.sync_dir, path)
        self.api.download_file(remote_entry, destination)
        local = self._read_local(path)
        remote.files[path] = remote_entry
        state.files[path] = self._file_state(remote_entry, local)
        self._downloaded += 1
        self._event("file_downloaded", path)
        return local

    def _archive_local(self, path: Path, relative_path: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.state_store.history_dir / stamp / PurePosixPath(relative_path)
        if target.exists():
            target = target.with_name(f"{target.stem}-{uuid.uuid4().hex[:6]}{target.suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        return target

    def archive_path(self, relative_path: str) -> bool:
        """Move a local item to recoverable history and schedule its deletion."""
        with self._sync_lock:
            absolute = safe_join(self.sync_dir, relative_path)
            if not absolute.exists():
                return False
            self._archive_local(absolute, relative_path)
            self._event("local_item_removed", relative_path)
        self.request_sync()
        return True

    def _conflict_path(self, path: str) -> str:
        source = PurePosixPath(path)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        base = f"{source.stem} (conflict-{self.device_name}-{stamp}){source.suffix}"
        candidate = source.with_name(base)
        index = 2
        while safe_join(self.sync_dir, str(candidate)).exists():
            candidate = source.with_name(
                f"{source.stem} (conflict-{self.device_name}-{stamp}-{index}){source.suffix}"
            )
            index += 1
        return str(candidate)

    def _resolve_conflict(
        self,
        path: str,
        local: LocalFile,
        remote_entry: FileEntry,
        remote: RemoteSnapshot,
        state: SyncState,
    ) -> None:
        conflict_path = self._conflict_path(path)
        conflict_absolute = safe_join(self.sync_dir, conflict_path)
        conflict_absolute.parent.mkdir(parents=True, exist_ok=True)
        os.replace(local.absolute_path, conflict_absolute)
        conflict_local = self._read_local(conflict_path)
        self._download(path, remote_entry, remote, state)
        self._upload(conflict_path, conflict_local, remote, state)
        self._conflicts += 1
        self._event("conflict_created", conflict_path, path, "warning")

    def _resolve_new_collision(
        self,
        path: str,
        local: LocalFile,
        remote_entry: FileEntry,
        remote: RemoteSnapshot,
        state: SyncState,
    ) -> None:
        compare_path = safe_join(
            self.sync_dir,
            str(PurePosixPath(path).with_name(f".lyrarma-compare-{uuid.uuid4().hex}.part")),
        )
        self.api.download_file(remote_entry, compare_path)
        try:
            if hash_file(compare_path) == local.digest:
                compare_path.unlink(missing_ok=True)
                state.files[path] = self._file_state(remote_entry, local)
                return
            conflict_path = self._conflict_path(path)
            conflict_absolute = safe_join(self.sync_dir, conflict_path)
            os.replace(local.absolute_path, conflict_absolute)
            os.replace(compare_path, local.absolute_path)
            downloaded = self._read_local(path)
            state.files[path] = self._file_state(remote_entry, downloaded)
            conflict_local = self._read_local(conflict_path)
            self._upload(conflict_path, conflict_local, remote, state)
            self._downloaded += 1
            self._conflicts += 1
            self._event("conflict_created", conflict_path, path, "warning")
        finally:
            compare_path.unlink(missing_ok=True)

    def _local_subtree_changed(
        self,
        folder: str,
        local_files: dict[str, LocalFile],
        local_directories: set[str],
        state: SyncState,
    ) -> bool:
        for path, local in local_files.items():
            if is_below(path, folder):
                record = state.files.get(path)
                if record is None or self._local_changed(record, local):
                    return True
        return any(
            is_below(path, folder) and path not in state.folders for path in local_directories
        )

    def _remote_subtree_changed(
        self,
        folder: str,
        remote: RemoteSnapshot,
        state: SyncState,
    ) -> bool:
        for path, entry in remote.files.items():
            if is_below(path, folder):
                record = state.files.get(path)
                if record is None or self._remote_changed(record, entry):
                    return True
        return any(is_below(path, folder) and path not in state.folders for path in remote.folders)

    def _remove_remote_subtree(self, folder: str, remote: RemoteSnapshot) -> None:
        remote.files = {
            path: entry for path, entry in remote.files.items() if not is_below(path, folder)
        }
        remote.folders = {
            path: entry for path, entry in remote.folders.items() if not is_below(path, folder)
        }

    def _synchronize_folders(
        self,
        remote: RemoteSnapshot,
        local_files: dict[str, LocalFile],
        local_directories: set[str],
        state: SyncState,
    ) -> None:
        removed_remote_roots: list[str] = []
        for path in sorted(remote.folders, key=lambda item: (item.count("/"), item)):
            if any(is_below(path, removed) for removed in removed_remote_roots):
                continue
            if path in local_directories or path not in state.folders:
                continue
            if self._remote_subtree_changed(path, remote, state):
                safe_join(self.sync_dir, path).mkdir(parents=True, exist_ok=True)
                local_directories.add(path)
                self._event("folder_downloaded", path)
                continue
            folder = remote.folders[path]
            self.api.delete_folder(folder.id)
            self._remove_remote_subtree(path, remote)
            removed_remote_roots.append(path)
            self._deleted += 1
            self._event("folder_deleted_remote", path)

        archived_local_roots: list[str] = []
        for path in sorted(local_directories, key=lambda item: (item.count("/"), item)):
            if any(is_below(path, archived) for archived in archived_local_roots):
                continue
            if path in remote.folders or path not in state.folders:
                continue
            if self._local_subtree_changed(path, local_files, local_directories, state):
                self._ensure_remote_folder(path, remote, state)
                continue
            absolute = safe_join(self.sync_dir, path)
            if absolute.exists():
                self._archive_local(absolute, path)
            archived_local_roots.append(path)
            for file_path in list(local_files):
                if is_below(file_path, path):
                    del local_files[file_path]
            for directory_path in list(local_directories):
                if is_below(directory_path, path):
                    local_directories.discard(directory_path)
            for file_path in list(state.files):
                if is_below(file_path, path):
                    del state.files[file_path]
            for directory_path in list(state.folders):
                if is_below(directory_path, path):
                    del state.folders[directory_path]
            self._deleted += 1
            self._event("folder_deleted_local", path)

        for path in sorted(local_directories, key=lambda item: (item.count("/"), item)):
            if path not in remote.folders:
                self._ensure_remote_folder(path, remote, state)
        for path in sorted(remote.folders, key=lambda item: (item.count("/"), item)):
            if path not in local_directories:
                safe_join(self.sync_dir, path).mkdir(parents=True, exist_ok=True)
                local_directories.add(path)
                self._event("folder_downloaded", path)

    def _synchronize_files(
        self,
        remote: RemoteSnapshot,
        local_files: dict[str, LocalFile],
        state: SyncState,
    ) -> None:
        all_paths = sorted(
            set(local_files) | set(remote.files) | set(state.files) | set(state.skipped_uploads)
        )
        max_upload_size = self.api.get_max_upload_size()

        for path in all_paths:
            local = local_files.get(path)
            remote_entry = remote.files.get(path)
            record = state.files.get(path)
            if local is None:
                state.skipped_uploads.pop(path, None)
            try:
                if record and local and remote_entry:
                    local_changed = self._local_changed(record, local)
                    remote_changed = self._remote_changed(record, remote_entry)
                    if local_changed and remote_changed:
                        if not self._upload_is_blocked(path, local, max_upload_size, state):
                            self._resolve_conflict(path, local, remote_entry, remote, state)
                    elif local_changed:
                        if not self._upload_is_blocked(path, local, max_upload_size, state):
                            self._upload(path, local, remote, state, replace=remote_entry)
                    elif remote_changed:
                        self._download(path, remote_entry, remote, state)
                    else:
                        state.skipped_uploads.pop(path, None)
                        state.files[path] = self._file_state(remote_entry, local)
                elif record and local and not remote_entry:
                    if self._local_changed(record, local):
                        if not self._upload_is_blocked(path, local, max_upload_size, state):
                            self._upload(path, local, remote, state)
                    else:
                        self._archive_local(local.absolute_path, path)
                        state.files.pop(path, None)
                        self._deleted += 1
                        self._event("file_deleted_local", path)
                elif record and not local and remote_entry:
                    if self._remote_changed(record, remote_entry):
                        self._download(path, remote_entry, remote, state)
                    else:
                        self.api.delete_file(remote_entry.id)
                        remote.files.pop(path, None)
                        state.files.pop(path, None)
                        self._deleted += 1
                        self._event("file_deleted_remote", path)
                elif record and not local and not remote_entry:
                    state.files.pop(path, None)
                elif not record and local and remote_entry:
                    if not self._upload_is_blocked(path, local, max_upload_size, state):
                        self._resolve_new_collision(path, local, remote_entry, remote, state)
                elif local:
                    if not self._upload_is_blocked(path, local, max_upload_size, state):
                        self._upload(path, local, remote, state)
                elif remote_entry:
                    self._download(path, remote_entry, remote, state)
            except ApiError as error:
                if (error.status_code == 413 or error.code == "too_large") and local:
                    self._remember_skipped_upload(
                        path,
                        local,
                        max_upload_size,
                        state,
                        detail=str(error),
                        level="error",
                    )
                else:
                    self._event("item_sync_failed", path, str(error), "error")
                self._last_error = str(error)
            except (OSError, ValueError) as error:
                self._event("item_sync_failed", path, str(error), "error")
                self._last_error = str(error)

    def _refresh_folder_state(self, remote: RemoteSnapshot, state: SyncState) -> None:
        local_files, local_directories = scan_local(self.sync_dir, state)
        state.folders = {
            path: entry.id for path, entry in remote.folders.items() if path in local_directories
        }
        for path in list(state.files):
            if path not in local_files and path not in remote.files:
                del state.files[path]
        for path in list(state.skipped_uploads):
            if path not in local_files:
                del state.skipped_uploads[path]

    def sync_once(self) -> None:
        """Run one non-overlapping bidirectional synchronization pass."""
        if not self._sync_lock.acquire(blocking=False):
            return
        try:
            self._uploaded = 0
            self._downloaded = 0
            self._deleted = 0
            self._conflicts = 0
            self._set_status(phase=SyncPhase.SYNCING, current_action="", last_error="")
            self.sync_dir.mkdir(parents=True, exist_ok=True)
            state = self.state_store.load()
            remote = self.api.get_snapshot()
            local_files, local_directories = scan_local(self.sync_dir, state)
            self._synchronize_folders(remote, local_files, local_directories, state)
            self._synchronize_files(remote, local_files, state)
            self._refresh_folder_state(remote, state)
            self.state_store.save(state)
            with self._status_lock:
                self._remote = remote
                self._last_sync = datetime.now()
                self._phase = SyncPhase.ERROR if self._last_error else SyncPhase.IDLE
                self._current_action = ""
                self._generation += 1
            self._event("sync_completed")
        except ApiError as error:
            phase = SyncPhase.OFFLINE if error.code == "connection_error" else SyncPhase.ERROR
            self._set_status(phase=phase, current_action="", last_error=str(error))
            self._event("sync_failed", detail=str(error), level="error")
        except (OSError, ValueError) as error:
            self._set_status(phase=SyncPhase.ERROR, current_action="", last_error=str(error))
            self._event("sync_failed", detail=str(error), level="error")
        finally:
            self._sync_lock.release()
