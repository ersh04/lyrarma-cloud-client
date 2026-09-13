"""Persistent application settings and credential storage."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

APP_DIR_NAME = "lyrarma-cloud-client"


def load_environment() -> None:
    project_file = Path(__file__).resolve().parents[3] / ".env"
    working_file = Path.cwd() / ".env"
    if working_file.exists():
        load_dotenv(working_file, override=False)
    elif project_file.exists():
        load_dotenv(project_file, override=False)


load_environment()


def _environment_text(name: str) -> str:
    return os.getenv(name, "").strip()


def _environment_int(name: str, fallback: int) -> int:
    value = _environment_text(name)
    if not value:
        return fallback
    try:
        return int(value)
    except ValueError:
        return fallback


def _bounded_interval(value: object, fallback: int = 15) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError):
        interval = fallback
    return max(3, min(interval, 3600))


def application_data_dir() -> Path:
    """Return the platform-specific private application data directory."""
    flet_path = os.getenv("FLET_APP_STORAGE_DATA")
    if flet_path:
        return Path(flet_path).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_DIR_NAME


def default_sync_dir() -> Path:
    """Return the configured sync directory or the user-level default."""
    configured = _environment_text("CLOUD_SYNC_DIR")
    return Path(configured).expanduser() if configured else Path.home() / "lyrarma-cloud"


@dataclass(slots=True)
class AppSettings:
    """Runtime settings loaded from JSON and non-empty environment values."""

    api_url: str = field(default_factory=lambda: _environment_text("CLOUD_API_URL"))
    public_url: str = field(default_factory=lambda: _environment_text("CLOUD_PUBLIC_URL"))
    username: str = field(default_factory=lambda: _environment_text("CLOUD_USERNAME"))
    language: str = field(default_factory=lambda: _environment_text("APP_LANGUAGE"))
    sync_dir: str = field(default_factory=lambda: _environment_text("CLOUD_SYNC_DIR"))
    sync_interval: int = field(default_factory=lambda: _environment_int("CLOUD_SYNC_INTERVAL", 15))

    def normalized_sync_dir(self) -> Path:
        return Path(self.sync_dir).expanduser() if self.sync_dir else default_sync_dir()

    def normalized_public_url(self) -> str:
        return (self.public_url or self.api_url).rstrip("/")


def _apply_environment_overrides(settings: AppSettings) -> AppSettings:
    string_fields = {
        "CLOUD_API_URL": "api_url",
        "CLOUD_PUBLIC_URL": "public_url",
        "CLOUD_USERNAME": "username",
        "APP_LANGUAGE": "language",
        "CLOUD_SYNC_DIR": "sync_dir",
    }
    for environment_name, field_name in string_fields.items():
        value = _environment_text(environment_name)
        if value:
            setattr(settings, field_name, value)

    interval = _environment_text("CLOUD_SYNC_INTERVAL")
    if interval:
        settings.sync_interval = _environment_int(
            "CLOUD_SYNC_INTERVAL",
            settings.sync_interval,
        )
    settings.sync_interval = _bounded_interval(settings.sync_interval)
    return settings


class JsonFile:
    """Small atomic JSON file adapter used by application stores."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self, default: Any) -> Any:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return default

    def write(self, value: Any, private: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if private:
            with contextlib.suppress(OSError):
                temporary.chmod(0o600)
        os.replace(temporary, self.path)


class SettingsStore:
    """Persist user-editable application settings."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or application_data_dir()
        self.file = JsonFile(self.data_dir / "settings.json")

    def load(self) -> AppSettings:
        raw = self.file.read({})
        allowed = AppSettings.__dataclass_fields__.keys()
        values = {key: raw[key] for key in allowed if key in raw}
        return _apply_environment_overrides(AppSettings(**values))

    def save(self, settings: AppSettings) -> None:
        self.file.write(asdict(settings))


class CredentialStore:
    """Persist the access token without ever storing the password."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.file = JsonFile((data_dir or application_data_dir()) / "credentials.json")

    def load(self, api_url: str, username: str) -> tuple[str, str] | None:
        environment_token = _environment_text("CLOUD_API_TOKEN")
        environment_username = _environment_text("CLOUD_USERNAME")
        if environment_token and environment_username == username and bool(username):
            return environment_token, ""
        value = self.file.read({})
        if value.get("api_url") != api_url or value.get("username") != username:
            return None
        token = str(value.get("token", ""))
        expires_at = str(value.get("expires_at", ""))
        return (token, expires_at) if token else None

    def save(self, api_url: str, username: str, token: str, expires_at: str) -> None:
        self.file.write(
            {
                "api_url": api_url,
                "username": username,
                "token": token,
                "expires_at": expires_at,
            },
            private=True,
        )

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.file.path.unlink()
