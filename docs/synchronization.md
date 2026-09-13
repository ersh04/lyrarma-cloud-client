# Synchronization and uploads

## Upload path

1. The Flet file picker returns local paths selected by the user.
2. `ui.uploads.stage_files()` copies each file to an ignored temporary name in the current sync directory.
3. `os.replace()` publishes the complete copy atomically. The watcher therefore cannot upload a partially copied file.
4. `SyncEngine` scans the directory, reuses unchanged hashes from state, and hashes new or modified content with SHA-256.
5. Missing remote folders are created from parent to child.
6. `CloudApi.upload_file()` streams the file as the multipart field `file` to `POST /api/files/upload?folder_id=<id>` with the Bearer token.
7. For a replacement, the old remote entry is deleted only after the new upload succeeds.
8. State is saved only after the pass has reconciled local and remote snapshots.

Before processing outbound changes, the engine obtains `max_upload_size` from `GET /api/info/max_upload_size`. A larger local file is not sent to the upload endpoint. Its content fingerprint and the observed limit are persisted in `skipped_uploads`, so unchanged files do not generate repeated attempts or activity events. The file becomes eligible again after its content changes or the server limit increases. The server's authoritative `413 too_large` response remains a fallback for limit changes that occur between the check and upload.

## Downloads

Downloads are streamed in 1 MiB chunks to a temporary file in the destination directory. The final name is installed with `os.replace()` only after the response completes. Interrupted downloads cannot replace a valid local file.

## Change decisions

For a path known from the previous successful pass, the engine compares both local content metadata and remote entry metadata:

| Local side | Remote side | Action |
| --- | --- | --- |
| Changed | Unchanged | Upload a new remote entry, then delete the old entry. |
| Unchanged | Changed | Download the remote entry atomically. |
| Changed | Changed | Keep the remote name and upload the local content as a conflict copy. |
| Deleted | Unchanged | Delete the remote entry. |
| Unchanged | Deleted | Move the local item to recoverable client history. |

A new path that exists on both sides is compared by content. Identical content is adopted without another upload; different content follows conflict-copy handling.

## Safety boundaries

- Remote names are sanitized for Windows-reserved names and invalid path characters.
- All remote paths pass through `safe_join()` to reject absolute paths and traversal.
- Symbolic links, operating-system metadata, and client temporary files are ignored.
- Cloud deletions are irreversible because the current server API has no recycle-bin endpoint. Files removed due to a remote deletion are retained in local client history.
