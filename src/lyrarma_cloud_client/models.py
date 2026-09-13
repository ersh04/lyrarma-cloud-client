"""Immutable API records and mutable synchronization state models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileEntry:
    """Metadata for one file returned by the cloud API."""

    id: str
    owner_id: str
    folder_id: str
    original_name: str
    size: int
    content_type: str
    is_public: bool
    uploaded_at: str

    @classmethod
    def from_dict(cls, value: dict) -> FileEntry:
        return cls(
            id=str(value["id"]),
            owner_id=str(value.get("owner_id", "")),
            folder_id=str(value.get("folder_id") or "root"),
            original_name=str(value["original_name"]),
            size=int(value.get("size", 0)),
            content_type=str(value.get("content_type") or "application/octet-stream"),
            is_public=bool(value.get("is_public", False)),
            uploaded_at=str(value.get("uploaded_at", "")),
        )


@dataclass(frozen=True, slots=True)
class FolderEntry:
    """Metadata for one folder returned by the cloud API."""

    id: str
    owner_id: str
    parent_id: str
    name: str
    is_public: bool
    created_at: str

    @classmethod
    def from_dict(cls, value: dict) -> FolderEntry:
        return cls(
            id=str(value["id"]),
            owner_id=str(value.get("owner_id", "")),
            parent_id=str(value.get("parent_id") or "root"),
            name=str(value["name"]),
            is_public=bool(value.get("is_public", False)),
            created_at=str(value.get("created_at", "")),
        )


@dataclass(slots=True)
class RemoteSnapshot:
    """Resolved remote paths captured during one synchronization pass."""

    files: dict[str, FileEntry] = field(default_factory=dict)
    folders: dict[str, FolderEntry] = field(default_factory=dict)
    duplicate_files: dict[str, list[FileEntry]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LocalFile:
    """Content and filesystem metadata for one local file."""

    absolute_path: Path
    relative_path: str
    size: int
    mtime_ns: int
    digest: str


@dataclass(slots=True)
class FileState:
    """Last synchronized metadata used for three-way change detection."""

    remote_id: str
    remote_uploaded_at: str
    remote_size: int
    local_hash: str
    local_size: int
    local_mtime_ns: int

    @classmethod
    def from_dict(cls, value: dict) -> FileState:
        return cls(
            remote_id=str(value.get("remote_id", "")),
            remote_uploaded_at=str(value.get("remote_uploaded_at", "")),
            remote_size=int(value.get("remote_size", 0)),
            local_hash=str(value.get("local_hash", "")),
            local_size=int(value.get("local_size", 0)),
            local_mtime_ns=int(value.get("local_mtime_ns", 0)),
        )

    def to_dict(self) -> dict:
        return {
            "remote_id": self.remote_id,
            "remote_uploaded_at": self.remote_uploaded_at,
            "remote_size": self.remote_size,
            "local_hash": self.local_hash,
            "local_size": self.local_size,
            "local_mtime_ns": self.local_mtime_ns,
        }


@dataclass(frozen=True, slots=True)
class SkippedUpload:
    """Fingerprint of a local file skipped because it exceeds the server limit."""

    local_hash: str
    local_size: int
    local_mtime_ns: int
    max_upload_size: int

    @classmethod
    def from_dict(cls, value: dict) -> SkippedUpload:
        return cls(
            local_hash=str(value.get("local_hash", "")),
            local_size=int(value.get("local_size", 0)),
            local_mtime_ns=int(value.get("local_mtime_ns", 0)),
            max_upload_size=int(value.get("max_upload_size", 0)),
        )

    def to_dict(self) -> dict:
        return {
            "local_hash": self.local_hash,
            "local_size": self.local_size,
            "local_mtime_ns": self.local_mtime_ns,
            "max_upload_size": self.max_upload_size,
        }


@dataclass(slots=True)
class SyncState:
    """Persisted state for one account and synchronization directory."""

    files: dict[str, FileState] = field(default_factory=dict)
    folders: dict[str, str] = field(default_factory=dict)
    skipped_uploads: dict[str, SkippedUpload] = field(default_factory=dict)


class SyncPhase(str, Enum):
    """Lifecycle state exposed by the synchronization engine."""

    STOPPED = "stopped"
    IDLE = "idle"
    SYNCING = "syncing"
    OFFLINE = "offline"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SyncEvent:
    """One user-visible synchronization activity record."""

    key: str
    path: str = ""
    detail: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    level: str = "info"


@dataclass(frozen=True, slots=True)
class SyncView:
    """Thread-safe snapshot consumed by the user interface."""

    phase: SyncPhase
    current_action: str
    last_sync: datetime | None
    last_error: str
    uploaded: int
    downloaded: int
    deleted: int
    conflicts: int
    generation: int
    files: dict[str, FileEntry]
    folders: dict[str, FolderEntry]
    events: tuple[SyncEvent, ...]
