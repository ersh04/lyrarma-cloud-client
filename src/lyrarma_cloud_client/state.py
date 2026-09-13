"""Account-specific persistence for synchronization metadata."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from .config import JsonFile
from .models import FileState, SkippedUpload, SyncState


def account_identifier(api_url: str, username: str) -> str:
    """Return a stable non-secret key for an account namespace."""
    source = f"{api_url.rstrip('/').lower()}\0{username.lower()}".encode()
    return hashlib.sha256(source).hexdigest()[:20]


class StateStore:
    """Persist sync metadata and own the recoverable local-history directory."""

    def __init__(
        self,
        data_dir: Path,
        api_url: str,
        username: str,
        sync_dir: Path | None = None,
    ) -> None:
        identifier = account_identifier(api_url, username)
        root_source = str(sync_dir.expanduser().resolve()) if sync_dir else "default"
        root_identifier = hashlib.sha256(root_source.encode()).hexdigest()[:12]
        account_dir = data_dir / "accounts" / identifier
        self.file = JsonFile(account_dir / f"sync-state-{root_identifier}.json")
        self.history_dir = account_dir / "local-history"
        self._lock = threading.RLock()

    def load(self) -> SyncState:
        with self._lock:
            raw = self.file.read({})
        files = {
            path: FileState.from_dict(value)
            for path, value in raw.get("files", {}).items()
            if isinstance(path, str) and isinstance(value, dict)
        }
        folders = {str(path): str(remote_id) for path, remote_id in raw.get("folders", {}).items()}
        skipped_uploads = {
            path: SkippedUpload.from_dict(value)
            for path, value in raw.get("skipped_uploads", {}).items()
            if isinstance(path, str) and isinstance(value, dict)
        }
        return SyncState(
            files=files,
            folders=folders,
            skipped_uploads=skipped_uploads,
        )

    def save(self, state: SyncState) -> None:
        value = {
            "version": 2,
            "files": {path: record.to_dict() for path, record in state.files.items()},
            "folders": state.folders,
            "skipped_uploads": {
                path: record.to_dict() for path, record in state.skipped_uploads.items()
            },
        }
        with self._lock:
            self.file.write(value, private=True)
