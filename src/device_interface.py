"""
device_interface.py - Abstract base class for device communication.

Defines a platform-agnostic interface so that TransferManager, BackupManager,
and RestoreManager can work with **Android (ADB)** and **iOS (pymobiledevice3)**
devices interchangeably.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class DevicePlatform(Enum):
    ANDROID = "android"
    IOS = "ios"
    UNKNOWN = "unknown"


class DeviceState(Enum):
    CONNECTED = "device"
    UNAUTHORIZED = "unauthorized"
    OFFLINE = "offline"
    RECOVERY = "recovery"
    LOCKED = "locked"
    DFU = "dfu"  # iOS specific


# ---------------------------------------------------------------------------
# Unified Device Info
# ---------------------------------------------------------------------------
@dataclass
class UnifiedDeviceInfo:
    """Platform-agnostic device information."""
    serial: str = ""
    platform: DevicePlatform = DevicePlatform.UNKNOWN
    state: DeviceState = DeviceState.CONNECTED
    model: str = ""
    manufacturer: str = ""
    os_version: str = ""        # e.g. "14" (Android) or "17.5" (iOS)
    product: str = ""
    storage_total: int = 0      # bytes
    storage_free: int = 0       # bytes
    battery_level: int = -1

    # iOS-specific
    udid: str = ""              # 40-char UDID or 24-char UUID
    device_class: str = ""      # iPhone, iPad, iPod
    ios_build: str = ""         # e.g. "21F79"

    # Android-specific
    sdk_version: str = ""
    android_codename: str = ""

    def friendly_name(self) -> str:
        if self.manufacturer and self.model:
            return f"{self.manufacturer} {self.model}"
        if self.model:
            return self.model
        return self.serial

    def platform_icon(self) -> str:
        """Return emoji for platform."""
        if self.platform == DevicePlatform.ANDROID:
            return "🤖"
        elif self.platform == DevicePlatform.IOS:
            return "🍎"
        return "❓"

    def platform_label(self) -> str:
        if self.platform == DevicePlatform.ANDROID:
            return f"Android {self.os_version}"
        elif self.platform == DevicePlatform.IOS:
            return f"iOS {self.os_version}"
        return "Desconhecido"

    def storage_summary(self) -> str:
        if self.storage_total <= 0:
            return ""
        return f"{_fmt(self.storage_free)} livre / {_fmt(self.storage_total)} total"

    def short_label(self) -> str:
        """Label for dropdown menus: icon + name + storage."""
        icon = self.platform_icon()
        name = self.friendly_name()
        stor = self.storage_summary()
        if stor:
            return f"{icon} {name}  [{stor}]"
        return f"{icon} {name}"


def _fmt(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f} PB"


# ---------------------------------------------------------------------------
# Contact / Calendar common formats
# ---------------------------------------------------------------------------
@dataclass
class ContactEntry:
    """A single contact in a platform-agnostic form."""
    display_name: str = ""
    phones: List[str] = field(default_factory=list)
    emails: List[str] = field(default_factory=list)
    organization: str = ""
    note: str = ""
    photo_uri: str = ""
    raw_vcard: str = ""  # Full vCard 3.0/4.0 text (if available)


@dataclass
class SMSEntry:
    """A single SMS message."""
    address: str = ""       # phone number
    body: str = ""
    date_ms: int = 0        # Unix timestamp in milliseconds
    msg_type: int = 1       # 1=inbox, 2=sent
    read: bool = True
    thread_id: int = 0


@dataclass
class CalendarEvent:
    """A single calendar event."""
    uid: str = ""
    summary: str = ""
    description: str = ""
    dtstart: str = ""       # ISO 8601
    dtend: str = ""
    location: str = ""
    raw_ics: str = ""       # Full iCalendar text


# ---------------------------------------------------------------------------
# Abstract Device Interface
# ---------------------------------------------------------------------------
class DeviceInterface(ABC):
    """Abstract interface for interacting with a mobile device.

    Concrete implementations:
      - ADBCore (Android)    → wraps adb binary
      - iOSCore (iOS)       → wraps pymobiledevice3
    """

    @abstractmethod
    def platform(self) -> DevicePlatform:
        """Return the platform this interface handles."""
        ...

    # ---- Discovery & connection ----------------------------------------

    @abstractmethod
    def list_devices(self) -> List[UnifiedDeviceInfo]:
        """Return all currently connected devices for this platform."""
        ...

    @abstractmethod
    def get_device_details(self, serial: str) -> UnifiedDeviceInfo:
        """Populate full details for a specific device."""
        ...

    # ---- File operations -----------------------------------------------

    @abstractmethod
    def pull(self, remote: str, local: str, serial: str) -> bool:
        """Copy a file from the device to local filesystem."""
        ...

    @abstractmethod
    def push(self, local: str, remote: str, serial: str) -> bool:
        """Copy a file from local filesystem to the device."""
        ...

    @abstractmethod
    def list_dir(self, remote_path: str, serial: str) -> List[str]:
        """List entries in a remote directory."""
        ...

    @abstractmethod
    def file_exists(self, remote_path: str, serial: str) -> bool:
        """Check whether a remote path exists."""
        ...

    @abstractmethod
    def mkdir(self, remote_path: str, serial: str) -> bool:
        """Create a remote directory (including parents)."""
        ...

    @abstractmethod
    def delete(self, remote_path: str, serial: str) -> bool:
        """Delete a remote file or directory."""
        ...

    @abstractmethod
    def stat_file(self, remote_path: str, serial: str) -> Tuple[int, float]:
        """Return (size_bytes, mtime_epoch) for a remote file."""
        ...

    # ---- Contacts ------------------------------------------------------

    @abstractmethod
    def export_contacts(self, serial: str, out_dir: Path) -> Optional[Path]:
        """Export contacts from device to a local VCF file.

        Returns the path to the exported VCF, or None on failure.
        """
        ...

    @abstractmethod
    def import_contacts(self, serial: str, vcf_path: Path) -> bool:
        """Import contacts from a VCF file into the device."""
        ...

    # ---- SMS / Messages ------------------------------------------------

    @abstractmethod
    def export_sms(self, serial: str, out_dir: Path) -> Optional[Path]:
        """Export SMS messages to a JSON file.

        Returns the path to the exported JSON, or None on failure.
        """
        ...

    @abstractmethod
    def import_sms(self, serial: str, json_path: Path) -> bool:
        """Import SMS messages from a JSON file into the device."""
        ...

    # ---- Photos / Videos / Media ---------------------------------------

    @abstractmethod
    def get_media_paths(self, serial: str) -> Dict[str, List[str]]:
        """Return a dict of media category → list of remote paths.

        Expected keys: 'photos', 'videos', 'music', 'documents'.
        """
        ...

    # ---- Storage info --------------------------------------------------

    @abstractmethod
    def get_free_bytes(self, serial: str) -> int:
        """Return free storage in bytes, or -1 if unknown."""
        ...

    @abstractmethod
    def get_total_bytes(self, serial: str) -> int:
        """Return total storage in bytes, or -1 if unknown."""
        ...

    # ---- Shell / raw commands (optional) --------------------------------

    def run_shell(self, cmd: str, serial: str, timeout: int = 60) -> str:
        """Run a shell command on device. Default: not supported."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support shell commands"
        )


# ---------------------------------------------------------------------------
# Device Manager – unified discovery for all platforms
# ---------------------------------------------------------------------------
class DeviceManager:
    """Aggregates multiple DeviceInterface implementations and provides a
    single view of all connected devices across platforms."""

    def __init__(self):
        self._interfaces: List[DeviceInterface] = []
        self._cache: Dict[str, Tuple[DeviceInterface, UnifiedDeviceInfo]] = {}

    def register(self, interface: DeviceInterface):
        """Register a platform interface (ADBCore adapter, iOSCore, …)."""
        self._interfaces.append(interface)

    def list_all_devices(self) -> List[UnifiedDeviceInfo]:
        """Return all connected devices from every registered platform."""
        all_devices: List[UnifiedDeviceInfo] = []
        self._cache.clear()
        for iface in self._interfaces:
            try:
                devs = iface.list_devices()
                for d in devs:
                    self._cache[d.serial] = (iface, d)
                    all_devices.append(d)
            except Exception:
                pass
        return all_devices

    def get_interface(self, serial: str) -> Optional[DeviceInterface]:
        """Return the DeviceInterface that owns *serial*."""
        if serial in self._cache:
            return self._cache[serial][0]
        # Re-scan
        self.list_all_devices()
        if serial in self._cache:
            return self._cache[serial][0]
        return None

    def get_device_info(self, serial: str) -> Optional[UnifiedDeviceInfo]:
        if serial in self._cache:
            return self._cache[serial][1]
        return None

    def is_cross_platform(self, serial_a: str, serial_b: str) -> bool:
        """Return True if the two devices are on different platforms."""
        a = self.get_device_info(serial_a)
        b = self.get_device_info(serial_b)
        if a and b:
            return a.platform != b.platform
        return False
