import asyncio
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import flet as ft

from lyrarma_cloud_client.config import AppSettings
from lyrarma_cloud_client.models import FileEntry, FolderEntry, SyncPhase, SyncView
from lyrarma_cloud_client.ui import DesktopClient, configure_desktop_backend
from lyrarma_cloud_client.ui.theme import DEFAULT_THEME


class DummyPage:
    def __init__(self) -> None:
        self.services = []
        self.controls = []
        self.title = ""

    def clean(self) -> None:
        self.controls.clear()

    def add(self, *controls) -> None:
        self.controls.extend(controls)

    def update(self) -> None:
        pass

    def show_dialog(self, dialog) -> None:
        self.dialog = dialog

    def pop_dialog(self) -> None:
        self.dialog = None

    def run_task(self, _handler, *_args) -> None:
        pass


class DummyEngine:
    def __init__(self, view: SyncView) -> None:
        self._view = view

    def view(self) -> SyncView:
        return self._view

    def request_sync(self) -> None:
        pass


def make_view() -> SyncView:
    folder = FolderEntry(
        id="folder-1",
        owner_id="user",
        parent_id="root",
        name="Documents",
        is_public=False,
        created_at="2026-01-01T00:00:00Z",
    )
    file = FileEntry(
        id="file-1",
        owner_id="user",
        folder_id="root",
        original_name="welcome.txt",
        size=7,
        content_type="text/plain",
        is_public=True,
        uploaded_at="2026-01-01T00:00:00Z",
    )
    return SyncView(
        phase=SyncPhase.IDLE,
        current_action="",
        last_sync=datetime.now(),
        last_error="",
        uploaded=0,
        downloaded=1,
        deleted=0,
        conflicts=0,
        generation=1,
        files={"welcome.txt": file},
        folders={"Documents": folder},
        events=(),
    )


def assert_valid_wrapping_rows(control: ft.Control) -> None:
    if isinstance(control, ft.Row) and control.wrap:
        assert not any(child.expand for child in control.controls)
    for child in getattr(control, "controls", ()) or ():
        assert_valid_wrapping_rows(child)
    content = getattr(control, "content", None)
    if isinstance(content, ft.Control):
        assert_valid_wrapping_rows(content)


def test_wayland_uses_x11_backend_without_overriding_explicit_choice(monkeypatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.delenv("GDK_BACKEND", raising=False)

    configure_desktop_backend("linux")

    assert os.environ["GDK_BACKEND"] == "x11"

    monkeypatch.setenv("GDK_BACKEND", "wayland")
    configure_desktop_backend("linux")
    assert os.environ["GDK_BACKEND"] == "wayland"


def test_all_primary_flet_screens_build(tmp_path: Path) -> None:
    page = DummyPage()
    client = DesktopClient(page)
    client.settings = AppSettings(
        api_url="https://cloud.example.test",
        public_url="https://share.example.test",
        username="alice",
        language="en",
        sync_dir=str(tmp_path / "sync"),
        sync_interval=15,
    )
    client.translator.set_language("en")

    client._render_login()
    assert page.controls

    view = make_view()
    client.engine = DummyEngine(view)
    client.screen = "dashboard"
    for section in range(3):
        client.section = section
        client._render_dashboard(view)
        assert page.controls
        root = page.controls[0]
        assert root.controls[0].height == 58
        workspace = root.controls[1].controls[1]
        assert workspace.bgcolor == DEFAULT_THEME.background
        assert isinstance(workspace.content, ft.Column)
        assert workspace.content.scroll == ft.ScrollMode.AUTO
        assert_valid_wrapping_rows(root)


class DummyWindow:
    def __init__(self) -> None:
        self.visible = True
        self.prevent_close = True
        self.destroyed = False
        self.brought_to_front = False

    async def destroy(self) -> None:
        self.destroyed = True

    async def to_front(self) -> None:
        self.brought_to_front = True


class DummyTray:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def test_only_close_window_event_hides_the_application() -> None:
    page = DummyPage()
    page.window = DummyWindow()
    client = DesktopClient(page)
    client.tray = DummyTray()

    client._on_window_event(SimpleNamespace(type=ft.WindowEventType.FOCUS))
    assert page.window.visible

    client._on_window_event(SimpleNamespace(type=ft.WindowEventType.CLOSE))
    assert not page.window.visible


def test_tray_exit_stops_services_and_destroys_window() -> None:
    page = DummyPage()
    page.window = DummyWindow()
    client = DesktopClient(page)
    tray = DummyTray()
    client.tray = tray
    stopped = []
    client._stop_engine = lambda: stopped.append(True)

    asyncio.run(client._exit_application())

    assert tray.stopped
    assert stopped == [True]
    assert not page.window.prevent_close
    assert page.window.destroyed
