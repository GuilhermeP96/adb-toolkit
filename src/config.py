"""
config.py - Application configuration and settings.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("adb_toolkit.config")

DEFAULT_CONFIG = {
    "app": {
        "name": "ADB Toolkit",
        "version": "1.3.0",
        "language": "pt-BR",
        "theme": "dark",
        "check_updates": True,
    },
    "adb": {
        "auto_download": True,
        "server_port": 5037,
        "timeout_seconds": 120,
        "device_poll_interval": 2.0,
    },
    "drivers": {
        "auto_install": True,
        "prefer_google_driver": True,
    },
    "backup": {
        "default_dir": "backups",
        "compress_backups": True,
        "include_apks": True,
        "include_shared": True,
        "include_system": False,
        "max_concurrent_pulls": 4,
        "default_categories": [
            "apps", "photos", "videos", "music", "documents", "contacts", "sms"
        ],
    },
    "transfer": {
        "temp_dir": "transfers",
        "cleanup_after": True,
        "verify_after_transfer": True,
        "ignore_cache": True,
        "ignore_thumbnails": True,
    },
    "ui": {
        "window_width": 1100,
        "window_height": 750,
        "show_advanced": False,
        "confirm_destructive": True,
    },
    "acceleration": {
        "gpu_enabled": True,
        "multi_gpu": True,
        "verify_checksums": True,
        "checksum_algo": "md5",
        "max_pull_workers": 0,
        "max_push_workers": 0,
        "auto_threads": True,
    },
    "virtualization": {
        "enabled": True,
        "prefer_hyperv": False,
    },
}


class Config:
    """Application configuration manager."""

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or (
            Path(__file__).resolve().parent.parent / "config.json"
        )
        self._data: Dict[str, Any] = {}
        self.load()

    def load(self):
        """Load config from file, creating defaults if needed."""
        if self.config_path.exists():
            try:
                self._data = json.loads(
                    self.config_path.read_text(encoding="utf-8")
                )
                # Merge with defaults for any missing keys
                self._data = self._deep_merge(DEFAULT_CONFIG, self._data)
                log.info("Config loaded from %s", self.config_path)
            except Exception as exc:
                log.warning("Failed to load config: %s. Using defaults.", exc)
                self._data = DEFAULT_CONFIG.copy()
        else:
            self._data = DEFAULT_CONFIG.copy()
            self.save()

    def save(self):
        """Save current config to file."""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("Failed to save config: %s", exc)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value using dot notation (e.g., 'backup.default_dir')."""
        keys = key.split(".")
        value = self._data
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    def set(self, key: str, value: Any):
        """Set a config value using dot notation."""
        keys = key.split(".")
        d = self._data
        for k in keys[:-1]:
            if k not in d or not isinstance(d[k], dict):
                d[k] = {}
            d = d[k]
        d[keys[-1]] = value
        self.save()

    def _deep_merge(self, base: dict, override: dict) -> dict:
        """Deep merge override into base."""
        result = base.copy()
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = self._deep_merge(result[k], v)
            else:
                result[k] = v
        return result
