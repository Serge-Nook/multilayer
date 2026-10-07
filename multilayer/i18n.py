import json
import os
import sys
import tempfile
from pathlib import Path

LANGUAGES = {"ru": "Русский", "en": "English"}
_language: str | None = None


def settings_path() -> Path:
    if override := os.environ.get("MULTILAYER_CONFIG"):
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home())))
        return base / "Multilayer" / "settings.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "multilayer" / "settings.json"


def load_settings() -> dict:
    try:
        data = json.loads(settings_path().read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def language() -> str:
    global _language
    if _language is None:
        for candidate in (os.environ.get("MULTILAYER_LANG"), load_settings().get("language")):
            if candidate in LANGUAGES:
                _language = candidate
                break
        else:
            _language = "ru"
    return _language


def set_language(code: str, persist: bool = True) -> None:
    global _language
    if code not in LANGUAGES:
        raise ValueError(code)
    if not persist:
        _language = code
        return
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = {**load_settings(), "language": code}
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".settings-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(settings, out, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
        _language = code
    finally:
        Path(temporary).unlink(missing_ok=True)


def tr(text: str, **values: object) -> str:
    if language() == "en":
        from multilayer.locale_en import EN

        text = EN.get(text, text)
    return text.format(**values) if values else text
