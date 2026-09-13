from pathlib import Path

from lyrarma_cloud_client.ui.uploads import stage_files


def test_stage_files_copies_atomically_and_replaces_existing_file(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "sync"
    source_dir.mkdir()
    destination_dir.mkdir()
    source = source_dir / "notes.txt"
    source.write_bytes(b"new content")
    (destination_dir / "notes.txt").write_bytes(b"old content")

    result = stage_files([source], destination_dir)

    assert result.copied == 1
    assert result.errors == ()
    assert (destination_dir / "notes.txt").read_bytes() == b"new content"
    assert not list(destination_dir.glob(".lyrarma-download-*.part"))


def test_stage_files_reports_invalid_sources_without_partial_files(tmp_path: Path) -> None:
    destination_dir = tmp_path / "sync"

    result = stage_files([tmp_path / "missing.txt"], destination_dir)

    assert result.copied == 0
    assert len(result.errors) == 1
    assert not list(destination_dir.glob(".lyrarma-download-*.part"))
