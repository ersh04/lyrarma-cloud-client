"""JSON-backed localization for the desktop interface."""

from __future__ import annotations

import json
import locale
from importlib.resources import files

SUPPORTED_LANGUAGES = ("en", "ru")


class Translator:
    """Resolve translated strings from the packaged English/Russian catalog."""

    def __init__(self, language: str | None = None) -> None:
        resource = files("lyrarma_cloud_client").joinpath("assets/translations.json")
        self._catalog = json.loads(resource.read_text(encoding="utf-8"))
        self.language = self.normalize(language or self.detect_language())

    @staticmethod
    def normalize(language: str) -> str:
        normalized = language.lower().replace("-", "_").split("_", 1)[0]
        return normalized if normalized in SUPPORTED_LANGUAGES else "en"

    @staticmethod
    def detect_language() -> str:
        current = locale.getlocale()[0] or "en"
        return current

    def set_language(self, language: str) -> None:
        self.language = self.normalize(language)

    def translate(self, key: str, **values: object) -> str:
        language_values = self._catalog.get(self.language, {})
        fallback_values = self._catalog.get("en", {})
        template = language_values.get(key, fallback_values.get(key, key))
        try:
            return template.format(**values)
        except (KeyError, ValueError):
            return template

    @property
    def catalog(self) -> dict[str, dict[str, str]]:
        return self._catalog
