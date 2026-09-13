"""Filesystem helpers used by the synchronization engine."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from watchdog.events import FileSystemEvent, FileSystemEventHandler

from ..models import LocalFile, SyncState

IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
INTERNAL_PREFIXES = (".lyrarma-download-", ".lyrarma-compare-")


def hash_file(path: Path) -> str:
    """Return the SHA-256 digest of a local file without loading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_ignored(path: Path) -> bool:
    """Return whether a path is metadata or a client-owned temporary file."""
    return path.name in IGNORED_NAMES or path.name.startswith(INTERNAL_PREFIXES)


def is_below(path: str, parent: str) -> bool:
    """Return whether a POSIX-style relative path belongs to a subtree."""
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def safe_join(root: Path, relative_path: str) -> Path:
    """Resolve a relative synchronization path while preventing directory traversal."""
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe relative path: {relative_path}")
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*relative.parts).resolve(strict=False)
    if os.path.commonpath((str(root_resolved), str(candidate))) != str(root_resolved):
        raise ValueError(f"Path escapes synchronization directory: {relative_path}")
    return candidate


def scan_local(root: Path, state: SyncState) -> tuple[dict[str, LocalFile], set[str]]:
    """Build a snapshot of regular local files and directories under the sync root."""
    local_files: dict[str, LocalFile] = {}
    directories: set[str] = set()
    root.mkdir(parents=True, exist_ok=True)
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directory_names[:] = [
            name
            for name in directory_names
            if not is_ignored(current_path / name) and not (current_path / name).is_symlink()
        ]
        for name in directory_names:
            relative = (current_path / name).relative_to(root).as_posix()
            directories.add(relative)
        for name in file_names:
            absolute = current_path / name
            if is_ignored(absolute) or absolute.is_symlink():
                continue
            try:
                metadata = absolute.stat()
                relative = absolute.relative_to(root).as_posix()
                previous = state.files.get(relative)
                skipped = state.skipped_uploads.get(relative)
                if (
                    previous
                    and previous.local_size == metadata.st_size
                    and previous.local_mtime_ns == metadata.st_mtime_ns
                    and previous.local_hash
                ):
                    digest = previous.local_hash
                elif (
                    skipped
                    and skipped.local_size == metadata.st_size
                    and skipped.local_mtime_ns == metadata.st_mtime_ns
                    and skipped.local_hash
                ):
                    digest = skipped.local_hash
                else:
                    digest = hash_file(absolute)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            local_files[relative] = LocalFile(
                absolute_path=absolute,
                relative_path=relative,
                size=metadata.st_size,
                mtime_ns=metadata.st_mtime_ns,
                digest=digest,
            )
    return local_files, directories


class ChangeHandler(FileSystemEventHandler):
    """Wake the synchronization engine after a relevant filesystem event."""

    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback

    def on_any_event(self, event: FileSystemEvent) -> None:
        """Forward non-internal file events to the configured callback."""
        if not is_ignored(Path(event.src_path)):
            self.callback()
