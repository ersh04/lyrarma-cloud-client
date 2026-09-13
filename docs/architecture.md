# Architecture

The client is organized as a small set of one-way dependencies around the synchronization engine.

```text
main.py
  └── ui
      ├── config
      ├── api
      ├── sync
      │   ├── filesystem
      │   ├── state
      │   └── models
      └── packaged JSON assets
```

## Packages

- `api` owns HTTP authentication, file and folder requests, remote-tree resolution, and safe conversion of remote names to local names.
- `config` owns `.env` loading, persisted user settings, access-token storage, and the English/Russian translation catalog.
- `sync` owns local scanning, file watching, three-way change detection, uploads, downloads, deletions, and conflict copies.
- `ui` owns Flet controls and desktop integration. Theme values and visible translations are loaded from packaged JSON resources.
- `models.py` defines the values shared across layers. `state.py` persists account- and directory-specific synchronization metadata.

The UI never sends a selected file directly to HTTP. It first stages a complete file in the local sync directory, then wakes the engine. This keeps manual file selection and ordinary filesystem changes on the same synchronization path.

## Persistent data

Settings and credentials are stored in the platform application-data directory. Synchronization state is separated by a hash of the server URL, username, and local sync directory. The hash identifies a namespace and does not contain the access token.

Writes to settings and synchronization state use a temporary file followed by `os.replace()`. On POSIX systems, credential and state files are restricted to mode `0600` when possible.

## Threading

Flet renders on its application thread. `SyncEngine` uses one daemon worker for periodic passes and `watchdog` for filesystem notifications. A non-blocking synchronization lock prevents overlapping passes, while a separate re-entrant lock protects status snapshots consumed by the UI.
