# Lyrarma Cloud Desktop

A desktop client for [Lyrarma Cloud](https://github.com/ersh04/lyrarma-cloud),
built with Python and Flet. It uses the same dark color palette as the web app
while adding background two-way synchronization, a local sync folder, an event
log, search, safe conflict resolution, and in-app settings.

## Features

- Sign in or create an account through the official HTTP API.
- Switch between English and Russian using a shared JSON translation catalog.
- Automatically create the local `~/lyrarma-cloud` folder.
- Detect local changes immediately with `watchdog`.
- Periodically check for changes made on other computers.
- Synchronize nested folders, new file versions, and deletions.
- Create conflict copies when the same file is edited in two places.
- Keep a recoverable local history of files deleted from the cloud.
- Enable or disable public access and copy public links.
- Add files manually, search, review the event log, and change the sync folder.

## Quick start

Python 3.10 or later is required. A Lyrarma Cloud server must already be running.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e .
python src/main.py
```

On Linux Wayland sessions, the client automatically uses XWayland to avoid a
Flutter 3.44 rendering regression. Set `GDK_BACKEND=wayland` before launch to
opt out. Flet uses Zenity for the system file picker. If the picker does not
open, install `zenity` with your distribution's package manager.

## Configuration

Copy the example environment file and set the URL of your server:

```sh
cp .env.example .env
```

| Variable | Description |
| --- | --- |
| `CLOUD_API_URL` | Base URL of the HTTP API, without a trailing `/`. |
| `CLOUD_PUBLIC_URL` | External URL used for public links. It may differ from the internal API URL. |
| `CLOUD_API_TOKEN` | Optional pre-issued JWT. The client normally obtains one through the sign-in form. |
| `CLOUD_USERNAME` | Optional username associated with the token. |
| `CLOUD_SYNC_DIR` | Local sync folder. Leave it empty to use `~/lyrarma-cloud`. |
| `CLOUD_SYNC_INTERVAL` | Cloud polling interval, from 3 to 3,600 seconds. |
| `APP_LANGUAGE` | Initial interface language: `en` or `ru`. |
| `CLOUD_CONNECT_TIMEOUT` | Connection timeout in seconds. |
| `CLOUD_TRANSFER_TIMEOUT` | Upload and download timeout in seconds. |

Non-empty values from `.env` override settings previously saved through the app.
Empty optional values do not reset saved settings.

The `.env` file is excluded from Git. A JWT obtained through the sign-in form is
stored in the app's private data file with `0600` permissions on POSIX systems;
the password is never stored. The data directory depends on the operating system:

- Linux: `~/.config/lyrarma-cloud-client`
- macOS: `~/Library/Application Support/lyrarma-cloud-client`
- Windows: `%APPDATA%\lyrarma-cloud-client`

## How synchronization works

The server API does not provide a change cursor, a content hash, or a way to
update a file in place. The client therefore keeps separate state for every
account and local-folder pair, then compares the remote tree with the local one.

When a local file changes, the client first uploads the complete file as a new
version. It removes the previous server entry only after the upload succeeds. If
the file has changed both locally and in the cloud since the last synchronization,
the cloud version keeps the original name. The local version is saved alongside
it with a `(conflict-<computer>-<date>)` suffix and is uploaded as well.

Files deleted on another computer are moved out of the sync folder and into
`local-history` in the client's data directory. A local deletion is propagated
to the server. The current server API has no recycle bin, so cloud deletions
cannot be undone.

Symbolic links and the client's internal temporary files are not synchronized.

## Developer documentation

- [Architecture](docs/architecture.md)
- [Synchronization and uploads](docs/synchronization.md)

## Checks

Install the development dependencies and run the linter and test suite:

```sh
pip install -e '.[dev]'
ruff check src tests
pytest -q
```

## Building the application

Flet builds a separate package for the current target platform:

```sh
flet build linux
# flet build windows
# flet build macos
```

Product metadata and entry-point settings are defined in `pyproject.toml`.

To rebuild the local PyInstaller distribution:

```sh
pip install -e '.[dev]'
pyinstaller --noconfirm --clean main.spec
./dist/main/main
```

`dist/main/main` is a native Linux ELF executable and must be started directly,
not through Wine. Build the Windows `.exe` on Windows.

## Project structure

```text
src/
├── main.py
└── lyrarma_cloud_client/
    ├── api/                 # HTTP client and remote snapshot mapping
    ├── assets/              # Packaged translations, theme, and icon
    ├── config/              # Environment, settings, credentials, and i18n
    ├── sync/                # Engine and filesystem helpers
    ├── ui/                  # Flet application, theme, and file staging
    ├── models.py
    └── state.py
docs/
tests/
```
