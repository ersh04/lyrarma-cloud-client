"""Atomic staging of user-selected files in the synchronization folder."""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class UploadStagingResult:
    """Summary of files copied into the local synchronization folder."""

    copied: int
    errors: tuple[str, ...]


def stage_files(sources: list[Path], destination_dir: Path) -> UploadStagingResult:
    """Copy selected files atomically so the watcher never sees partial content."""
    destination_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    errors: list[str] = []
    for source in sources:
        destination = destination_dir / source.name
        temporary = destination.with_name(f".lyrarma-download-{uuid.uuid4().hex}.part")
        try:
            if not source.is_file():
                raise OSError(f"Not a readable file: {source}")
            if source.resolve() == destination.resolve():
                continue
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
            copied += 1
        except OSError as error:
            errors.append(str(error))
        finally:
            temporary.unlink(missing_ok=True)
    return UploadStagingResult(copied=copied, errors=tuple(errors))
