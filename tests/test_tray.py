from types import SimpleNamespace

from lyrarma_cloud_client.ui import tray as tray_module
from lyrarma_cloud_client.ui.tray import SystemTray


class FakeImage:
    def convert(self, mode: str):
        assert mode == "RGBA"
        return self


class FakeImageModule:
    @staticmethod
    def open(_stream) -> FakeImage:
        return FakeImage()


class FakeMenuItem:
    def __init__(self, text, action, default=False) -> None:
        self.text = text
        self.action = action
        self.default = default


class FakeMenu(tuple):
    SEPARATOR = object()

    def __new__(cls, *items):
        return super().__new__(cls, items)


class FakeIcon:
    HAS_MENU = True
    instances = []

    def __init__(self, name, image, title, menu) -> None:
        self.name = name
        self.image = image
        self.title = title
        self.menu = menu
        self.detached = False
        self.stopped = False
        self.__class__.instances.append(self)

    def run_detached(self) -> None:
        self.detached = True

    def stop(self) -> None:
        self.stopped = True


def fake_backend(has_menu: bool = True):
    icon_type = type("ConfiguredFakeIcon", (FakeIcon,), {"HAS_MENU": has_menu, "instances": []})
    backend = SimpleNamespace(Icon=icon_type, Menu=FakeMenu, MenuItem=FakeMenuItem)
    return backend, FakeImageModule


def test_system_tray_exposes_show_and_exit_actions(monkeypatch) -> None:
    actions = []
    backend = fake_backend()
    monkeypatch.setattr(tray_module, "_load_backend", lambda: backend)
    tray = SystemTray(
        title="Lyrarma Cloud",
        show_label="Open",
        exit_label="Exit",
        on_show=lambda: actions.append("show"),
        on_exit=lambda: actions.append("exit"),
    )

    assert tray.start()
    assert tray.is_running
    icon = backend[0].Icon.instances[0]
    assert icon.detached
    assert icon.menu[0].text == "Open"
    assert icon.menu[0].default
    assert icon.menu[2].text == "Exit"

    icon.menu[0].action()
    icon.menu[2].action()
    assert actions == ["show", "exit"]

    tray.update_labels(title="Облако", show_label="Открыть", exit_label="Выйти")
    assert icon.title == "Облако"
    assert icon.menu[0].text == "Открыть"
    assert icon.menu[2].text == "Выйти"

    tray.stop()
    assert icon.stopped
    assert not tray.is_running


def test_xorg_fallback_uses_exit_as_the_default_action(monkeypatch) -> None:
    actions = []
    backend = fake_backend(has_menu=False)
    monkeypatch.setattr(tray_module, "_load_backend", lambda: backend)
    tray = SystemTray(
        title="Lyrarma Cloud",
        show_label="Open",
        exit_label="Exit",
        on_show=lambda: actions.append("show"),
        on_exit=lambda: actions.append("exit"),
    )

    assert tray.start()
    icon = backend[0].Icon.instances[0]
    assert len(icon.menu) == 1
    assert icon.menu[0].text == "Exit"
    assert icon.menu[0].default

    icon.menu[0].action()
    assert actions == ["exit"]
