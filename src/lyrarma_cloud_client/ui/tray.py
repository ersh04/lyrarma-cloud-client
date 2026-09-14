"""Cross-platform system tray integration for the desktop client."""

from __future__ import annotations

import importlib
import logging
import os
import sys
import threading
from collections.abc import Callable
from importlib.resources import files
from io import BytesIO
from typing import Any

LOGGER = logging.getLogger(__name__)


def _prepare_linux_backend() -> None:
    """Avoid a broken PyGObject shim while retaining AppIndicator when available."""
    if not sys.platform.startswith("linux") or os.getenv("PYSTRAY_BACKEND"):
        return
    try:
        gi = importlib.import_module("gi")
    except ImportError:
        return
    if not callable(getattr(gi, "require_version", None)):
        os.environ["PYSTRAY_BACKEND"] = "xorg"


def _load_backend() -> tuple[Any, Any]:
    """Load optional desktop modules only when the tray is started."""
    _prepare_linux_backend()
    import pystray
    from PIL import Image

    return pystray, Image


class SystemTray:
    """Own a native tray icon and forward its actions to the Flet controller."""

    def __init__(
        self,
        *,
        title: str,
        show_label: str,
        exit_label: str,
        on_show: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> None:
        self.title = title
        self.show_label = show_label
        self.exit_label = exit_label
        self.on_show = on_show
        self.on_exit = on_exit
        self._backend: Any | None = None
        self._icon: Any | None = None
        self._lock = threading.RLock()

    @property
    def is_running(self) -> bool:
        """Return whether the native tray backend started successfully."""
        with self._lock:
            return self._icon is not None

    def _show(self, _icon=None, _item=None) -> None:
        self.on_show()

    def _exit(self, _icon=None, _item=None) -> None:
        self.on_exit()

    def _create_menu(self, backend: Any, supports_menu: bool) -> Any:
        if supports_menu:
            return backend.Menu(
                backend.MenuItem(self.show_label, self._show, default=True),
                backend.Menu.SEPARATOR,
                backend.MenuItem(self.exit_label, self._exit),
            )
        return backend.Menu(backend.MenuItem(self.exit_label, self._exit, default=True))

    def start(self) -> bool:
        """Start the tray in detached mode and return whether it is available."""
        with self._lock:
            if self._icon is not None:
                return True
            try:
                backend, image_module = _load_backend()
                image_bytes = files("lyrarma_cloud_client").joinpath("assets/icon.png").read_bytes()
                image = image_module.open(BytesIO(image_bytes)).convert("RGBA")
                supports_menu = bool(backend.Icon.HAS_MENU)
                menu = self._create_menu(backend, supports_menu)
                icon = backend.Icon(
                    "lyrarma-cloud-client",
                    image,
                    self.title,
                    menu,
                )
                icon.run_detached()
            except Exception as error:
                LOGGER.warning("System tray is unavailable: %s", error)
                return False
            self._backend = backend
            self._icon = icon
            return True

    def update_labels(self, *, title: str, show_label: str, exit_label: str) -> None:
        """Refresh localized tray labels without restarting the native icon."""
        with self._lock:
            self.title = title
            self.show_label = show_label
            self.exit_label = exit_label
            if self._icon is None or self._backend is None:
                return
            self._icon.title = title
            self._icon.menu = self._create_menu(
                self._backend,
                bool(self._backend.Icon.HAS_MENU),
            )

    def stop(self) -> None:
        """Remove the tray icon and stop its native event loop."""
        with self._lock:
            icon = self._icon
            self._icon = None
            self._backend = None
        if icon is not None:
            try:
                icon.stop()
            except Exception as error:
                LOGGER.warning("Could not stop the system tray: %s", error)
