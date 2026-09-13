import json
from importlib.resources import files

from lyrarma_cloud_client.config import Translator


def test_translation_catalogs_have_identical_keys() -> None:
    resource = files("lyrarma_cloud_client").joinpath("assets/translations.json")
    catalog = json.loads(resource.read_text(encoding="utf-8"))

    assert set(catalog) == {"en", "ru"}
    assert set(catalog["en"]) == set(catalog["ru"])
    assert all(catalog[language][key] for language in catalog for key in catalog[language])


def test_translator_formats_and_falls_back() -> None:
    translator = Translator("ru_RU")

    assert translator.language == "ru"
    assert translator.translate("minutes_ago", count=4) == "4 мин назад"
    assert translator.translate("missing_key") == "missing_key"


def test_all_literal_ui_translation_keys_exist() -> None:
    import ast
    from pathlib import Path

    source = Path("src/lyrarma_cloud_client/ui/app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    used_keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "translate" or not node.args:
            continue
        key = node.args[0]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            used_keys.add(key.value)

    translator = Translator("en")
    assert used_keys <= set(translator.catalog["en"])
