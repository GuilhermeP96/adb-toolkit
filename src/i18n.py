"""
i18n.py - Internationalization support for ADB Toolkit.

Features:
  - Auto-detects the operating system language
  - Falls back to English if no matching locale is found
  - Loads translations from JSON files in the locales/ directory
  - Simple t("key") function with format kwargs support
  - Easy to contribute new languages (just add a JSON file)

Usage:
    from .i18n import t, set_language, get_language, available_languages

    label = t("tabs.devices")              # simple lookup
    msg = t("backup.progress", pct=42)     # with formatting: "Backup {pct}%"
"""

import json
import locale
import logging
import os
import platform
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("adb_toolkit.i18n")

# ---------------------------------------------------------------------------
# Locale directory — sits alongside src/ at the project root
# ---------------------------------------------------------------------------
_LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
_current_lang: str = "en"
_strings: Dict[str, str] = {}
_fallback_strings: Dict[str, str] = {}  # always English
_available: Dict[str, str] = {}  # code -> display name
_callbacks: list = []  # called on language change


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def t(key: str, **kwargs: Any) -> str:
    """Translate *key* using the current language.

    If *kwargs* are supplied the translated string is formatted with
    ``str.format_map(kwargs)``.  Missing keys fall back to English,
    then to the raw key itself.
    """
    text = _strings.get(key) or _fallback_strings.get(key) or key
    if kwargs:
        try:
            text = text.format_map(kwargs)
        except (KeyError, ValueError):
            pass
    return text


def set_language(code: str) -> None:
    """Switch to *code* (e.g. ``"pt_BR"``, ``"en"``).

    Reloads the string table and fires all registered callbacks.
    """
    global _current_lang, _strings
    code = _normalise_code(code)
    _strings = _load_locale_file(code)
    if not _strings:
        # Try base language (e.g. "pt" from "pt_BR")
        base = code.split("_")[0]
        _strings = _load_locale_file(base)
    if not _strings:
        _strings = _fallback_strings.copy()
        code = "en"
    _current_lang = code
    log.info("Language set to: %s (%d strings loaded)", code, len(_strings))
    for cb in _callbacks:
        try:
            cb(code)
        except Exception as exc:
            log.debug("Language change callback error: %s", exc)


def get_language() -> str:
    """Return the current language code."""
    return _current_lang


def available_languages() -> Dict[str, str]:
    """Return ``{code: display_name}`` for every locale file found."""
    if not _available:
        _scan_available()
    return dict(_available)


def on_language_change(callback) -> None:
    """Register a callable to be invoked when the language changes."""
    _callbacks.append(callback)


def detect_os_language() -> str:
    """Detect the operating system UI language and return a locale code.

    Returns codes like ``"pt_BR"``, ``"en_US"``, ``"es"``, etc.
    Falls back to ``"en"`` when detection fails.
    """
    code = None

    # Windows: use the GetUserDefaultUILanguage API for the most reliable result
    if platform.system() == "Windows":
        try:
            import ctypes
            windll = ctypes.windll.kernel32
            lang_id = windll.GetUserDefaultUILanguage()
            # Convert Win32 LANGID to locale name
            buf = ctypes.create_unicode_buffer(85)
            windll.LCIDToLocaleName(lang_id, buf, 85, 0)
            win_locale = buf.value  # e.g. "pt-BR", "en-US"
            if win_locale:
                code = win_locale.replace("-", "_")
        except Exception:
            pass

    # Fallback: LANG / LC_ALL environment variables (Linux/macOS/WSL)
    if not code:
        for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
            val = os.environ.get(var, "")
            if val and val not in ("C", "POSIX"):
                code = val.split(".")[0]  # strip encoding (e.g. "pt_BR.UTF-8")
                break

    # Fallback: Python locale
    if not code:
        try:
            loc = locale.getdefaultlocale()[0]  # e.g. "pt_BR"
            if loc:
                code = loc
        except Exception:
            pass

    if not code:
        code = "en"

    return _normalise_code(code)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalise_code(code: str) -> str:
    """Normalise a locale code: ``pt-BR`` → ``pt_BR``."""
    return code.replace("-", "_")


def _load_locale_file(code: str) -> Dict[str, str]:
    """Load a flat ``{key: string}`` dict from ``locales/<code>.json``."""
    path = _LOCALES_DIR / f"{code}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Support nested JSON → flatten with dot notation
        if isinstance(data, dict):
            return _flatten(data)
        return {}
    except Exception as exc:
        log.warning("Failed to load locale '%s': %s", code, exc)
        return {}


def _flatten(d: dict, prefix: str = "") -> Dict[str, str]:
    """Flatten nested dicts with dot-separated keys."""
    items: Dict[str, str] = {}
    for k, v in d.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            items.update(_flatten(v, full_key))
        else:
            items[full_key] = str(v)
    return items


def _scan_available() -> None:
    """Populate _available from locale files on disk."""
    global _available
    _available = {}
    if not _LOCALES_DIR.exists():
        return
    for f in sorted(_LOCALES_DIR.glob("*.json")):
        code = f.stem
        # Try to read a "language_name" key from the file for display
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            name = data.get("_language_name", code)
        except Exception:
            name = code
        _available[code] = name


# ---------------------------------------------------------------------------
# Module init — load fallback (English) and auto-detect OS language
# ---------------------------------------------------------------------------

def _init() -> None:
    global _fallback_strings, _current_lang, _strings

    # Always load English as the fallback
    _fallback_strings = _load_locale_file("en")

    # Detect OS language
    os_lang = detect_os_language()
    log.info("Detected OS language: %s", os_lang)

    # Load the matching locale (or fall back)
    _strings = _load_locale_file(os_lang)
    if _strings:
        _current_lang = os_lang
    else:
        base = os_lang.split("_")[0]
        _strings = _load_locale_file(base)
        if _strings:
            _current_lang = base
        else:
            _strings = _fallback_strings.copy()
            _current_lang = "en"

    log.info("i18n initialized: lang=%s, strings=%d", _current_lang, len(_strings))


_init()
