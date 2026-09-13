"""Flet desktop application and screen controller."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import flet as ft

from ..api import ApiError, CloudApi, normalize_api_url
from ..config import (
    CredentialStore,
    SettingsStore,
    Translator,
    application_data_dir,
)
from ..models import FileEntry, FolderEntry, SyncEvent, SyncPhase, SyncView
from ..state import StateStore
from ..sync import SyncEngine, is_ignored, safe_join
from .theme import DEFAULT_THEME
from .uploads import stage_files


def format_size(value: int) -> str:
    size = float(value)
    units = ("B", "KB", "MB", "GB", "TB")
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    precision = 0 if unit == "B" else 1
    return f"{size:.{precision}f} {unit}"


def path_parent(value: str) -> str:
    parent = str(PurePosixPath(value).parent)
    return "" if parent == "." else parent


def direct_child(value: str, parent: str) -> bool:
    return path_parent(value) == parent


def token_is_current(expires_at: str) -> bool:
    if not expires_at:
        return True
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed > datetime.now(timezone.utc)
    except ValueError:
        return False


def configure_desktop_backend(platform_name: str | None = None) -> None:
    current_platform = platform_name or sys.platform
    session_type = os.getenv("XDG_SESSION_TYPE", "").casefold()
    if current_platform == "linux" and session_type == "wayland":
        os.environ.setdefault("GDK_BACKEND", "x11")


class DesktopClient:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.data_dir = application_data_dir()
        self.settings_store = SettingsStore(self.data_dir)
        self.credentials = CredentialStore(self.data_dir)
        self.settings = self.settings_store.load()
        self.translator = Translator(self.settings.language or None)
        self.settings.language = self.translator.language
        self.api: CloudApi | None = None
        self.engine: SyncEngine | None = None
        self.token = ""
        self.auth_mode = "login"
        self.screen = "login"
        self.section = 0
        self.current_folder = ""
        self.search_query = ""
        self.monitor_serial = 0
        self.last_rendered_generation = -1
        self.clipboard = ft.Clipboard()
        self.file_picker = ft.FilePicker()
        self.page.services.extend((self.clipboard, self.file_picker))

        palette = DEFAULT_THEME
        for field_name in palette.__dataclass_fields__:
            setattr(self, field_name, getattr(palette, field_name))

    @property
    def translate(self):
        return self.translator.translate

    async def start(self) -> None:
        self.page.title = self.translate("app_name")
        self.page.bgcolor = self.background
        self.page.padding = 0
        self.page.spacing = 0
        self.page.theme_mode = ft.ThemeMode.DARK
        app_theme = ft.Theme(
            color_scheme_seed=self.primary,
            color_scheme=ft.ColorScheme(
                primary=self.primary,
                on_primary=self.text,
                surface=self.surface,
                on_surface=self.text,
                on_surface_variant=self.muted,
                outline=self.border,
                outline_variant=self.border,
                surface_container=self.surface,
                surface_container_low=self.background,
                surface_container_lowest=self.background,
                surface_container_high=self.surface_soft,
                surface_container_highest=self.surface_soft,
                surface_dim=self.background,
            ),
            canvas_color=self.background,
            scaffold_bgcolor=self.background,
            card_bgcolor=self.surface,
            font_family="Segoe UI",
            use_material3=True,
        )
        self.page.theme = app_theme
        self.page.dark_theme = app_theme
        self.page.window.bgcolor = self.background
        self.page.window.width = 1280
        self.page.window.height = 820
        self.page.window.min_width = 920
        self.page.window.min_height = 680
        self.page.on_close = self._on_close
        saved = self.credentials.load(self.settings.api_url, self.settings.username)
        if saved and token_is_current(saved[1]) and self.settings.api_url:
            self.token = saved[0]
            self._show_dashboard()
        else:
            self.credentials.clear()
            self._render_login()

    def _on_close(self, _event=None) -> None:
        self._stop_engine()

    def _stop_engine(self) -> None:
        if self.engine:
            self.engine.stop()
            self.engine = None
        if self.api:
            self.api.close()
            self.api = None

    def _card(
        self,
        content: ft.Control,
        *,
        padding: int | ft.Padding = 18,
        expand: bool | int | None = None,
    ) -> ft.Container:
        return ft.Container(
            content=content,
            padding=padding,
            bgcolor=self.surface,
            border=ft.Border.all(1, self.border),
            border_radius=22,
            shadow=ft.BoxShadow(
                blur_radius=18,
                color="#30000000",
                offset=ft.Offset(0, 6),
            ),
            expand=expand,
        )

    def _primary_button(self, text: str, icon, on_click, **kwargs) -> ft.Button:
        return ft.Button(
            content=text,
            icon=icon,
            bgcolor=self.primary,
            color=self.text,
            elevation=0,
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=12),
                padding=ft.Padding.symmetric(horizontal=20, vertical=15),
            ),
            on_click=on_click,
            **kwargs,
        )

    def _outlined_button(self, text: str, icon, on_click, **kwargs) -> ft.OutlinedButton:
        return ft.OutlinedButton(
            content=text,
            icon=icon,
            style=ft.ButtonStyle(
                color=self.text,
                side=ft.BorderSide(1, self.border),
                shape=ft.RoundedRectangleBorder(radius=12),
                padding=ft.Padding.symmetric(horizontal=18, vertical=14),
            ),
            on_click=on_click,
            **kwargs,
        )

    def _language_switch(self) -> ft.Container:
        controls: list[ft.Control] = []
        for code, key in (("ru", "language_ru_short"), ("en", "language_en_short")):
            selected = self.translator.language == code
            controls.append(
                ft.TextButton(
                    content=self.translate(key),
                    style=ft.ButtonStyle(
                        color=self.text if selected else self.muted,
                        bgcolor=self.primary if selected else self.background,
                        shape=ft.RoundedRectangleBorder(radius=10),
                        padding=ft.Padding.symmetric(horizontal=11, vertical=5),
                    ),
                    on_click=lambda _event, language=code: self._change_language(language),
                )
            )
        return ft.Container(
            content=ft.Row(controls=controls, spacing=2),
            padding=2,
            bgcolor=self.background,
            border=ft.Border.all(1, self.border),
            border_radius=11,
        )

    def _change_language(self, language: str) -> None:
        self.translator.set_language(language)
        self.settings.language = self.translator.language
        self.settings_store.save(self.settings)
        self.page.title = self.translate("app_name")
        if self.screen == "dashboard":
            self._render_dashboard()
        else:
            self._render_login()

    def _header(self, authenticated: bool = False) -> ft.Container:
        logo = ft.Row(
            controls=[
                ft.Icon(ft.Icons.AUTO_AWESOME_ROUNDED, color=self.primary, size=21),
                ft.Text(
                    self.translate("app_name"),
                    size=18,
                    weight=ft.FontWeight.W_700,
                    color=self.primary,
                ),
            ],
            spacing=8,
        )
        actions: list[ft.Control] = [self._language_switch()]
        if authenticated:
            actions.extend(
                [
                    ft.Text(
                        self.settings.username,
                        size=12,
                        color=self.muted,
                        max_lines=1,
                    ),
                    ft.PopupMenuButton(
                        icon=ft.Icons.ACCOUNT_CIRCLE_OUTLINED,
                        icon_color=self.text,
                        tooltip=self.settings.username,
                        items=[
                            ft.PopupMenuItem(
                                content=self.settings.username,
                                icon=ft.Icons.PERSON_OUTLINE,
                                disabled=True,
                            ),
                            ft.PopupMenuItem(
                                content=self.translate("logout"),
                                icon=ft.Icons.LOGOUT_ROUNDED,
                                on_click=self._logout,
                            ),
                        ],
                    ),
                ]
            )
        return ft.Container(
            content=ft.Row(
                controls=[logo, ft.Row(controls=actions, spacing=8)],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            height=58,
            padding=ft.Padding.symmetric(horizontal=20),
            bgcolor=self.surface,
            border=ft.Border.only(bottom=ft.BorderSide(1, self.background)),
        )

    def _text_field(self, label: str, value: str = "", **kwargs) -> ft.TextField:
        return ft.TextField(
            value=value,
            label=label,
            color=self.text,
            label_style=ft.TextStyle(color=self.muted),
            bgcolor=self.surface_soft,
            border_color=self.border,
            focused_border_color=self.primary,
            cursor_color=self.primary_hover,
            border_radius=12,
            content_padding=ft.Padding.symmetric(horizontal=16, vertical=14),
            **kwargs,
        )

    def _render_login(self) -> None:
        self.screen = "login"
        title_key = "login_title" if self.auth_mode == "login" else "register_title"
        subtitle_key = "login_subtitle" if self.auth_mode == "login" else "register_subtitle"
        server = self._text_field(
            self.translate("server_url"),
            self.settings.api_url,
            hint_text=self.translate("server_hint"),
            prefix_icon=ft.Icons.DNS_OUTLINED,
            autofocus=not bool(self.settings.api_url),
        )
        username = self._text_field(
            self.translate("username"),
            self.settings.username,
            prefix_icon=ft.Icons.PERSON_OUTLINE,
            autofocus=bool(self.settings.api_url),
        )
        password = self._text_field(
            self.translate("password"),
            password=True,
            can_reveal_password=True,
            prefix_icon=ft.Icons.LOCK_OUTLINE,
        )
        confirm = self._text_field(
            self.translate("confirm_password"),
            password=True,
            can_reveal_password=True,
            prefix_icon=ft.Icons.LOCK_RESET_OUTLINED,
            visible=self.auth_mode == "register",
        )
        error_text = ft.Text("", color=self.danger, size=13, visible=False)
        submit_button: ft.Button

        async def submit(_event=None) -> None:
            error_text.visible = False
            if len(username.value.strip()) < 3:
                error_text.value = self.translate("username_too_short")
                error_text.visible = True
                self.page.update()
                return
            if len(password.value) < 8:
                error_text.value = self.translate("password_too_short")
                error_text.visible = True
                self.page.update()
                return
            if self.auth_mode == "register" and password.value != confirm.value:
                error_text.value = self.translate("passwords_do_not_match")
                error_text.visible = True
                self.page.update()
                return
            try:
                api_url = normalize_api_url(server.value)
            except ValueError:
                error_text.value = self.translate("invalid_server_url")
                error_text.visible = True
                self.page.update()
                return
            submit_button.disabled = True
            submit_button.content = self.translate("connecting")
            self.page.update()
            api = CloudApi(api_url)
            try:
                if self.auth_mode == "register":
                    await asyncio.to_thread(api.register, username.value.strip(), password.value)
                token, expires_at = await asyncio.to_thread(
                    api.login,
                    username.value.strip(),
                    password.value,
                )
            except ApiError as error:
                key = f"api_error_{error.code}"
                message = self.translate(key)
                if message == key:
                    message = self.translate("api_error_request_failed")
                error_text.value = message
                error_text.visible = True
                submit_button.disabled = False
                submit_button.content = self.translate(
                    "sign_in" if self.auth_mode == "login" else "register"
                )
                api.close()
                self.page.update()
                return
            self.token = token
            previous_api_url = self.settings.api_url.rstrip("/")
            previous_public_url = self.settings.public_url.rstrip("/")
            self.settings.api_url = api_url
            if not previous_public_url or previous_public_url == previous_api_url:
                self.settings.public_url = api_url
            self.settings.username = username.value.strip()
            self.settings.sync_dir = str(self.settings.normalized_sync_dir())
            self.settings_store.save(self.settings)
            self.credentials.save(api_url, self.settings.username, token, expires_at)
            api.close()
            self._show_dashboard()

        def change_mode(event) -> None:
            selected = event.control.selected
            self.auth_mode = selected[0] if selected else "login"
            self._render_login()

        auth_switch = ft.SegmentedButton(
            segments=[
                ft.Segment(value="login", label=self.translate("sign_in")),
                ft.Segment(value="register", label=self.translate("register")),
            ],
            selected=[self.auth_mode],
            show_selected_icon=False,
            style=ft.ButtonStyle(
                color={ft.ControlState.SELECTED: self.text, ft.ControlState.DEFAULT: self.muted},
                bgcolor={
                    ft.ControlState.SELECTED: self.primary,
                    ft.ControlState.DEFAULT: self.background,
                },
                side=ft.BorderSide(1, self.border),
            ),
            on_change=change_mode,
        )
        submit_button = self._primary_button(
            self.translate("sign_in" if self.auth_mode == "login" else "register"),
            ft.Icons.LOGIN_ROUNDED if self.auth_mode == "login" else ft.Icons.PERSON_ADD_ALT_1,
            submit,
            width=460,
        )
        form = ft.Column(
            controls=[
                auth_switch,
                ft.Container(height=4),
                server,
                username,
                password,
                confirm,
                error_text,
                submit_button,
                ft.Row(
                    controls=[
                        ft.Icon(ft.Icons.SHIELD_OUTLINED, color=self.muted, size=16),
                        ft.Text(self.translate("remember_note"), color=self.muted, size=12),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
            ],
            spacing=13,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        card = self._card(
            ft.Column(
                controls=[
                    ft.Container(
                        content=ft.Icon(ft.Icons.CLOUD_SYNC_ROUNDED, size=38, color=self.text),
                        width=66,
                        height=66,
                        border_radius=20,
                        bgcolor=self.primary,
                        alignment=ft.Alignment.CENTER,
                    ),
                    ft.Text(self.translate(title_key), size=30, weight=ft.FontWeight.W_700),
                    ft.Text(self.translate(subtitle_key), color=self.muted, size=15),
                    ft.Container(height=8),
                    form,
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=7,
            ),
            padding=30,
        )
        self.page.clean()
        self.page.add(
            ft.Column(
                controls=[
                    self._header(),
                    ft.Container(
                        content=card,
                        alignment=ft.Alignment.CENTER,
                        expand=True,
                        padding=24,
                    ),
                ],
                spacing=0,
                expand=True,
            )
        )
        self.page.update()

    def _start_engine(self) -> None:
        self._stop_engine()
        sync_dir = self.settings.normalized_sync_dir()
        sync_dir.mkdir(parents=True, exist_ok=True)
        self.api = CloudApi(self.settings.api_url, self.token)
        state_store = StateStore(
            self.data_dir,
            self.settings.api_url,
            self.settings.username,
            sync_dir,
        )
        self.engine = SyncEngine(
            self.api,
            sync_dir,
            state_store,
            interval=self.settings.sync_interval,
        )
        self.engine.start()

    def _show_dashboard(self) -> None:
        self.screen = "dashboard"
        self.current_folder = ""
        self.section = 0
        self._start_engine()
        self.monitor_serial += 1
        serial = self.monitor_serial
        self.last_rendered_generation = -1
        self._render_dashboard()
        self.page.run_task(self._monitor, serial)

    async def _monitor(self, serial: int) -> None:
        while self.screen == "dashboard" and serial == self.monitor_serial:
            if self.engine:
                view = self.engine.view()
                if view.generation != self.last_rendered_generation:
                    self.last_rendered_generation = view.generation
                    self._render_dashboard(view)
            await asyncio.sleep(0.8)

    def _logout(self, _event=None) -> None:
        self.monitor_serial += 1
        self._stop_engine()
        self.credentials.clear()
        self.token = ""
        self.auth_mode = "login"
        self._render_login()

    def _select_section(self, index: int) -> None:
        self.section = index
        self._render_dashboard()

    def _nav_button(self, index: int, label: str, icon) -> ft.Container:
        selected = self.section == index
        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Icon(icon, size=21, color=self.text if selected else self.muted),
                    ft.Text(
                        label,
                        color=self.text if selected else self.muted,
                        weight=ft.FontWeight.W_600 if selected else ft.FontWeight.W_400,
                    ),
                ],
                spacing=12,
            ),
            padding=ft.Padding.symmetric(horizontal=15, vertical=12),
            border_radius=12,
            bgcolor=self.primary if selected else None,
            ink=True,
            on_click=lambda _event: self._select_section(index),
        )

    def _status_values(self, phase: SyncPhase) -> tuple[str, str, str]:
        values = {
            SyncPhase.IDLE: ("status_idle", self.success, self.success_bg),
            SyncPhase.SYNCING: ("status_syncing", self.primary_hover, self.surface_soft),
            SyncPhase.OFFLINE: ("status_offline", self.warning, self.warning_bg),
            SyncPhase.ERROR: ("status_error", self.danger, self.danger_bg),
            SyncPhase.STOPPED: ("status_stopped", self.muted, self.background),
        }
        key, color, background = values[phase]
        return self.translate(key), color, background

    def _sidebar(self, view: SyncView) -> ft.Container:
        status, color, status_background = self._status_values(view.phase)
        status_icon = (
            ft.Icons.SYNC_ROUNDED
            if view.phase == SyncPhase.SYNCING
            else ft.Icons.CLOUD_DONE_ROUNDED
        )
        status_card = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Container(
                                content=ft.Icon(status_icon, color=color, size=21),
                                width=38,
                                height=38,
                                bgcolor=status_background,
                                border_radius=12,
                                alignment=ft.Alignment.CENTER,
                            ),
                            ft.Column(
                                controls=[
                                    ft.Text(status, size=13, weight=ft.FontWeight.W_600),
                                    ft.Text(
                                        self._relative_time(view.last_sync),
                                        size=11,
                                        color=self.muted,
                                    ),
                                ],
                                spacing=1,
                                expand=True,
                            ),
                        ]
                    ),
                    ft.Text(
                        str(self.settings.normalized_sync_dir()),
                        size=10,
                        color=self.muted,
                        max_lines=2,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                ],
                spacing=10,
            ),
            padding=13,
            bgcolor=self.background,
            border=ft.Border.all(1, self.border),
            border_radius=16,
        )
        return ft.Container(
            content=ft.Column(
                controls=[
                    status_card,
                    ft.Container(height=5),
                    self._nav_button(0, self.translate("files"), ft.Icons.FOLDER_COPY_OUTLINED),
                    self._nav_button(1, self.translate("activity"), ft.Icons.HISTORY_ROUNDED),
                    self._nav_button(2, self.translate("settings"), ft.Icons.TUNE_ROUNDED),
                    ft.Container(expand=True),
                    self._outlined_button(
                        self.translate("open_folder"),
                        ft.Icons.FOLDER_OPEN_OUTLINED,
                        lambda _event: self._open_path(self.settings.normalized_sync_dir()),
                        width=208,
                    ),
                ],
                spacing=7,
                expand=True,
            ),
            width=248,
            padding=20,
            bgcolor=self.surface,
            border=ft.Border.only(right=ft.BorderSide(1, self.border)),
        )

    def _render_dashboard(self, view: SyncView | None = None) -> None:
        if not self.engine:
            return
        view = view or self.engine.view()
        self.last_rendered_generation = view.generation
        if self.section == 0:
            content = self._files_view(view)
        elif self.section == 1:
            content = self._activity_view(view)
        else:
            content = self._settings_view(view)
        self.page.clean()
        self.page.add(
            ft.Column(
                controls=[
                    self._header(authenticated=True),
                    ft.Row(
                        controls=[
                            self._sidebar(view),
                            ft.Container(
                                content=content,
                                padding=20,
                                bgcolor=self.background,
                                expand=True,
                            ),
                        ],
                        spacing=0,
                        expand=True,
                        vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                    ),
                ],
                spacing=0,
                expand=True,
            )
        )
        self.page.update()

    def _relative_time(self, value: datetime | None) -> str:
        if value is None:
            return self.translate("never")
        seconds = max(0, int((datetime.now() - value).total_seconds()))
        if seconds < 60:
            return self.translate("just_now")
        if seconds < 3600:
            return self.translate("minutes_ago", count=seconds // 60)
        return self.translate("hours_ago", count=seconds // 3600)

    def _format_timestamp(self, value: float | str) -> str:
        try:
            if isinstance(value, str):
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
            else:
                parsed = datetime.fromtimestamp(value)
        except (ValueError, OSError):
            return "—"
        if self.translator.language == "ru":
            return parsed.strftime("%d.%m.%Y %H:%M")
        return parsed.strftime("%b %d, %Y %H:%M")

    def _stat_card(self, label: str, value: str, icon, color: str) -> ft.Container:
        return self._card(
            ft.Row(
                controls=[
                    ft.Container(
                        content=ft.Icon(icon, color=color, size=24),
                        width=46,
                        height=46,
                        bgcolor=self.background,
                        border_radius=14,
                        alignment=ft.Alignment.CENTER,
                    ),
                    ft.Column(
                        controls=[
                            ft.Text(label, color=self.muted, size=12),
                            ft.Text(value, size=20, weight=ft.FontWeight.W_700),
                        ],
                        spacing=1,
                    ),
                ]
            ),
            padding=15,
            expand=True,
        )

    def _files_view(self, view: SyncView) -> ft.Control:
        header = ft.Row(
            controls=[
                ft.Column(
                    controls=[
                        ft.Text(self.translate("my_files"), size=29, weight=ft.FontWeight.W_700),
                        ft.Text(self.translate("my_files_subtitle"), color=self.muted, size=13),
                    ],
                    spacing=2,
                    expand=True,
                ),
                self._outlined_button(
                    self.translate("sync_now"),
                    ft.Icons.SYNC_ROUNDED,
                    lambda _event: self.engine.request_sync() if self.engine else None,
                ),
                self._primary_button(
                    self.translate("add_files"),
                    ft.Icons.UPLOAD_FILE_ROUNDED,
                    self._add_files,
                ),
            ],
        )
        total_size = sum(entry.size for entry in view.files.values())
        stats = ft.Row(
            controls=[
                self._stat_card(
                    self.translate("all_files"),
                    str(len(view.files)),
                    ft.Icons.DESCRIPTION_OUTLINED,
                    self.primary_hover,
                ),
                self._stat_card(
                    self.translate("folders"),
                    str(len(view.folders)),
                    ft.Icons.FOLDER_OUTLINED,
                    self.warning,
                ),
                self._stat_card(
                    self.translate("cloud_size"),
                    format_size(total_size),
                    ft.Icons.CLOUD_OUTLINED,
                    self.success,
                ),
                self._stat_card(
                    self.translate("last_sync"),
                    self._relative_time(view.last_sync),
                    ft.Icons.SCHEDULE_ROUNDED,
                    self.muted,
                ),
            ],
            spacing=12,
        )
        status_message: ft.Control | None = None
        if view.phase == SyncPhase.SYNCING:
            label = (
                self.translate("syncing_item", path=view.current_action)
                if view.current_action
                else self.translate("status_syncing")
            )
            status_message = ft.Container(
                content=ft.Row(
                    controls=[
                        ft.ProgressRing(width=18, height=18, stroke_width=2, color=self.primary),
                        ft.Text(label, size=12, color=self.muted),
                    ]
                ),
                padding=ft.Padding.symmetric(horizontal=14, vertical=9),
                bgcolor=self.surface_soft,
                border_radius=12,
            )
        elif view.last_error:
            status_message = ft.Container(
                content=ft.Text(
                    self.translate("sync_error_detail", error=view.last_error),
                    size=12,
                    color=self.danger,
                ),
                padding=12,
                bgcolor=self.danger_bg,
                border=ft.Border.all(1, self.danger),
                border_radius=12,
            )
        browser = self._browser(view)
        controls = [header, stats]
        if status_message:
            controls.append(status_message)
        controls.append(browser)
        return ft.Column(
            controls=controls,
            spacing=17,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
        )

    def _breadcrumbs(self) -> ft.Row:
        controls: list[ft.Control] = [
            ft.TextButton(
                content=self.translate("root"),
                icon=ft.Icons.CLOUD_OUTLINED,
                style=ft.ButtonStyle(color=self.primary_hover),
                on_click=lambda _event: self._open_folder(""),
            )
        ]
        accumulated: list[str] = []
        for part in PurePosixPath(self.current_folder).parts if self.current_folder else ():
            accumulated.append(part)
            path = "/".join(accumulated)
            controls.extend(
                [
                    ft.Icon(ft.Icons.CHEVRON_RIGHT_ROUNDED, size=17, color=self.muted),
                    ft.TextButton(
                        content=part,
                        style=ft.ButtonStyle(color=self.text),
                        on_click=lambda _event, target=path: self._open_folder(target),
                    ),
                ]
            )
        return ft.Row(controls=controls, spacing=1, scroll=ft.ScrollMode.AUTO)

    def _browser(self, view: SyncView) -> ft.Container:
        directory = safe_join(self.settings.normalized_sync_dir(), self.current_folder)
        directory.mkdir(parents=True, exist_ok=True)
        local_items: dict[str, Path] = {}
        try:
            for item in directory.iterdir():
                if not is_ignored(item) and not item.is_symlink():
                    local_items[item.name] = item
        except OSError:
            pass
        remote_folders = {
            PurePosixPath(path).name: (path, entry)
            for path, entry in view.folders.items()
            if direct_child(path, self.current_folder)
        }
        remote_files = {
            PurePosixPath(path).name: (path, entry)
            for path, entry in view.files.items()
            if direct_child(path, self.current_folder)
        }
        folder_names = {name for name, item in local_items.items() if item.is_dir()} | set(
            remote_folders
        )
        file_names = {name for name, item in local_items.items() if item.is_file()} | set(
            remote_files
        )
        if self.search_query:
            query = self.search_query.casefold()
            folder_names = {name for name in folder_names if query in name.casefold()}
            file_names = {name for name in file_names if query in name.casefold()}

        def submit_search(event) -> None:
            self.search_query = event.control.value.strip()
            self._render_dashboard()

        toolbar = ft.Row(
            controls=[
                self._breadcrumbs(),
                ft.Container(expand=True),
                self._text_field(
                    self.translate("search_files"),
                    self.search_query,
                    prefix_icon=ft.Icons.SEARCH_ROUNDED,
                    dense=True,
                    width=230,
                    on_submit=submit_search,
                ),
                self._outlined_button(
                    self.translate("new_folder"),
                    ft.Icons.CREATE_NEW_FOLDER_OUTLINED,
                    self._new_folder,
                ),
            ],
        )
        controls: list[ft.Control] = [toolbar]
        if folder_names:
            tiles = [
                self._folder_tile(
                    name,
                    remote_folders.get(name, (self._child_path(name), None))[0],
                    remote_folders.get(name, ("", None))[1],
                )
                for name in sorted(folder_names, key=str.casefold)
            ]
            controls.extend(
                [
                    ft.Text(
                        self.translate("folders"),
                        size=13,
                        color=self.muted,
                        weight=ft.FontWeight.W_600,
                    ),
                    ft.Row(controls=tiles, wrap=True, spacing=10, run_spacing=10),
                ]
            )
        if file_names:
            controls.extend(
                [
                    ft.Container(height=2),
                    self._file_table_header(),
                    *[
                        self._file_row(
                            name,
                            local_items.get(name),
                            remote_files.get(name, (self._child_path(name), None))[0],
                            remote_files.get(name, ("", None))[1],
                        )
                        for name in sorted(file_names, key=str.casefold)
                    ],
                ]
            )
        if not folder_names and not file_names:
            controls.append(
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Container(
                                content=ft.Icon(
                                    ft.Icons.FOLDER_OPEN_OUTLINED,
                                    size=42,
                                    color=self.primary_hover,
                                ),
                                width=74,
                                height=74,
                                bgcolor=self.background,
                                border_radius=22,
                                alignment=ft.Alignment.CENTER,
                            ),
                            ft.Text(
                                self.translate("empty_folder"),
                                size=18,
                                weight=ft.FontWeight.W_600,
                            ),
                            ft.Text(
                                self.translate("empty_folder_hint"),
                                size=12,
                                color=self.muted,
                                text_align=ft.TextAlign.CENTER,
                            ),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    alignment=ft.Alignment.CENTER,
                    padding=45,
                )
            )
        return self._card(ft.Column(controls=controls, spacing=11), padding=17)

    def _child_path(self, name: str) -> str:
        return f"{self.current_folder}/{name}" if self.current_folder else name

    def _open_folder(self, path: str) -> None:
        self.current_folder = path
        self.search_query = ""
        self._render_dashboard()

    def _folder_tile(
        self,
        name: str,
        path: str,
        remote: FolderEntry | None,
    ) -> ft.Container:
        async def toggle(event) -> None:
            if not remote or not self.api:
                return
            try:
                await asyncio.to_thread(self.api.set_folder_public, remote.id, event.control.value)
                if self.engine:
                    self.engine.request_sync()
            except ApiError as error:
                event.control.value = not event.control.value
                self._snack(self.translate("operation_failed", error=str(error)), error=True)

        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Container(
                        content=ft.Icon(ft.Icons.FOLDER_ROUNDED, color=self.warning, size=25),
                        width=42,
                        height=42,
                        bgcolor=self.background,
                        border_radius=13,
                        alignment=ft.Alignment.CENTER,
                    ),
                    ft.Column(
                        controls=[
                            ft.Text(
                                name,
                                weight=ft.FontWeight.W_600,
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(
                                self.translate("public")
                                if remote and remote.is_public
                                else self.translate("private"),
                                color=self.muted,
                                size=10,
                            ),
                        ],
                        spacing=1,
                        expand=True,
                    ),
                    ft.Switch(
                        value=bool(remote and remote.is_public),
                        active_track_color=self.primary,
                        disabled=remote is None,
                        on_change=toggle,
                        scale=0.72,
                        tooltip=self.translate("access"),
                    ),
                    ft.IconButton(
                        icon=ft.Icons.DELETE_OUTLINE,
                        icon_color=self.danger,
                        tooltip=self.translate("delete"),
                        on_click=lambda _event: self._confirm_delete(path, name, True),
                    ),
                ]
            ),
            width=330,
            padding=12,
            bgcolor=self.background,
            border=ft.Border.all(1, self.border),
            border_radius=16,
            ink=True,
            on_click=lambda _event: self._open_folder(path),
        )

    def _file_table_header(self) -> ft.Container:
        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Text(self.translate("name"), color=self.muted, size=11, expand=True),
                    ft.Text(self.translate("size"), color=self.muted, size=11, width=90),
                    ft.Text(self.translate("modified"), color=self.muted, size=11, width=150),
                    ft.Text(self.translate("access"), color=self.muted, size=11, width=90),
                    ft.Text(self.translate("actions"), color=self.muted, size=11, width=92),
                ]
            ),
            padding=ft.Padding.symmetric(horizontal=14, vertical=5),
        )

    def _file_icon(self, name: str):
        extension = Path(name).suffix.lower()
        if extension in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
            return ft.Icons.IMAGE_OUTLINED, "#8bd5ff"
        if extension == ".pdf":
            return ft.Icons.PICTURE_AS_PDF_OUTLINED, self.danger
        if extension in {".zip", ".rar", ".7z", ".tar", ".gz"}:
            return ft.Icons.ARCHIVE_OUTLINED, self.warning
        if extension in {".mp3", ".wav", ".flac", ".ogg"}:
            return ft.Icons.AUDIO_FILE_OUTLINED, "#f5b8ff"
        if extension in {".mp4", ".mkv", ".mov", ".avi"}:
            return ft.Icons.VIDEO_FILE_OUTLINED, "#ffb38b"
        if extension in {".py", ".js", ".ts", ".go", ".rs", ".java", ".cs", ".html", ".css"}:
            return ft.Icons.CODE_ROUNDED, self.success
        return ft.Icons.DESCRIPTION_OUTLINED, self.primary_hover

    def _file_row(
        self,
        name: str,
        local: Path | None,
        path: str,
        remote: FileEntry | None,
    ) -> ft.Container:
        icon, color = self._file_icon(name)
        try:
            metadata = local.stat() if local else None
        except OSError:
            metadata = None
        size = remote.size if remote else metadata.st_size if metadata else 0
        modified = (
            self._format_timestamp(remote.uploaded_at)
            if remote
            else self._format_timestamp(metadata.st_mtime)
            if metadata
            else "—"
        )

        async def toggle(event) -> None:
            if not remote or not self.api:
                return
            try:
                await asyncio.to_thread(self.api.set_file_public, remote.id, event.control.value)
                if self.engine:
                    self.engine.request_sync()
            except ApiError as error:
                event.control.value = not event.control.value
                self._snack(self.translate("operation_failed", error=str(error)), error=True)

        async def share(_event) -> None:
            if not remote or not remote.is_public:
                self._snack(self.translate("make_public_first"), error=True)
                return
            url = f"{self.settings.normalized_public_url()}/shared-file/{quote(remote.id)}"
            await self.clipboard.set(url)
            self._snack(self.translate("link_copied"))

        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Container(
                                content=ft.Icon(icon, color=color, size=23),
                                width=40,
                                height=40,
                                bgcolor=self.surface_soft,
                                border_radius=12,
                                alignment=ft.Alignment.CENTER,
                            ),
                            ft.Column(
                                controls=[
                                    ft.Text(
                                        name,
                                        max_lines=1,
                                        overflow=ft.TextOverflow.ELLIPSIS,
                                        weight=ft.FontWeight.W_500,
                                    ),
                                    ft.Text(
                                        self.translate("downloaded_locally")
                                        if local
                                        else self.translate("status_syncing"),
                                        color=self.muted,
                                        size=10,
                                    ),
                                ],
                                spacing=0,
                                expand=True,
                            ),
                        ],
                        spacing=10,
                        expand=True,
                    ),
                    ft.Text(format_size(size), color=self.muted, size=12, width=90),
                    ft.Text(modified, color=self.muted, size=11, width=150),
                    ft.Switch(
                        value=bool(remote and remote.is_public),
                        active_track_color=self.primary,
                        disabled=remote is None,
                        on_change=toggle,
                        scale=0.75,
                        width=90,
                        tooltip=self.translate("access"),
                    ),
                    ft.Row(
                        controls=[
                            ft.IconButton(
                                icon=ft.Icons.LINK_ROUNDED,
                                icon_color=self.primary_hover,
                                tooltip=self.translate("share"),
                                disabled=not bool(remote and remote.is_public),
                                on_click=share,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                icon_color=self.danger,
                                tooltip=self.translate("delete"),
                                on_click=lambda _event: self._confirm_delete(path, name, False),
                            ),
                        ],
                        spacing=0,
                        width=92,
                    ),
                ]
            ),
            padding=ft.Padding.symmetric(horizontal=14, vertical=9),
            bgcolor=self.background,
            border=ft.Border.all(1, self.border),
            border_radius=15,
        )

    async def _add_files(self, _event=None) -> None:
        selected = await self.file_picker.pick_files(
            dialog_title=self.translate("select_files"),
            allow_multiple=True,
        )
        if not selected:
            return
        destination_dir = safe_join(self.settings.normalized_sync_dir(), self.current_folder)
        sources = [Path(item.path) for item in selected if item.path]
        result = stage_files(sources, destination_dir)
        for error in result.errors:
            self._snack(self.translate("operation_failed", error=error), error=True)
        if result.copied:
            self._snack(self.translate("files_added", count=result.copied))
            if self.engine:
                self.engine.request_sync()
            self._render_dashboard()

    def _new_folder(self, _event=None) -> None:
        name = self._text_field(self.translate("folder_name"), autofocus=True)
        error = ft.Text("", color=self.danger, size=12, visible=False)

        def create(_event=None) -> None:
            cleaned = name.value.strip()
            if not cleaned or "/" in cleaned or "\\" in cleaned:
                error.value = self.translate("folder_invalid")
                error.visible = True
                self.page.update()
                return
            target = safe_join(self.settings.normalized_sync_dir(), self._child_path(cleaned))
            if target.exists():
                error.value = self.translate("folder_exists")
                error.visible = True
                self.page.update()
                return
            target.mkdir(parents=True)
            self.page.pop_dialog()
            self._snack(self.translate("folder_created"))
            if self.engine:
                self.engine.request_sync()
            self._render_dashboard()

        dialog = ft.AlertDialog(
            modal=True,
            bgcolor=self.surface,
            title=ft.Text(self.translate("new_folder")),
            content=ft.Column(controls=[name, error], tight=True, width=410),
            actions=[
                ft.TextButton(
                    content=self.translate("cancel"), on_click=lambda _event: self.page.pop_dialog()
                ),
                self._primary_button(self.translate("create"), ft.Icons.ADD_ROUNDED, create),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dialog)

    def _confirm_delete(self, path: str, name: str, is_folder: bool) -> None:
        def remove(_event=None) -> None:
            self.page.pop_dialog()
            if self.engine and self.engine.archive_path(path):
                if is_folder and self.current_folder == path:
                    self.current_folder = path_parent(path)
                self._render_dashboard()

        dialog = ft.AlertDialog(
            modal=True,
            bgcolor=self.surface,
            title=ft.Text(self.translate("confirm_delete_title", name=name)),
            content=ft.Column(
                controls=[
                    ft.Text(
                        self.translate(
                            "confirm_delete_folder" if is_folder else "confirm_delete_file"
                        )
                    ),
                    ft.Container(
                        content=ft.Row(
                            controls=[
                                ft.Icon(ft.Icons.RESTORE_ROUNDED, color=self.warning, size=18),
                                ft.Text(
                                    self.translate("delete_recovery_note"),
                                    color=self.muted,
                                    size=12,
                                    expand=True,
                                ),
                            ]
                        ),
                        padding=12,
                        bgcolor=self.warning_bg,
                        border_radius=12,
                    ),
                ],
                tight=True,
                width=450,
            ),
            actions=[
                ft.TextButton(
                    content=self.translate("cancel"), on_click=lambda _event: self.page.pop_dialog()
                ),
                ft.Button(
                    content=self.translate("delete"),
                    icon=ft.Icons.DELETE_OUTLINE,
                    color=self.text,
                    bgcolor=self.danger_bg,
                    style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12)),
                    on_click=remove,
                ),
            ],
        )
        self.page.show_dialog(dialog)

    def _activity_view(self, view: SyncView) -> ft.Control:
        header = ft.Column(
            controls=[
                ft.Text(self.translate("activity_title"), size=29, weight=ft.FontWeight.W_700),
                ft.Text(self.translate("activity_subtitle"), color=self.muted, size=13),
            ],
            spacing=2,
        )
        if not view.events:
            body = self._card(
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Icon(
                                ft.Icons.HISTORY_TOGGLE_OFF_ROUNDED, size=48, color=self.primary
                            ),
                            ft.Text(self.translate("empty_activity"), size=18),
                            ft.Text(
                                self.translate("empty_activity_hint"), size=12, color=self.muted
                            ),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    alignment=ft.Alignment.CENTER,
                    padding=50,
                )
            )
            return ft.Column(
                controls=[header, body],
                spacing=18,
                expand=True,
                scroll=ft.ScrollMode.AUTO,
            )
        rows = [self._event_row(event) for event in view.events]
        return ft.Column(
            controls=[header, self._card(ft.Column(controls=rows, spacing=0), padding=5)],
            spacing=18,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
        )

    def _event_row(self, event: SyncEvent) -> ft.Container:
        colors = {"error": self.danger, "warning": self.warning, "info": self.primary_hover}
        icons = {
            "error": ft.Icons.ERROR_OUTLINE_ROUNDED,
            "warning": ft.Icons.WARNING_AMBER_ROUNDED,
            "info": ft.Icons.CHECK_CIRCLE_OUTLINE_ROUNDED,
        }
        color = colors.get(event.level, self.primary_hover)
        key = f"event_{event.key}"
        label = self.translate(key)
        if label == key:
            label = event.key.replace("_", " ").title()
        details = event.path
        if event.detail:
            details = f"{details} · {event.detail}" if details else event.detail
        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Container(
                        content=ft.Icon(
                            icons.get(event.level, icons["info"]), color=color, size=20
                        ),
                        width=40,
                        height=40,
                        bgcolor=self.background,
                        border_radius=12,
                        alignment=ft.Alignment.CENTER,
                    ),
                    ft.Column(
                        controls=[
                            ft.Text(label, size=13, weight=ft.FontWeight.W_600),
                            ft.Text(
                                details or self.translate("app_name"),
                                color=self.muted,
                                size=11,
                                max_lines=2,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                        ],
                        spacing=1,
                        expand=True,
                    ),
                    ft.Text(event.created_at.strftime("%H:%M"), color=self.muted, size=11),
                ]
            ),
            padding=ft.Padding.symmetric(horizontal=13, vertical=10),
            border=ft.Border.only(bottom=ft.BorderSide(1, self.background)),
        )

    def _settings_view(self, _view: SyncView) -> ft.Control:
        sync_dir = self._text_field(
            self.translate("sync_folder"),
            str(self.settings.normalized_sync_dir()),
            read_only=True,
            prefix_icon=ft.Icons.FOLDER_OUTLINED,
        )
        public_url = self._text_field(
            self.translate("public_server_url"),
            self.settings.normalized_public_url(),
            prefix_icon=ft.Icons.LINK_ROUNDED,
        )
        interval = self._text_field(
            self.translate("sync_interval"),
            str(self.settings.sync_interval),
            suffix=self.translate("seconds"),
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        language = ft.SegmentedButton(
            segments=[
                ft.Segment(value="en", label=self.translate("language_en")),
                ft.Segment(value="ru", label=self.translate("language_ru")),
            ],
            selected=[self.translator.language],
            show_selected_icon=True,
        )
        error = ft.Text("", color=self.danger, size=12, visible=False)

        async def choose(_event=None) -> None:
            chosen = await self.file_picker.get_directory_path(
                dialog_title=self.translate("select_sync_folder"),
                initial_directory=sync_dir.value,
            )
            if chosen:
                sync_dir.value = chosen
                self.page.update()

        def save(_event=None) -> None:
            try:
                interval_value = int(interval.value)
                if not 3 <= interval_value <= 3600:
                    raise ValueError
            except ValueError:
                error.value = self.translate("interval_invalid")
                error.visible = True
                self.page.update()
                return
            try:
                folder = Path(sync_dir.value).expanduser()
                folder.mkdir(parents=True, exist_ok=True)
                public_value = normalize_api_url(public_url.value)
            except (OSError, ValueError):
                error.value = self.translate("folder_invalid")
                error.visible = True
                self.page.update()
                return
            selected = language.selected
            self.translator.set_language(selected[0] if selected else "en")
            self.settings.language = self.translator.language
            self.settings.sync_dir = str(folder.resolve())
            self.settings.sync_interval = interval_value
            self.settings.public_url = public_value
            self.settings_store.save(self.settings)
            self._start_engine()
            self.monitor_serial += 1
            serial = self.monitor_serial
            self.page.run_task(self._monitor, serial)
            self._snack(self.translate("settings_saved"))
            self._render_dashboard()

        account_card = self._card(
            ft.Column(
                controls=[
                    ft.Text(self.translate("account"), color=self.muted, size=12),
                    ft.Row(
                        controls=[
                            ft.Container(
                                content=ft.Icon(ft.Icons.PERSON_ROUNDED, color=self.text),
                                width=48,
                                height=48,
                                bgcolor=self.primary,
                                border_radius=15,
                                alignment=ft.Alignment.CENTER,
                            ),
                            ft.Column(
                                controls=[
                                    ft.Text(
                                        self.settings.username,
                                        size=17,
                                        weight=ft.FontWeight.W_600,
                                    ),
                                    ft.Text(self.settings.api_url, color=self.muted, size=11),
                                ],
                                spacing=2,
                            ),
                        ]
                    ),
                ]
            )
        )
        form = self._card(
            ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            sync_dir,
                            self._outlined_button(
                                self.translate("choose_folder"),
                                ft.Icons.FOLDER_OPEN_OUTLINED,
                                choose,
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.END,
                    ),
                    public_url,
                    interval,
                    ft.Column(
                        controls=[
                            ft.Text(self.translate("language"), color=self.muted, size=12),
                            language,
                        ],
                        spacing=6,
                    ),
                    error,
                    self._primary_button(
                        self.translate("save_changes"),
                        ft.Icons.SAVE_OUTLINED,
                        save,
                    ),
                ],
                spacing=16,
            ),
            padding=22,
        )
        return ft.Column(
            controls=[
                ft.Column(
                    controls=[
                        ft.Text(
                            self.translate("settings_title"), size=29, weight=ft.FontWeight.W_700
                        ),
                        ft.Text(self.translate("settings_subtitle"), color=self.muted, size=13),
                    ],
                    spacing=2,
                ),
                account_card,
                form,
            ],
            spacing=18,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
        )

    def _open_path(self, path: Path) -> None:
        target = path if path.is_dir() else path.parent
        try:
            if sys.platform == "win32":
                os.startfile(str(target))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as error:
            self._snack(self.translate("operation_failed", error=str(error)), error=True)

    def _snack(self, message: str, error: bool = False) -> None:
        self.page.show_dialog(
            ft.SnackBar(
                content=ft.Text(message, color=self.text),
                bgcolor=self.danger_bg if error else self.surface_soft,
            )
        )


async def main(page: ft.Page) -> None:
    client = DesktopClient(page)
    page.data = client
    await client.start()


def run() -> None:
    configure_desktop_backend()
    ft.run(main)


if __name__ == "__main__":
    run()
