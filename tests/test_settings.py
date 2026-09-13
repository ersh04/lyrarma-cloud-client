from pathlib import Path

from lyrarma_cloud_client.config import AppSettings, CredentialStore, SettingsStore


def test_app_settings_read_new_environment_names(monkeypatch) -> None:
    monkeypatch.setenv("CLOUD_API_URL", "https://api.example.test")
    monkeypatch.setenv("CLOUD_PUBLIC_URL", "https://share.example.test")
    monkeypatch.setenv("CLOUD_USERNAME", "alice")
    monkeypatch.setenv("CLOUD_SYNC_DIR", "/tmp/example-sync")
    monkeypatch.setenv("CLOUD_SYNC_INTERVAL", "27")
    monkeypatch.setenv("APP_LANGUAGE", "ru")

    settings = AppSettings()

    assert settings.api_url == "https://api.example.test"
    assert settings.public_url == "https://share.example.test"
    assert settings.username == "alice"
    assert settings.sync_dir == "/tmp/example-sync"
    assert settings.sync_interval == 27
    assert settings.language == "ru"


def test_non_empty_environment_overrides_saved_settings(
    monkeypatch,
    tmp_path: Path,
) -> None:
    store = SettingsStore(tmp_path)
    store.save(
        AppSettings(
            api_url="https://old-api.example.test",
            public_url="https://old-share.example.test",
            username="old-user",
            language="en",
            sync_dir="/tmp/old-sync",
            sync_interval=15,
        )
    )
    monkeypatch.setenv("CLOUD_API_URL", "https://new-api.example.test")
    monkeypatch.setenv("CLOUD_PUBLIC_URL", "https://new-share.example.test")
    monkeypatch.setenv("CLOUD_USERNAME", "new-user")
    monkeypatch.setenv("CLOUD_SYNC_DIR", "/tmp/new-sync")
    monkeypatch.setenv("CLOUD_SYNC_INTERVAL", "30")
    monkeypatch.setenv("APP_LANGUAGE", "ru")

    settings = store.load()

    assert settings.api_url == "https://new-api.example.test"
    assert settings.public_url == "https://new-share.example.test"
    assert settings.username == "new-user"
    assert settings.sync_dir == "/tmp/new-sync"
    assert settings.sync_interval == 30
    assert settings.language == "ru"


def test_empty_environment_keeps_saved_optional_settings(
    monkeypatch,
    tmp_path: Path,
) -> None:
    store = SettingsStore(tmp_path)
    store.save(
        AppSettings(
            api_url="https://saved.example.test",
            public_url="",
            username="alice",
            language="ru",
            sync_dir="/tmp/saved-sync",
            sync_interval=21,
        )
    )
    for name in (
        "CLOUD_API_URL",
        "CLOUD_PUBLIC_URL",
        "CLOUD_USERNAME",
        "CLOUD_SYNC_DIR",
        "CLOUD_SYNC_INTERVAL",
        "APP_LANGUAGE",
    ):
        monkeypatch.setenv(name, "")

    settings = store.load()

    assert settings.api_url == "https://saved.example.test"
    assert settings.username == "alice"
    assert settings.sync_dir == "/tmp/saved-sync"
    assert settings.sync_interval == 21
    assert settings.language == "ru"


def test_environment_credentials_use_new_names(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLOUD_API_TOKEN", "environment-token")
    monkeypatch.setenv("CLOUD_USERNAME", "alice")

    credentials = CredentialStore(tmp_path).load(
        "https://api.example.test",
        "alice",
    )

    assert credentials == ("environment-token", "")
