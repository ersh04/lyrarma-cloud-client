"""Theme resource loading for the desktop user interface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files


@dataclass(frozen=True, slots=True)
class ThemePalette:
    """Colors loaded from the packaged JSON theme catalog."""

    background: str
    surface: str
    surface_soft: str
    border: str
    text: str
    muted: str
    primary: str
    primary_hover: str
    danger: str
    danger_bg: str
    success: str
    success_bg: str
    warning: str
    warning_bg: str


def load_theme() -> ThemePalette:
    """Load and validate the packaged desktop theme."""
    resource = files("lyrarma_cloud_client").joinpath("assets/theme.json")
    values = json.loads(resource.read_text(encoding="utf-8"))
    return ThemePalette(
        **{field: values[field.upper()] for field in ThemePalette.__dataclass_fields__}
    )


DEFAULT_THEME = load_theme()
