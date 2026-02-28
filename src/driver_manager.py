"""
driver_manager.py - Automatic ADB/USB driver detection and installation for Windows.

Features:
  - Detects if connected Android device has proper USB drivers
  - Downloads and installs Google USB Driver automatically
  - Falls back to universal ADB driver (Koush/UniversalAdbDriver)
  - Handles Windows Device Manager interaction via devcon/pnputil
"""

import ctypes
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger("adb_toolkit.driver_manager")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GOOGLE_USB_DRIVER_URL = (
    "https://dl.google.com/android/repository/usb_driver_r13-windows.zip"
)
# The zip extracts to a folder named 'usb_driver' (not 'google_usb_driver')
GOOGLE_USB_DRIVER_EXTRACT_NAME = "usb_driver"

# ---------------------------------------------------------------------------
# Apple / iOS driver constants
# ---------------------------------------------------------------------------
APPLE_USB_VID = "0x05AC"  # Apple Inc.

# iTunes installers — the standard way to get Apple Mobile Device Support
ITUNES_DOWNLOAD_URL = (
    "https://www.apple.com/itunes/download/win64"
)
# Apple Mobile Device driver .inf shipped inside iTunes/Apple Application Support
APPLE_DRIVER_INF_PATHS = [
    r"C:\Program Files\Common Files\Apple\Mobile Device Support\Drivers",
    r"C:\Program Files (x86)\Common Files\Apple\Mobile Device Support\Drivers",
    r"C:\Program Files\Common Files\Apple\Apple Application Support",
]
# Service names related to Apple Mobile Device / iOS connectivity
APPLE_SERVICE_NAMES = [
    "Apple Mobile Device Service",
    "AppleMobileDeviceService",
    "AppleMobileDevice",
]

ANDROID_USB_VID_LIST = [
    "0x18D1",  # Google
    "0x04E8",  # Samsung
    "0x2717",  # Xiaomi
    "0x22B8",  # Motorola
    "0x0BB4",  # HTC
    "0x12D1",  # Huawei
    "0x2A70",  # OnePlus
    "0x1004",  # LG
    "0x0FCE",  # Sony
    "0x2916",  # Yota
    "0x1949",  # Lab126 (Amazon)
    "0x0E8D",  # MediaTek
    "0x2C7C",  # Quectel
    "0x19D2",  # ZTE
    "0x2A45",  # Meizu
    "0x0502",  # Acer
    "0x0B05",  # Asus
    "0x413C",  # Dell
    "0x0489",  # Foxconn
    "0x091E",  # Garmin-Asus
    "0x2080",  # Nook
    "0x05C6",  # Qualcomm
]

# ---------------------------------------------------------------------------
# Chipset-specific VID/PID definitions for WinUSB .inf generation
# ---------------------------------------------------------------------------
CHIPSET_DRIVERS = {
    "samsung": {
        "name": "Samsung",
        "description": "Samsung Galaxy / Exynos / ODIN mode",
        "vid_pids": [
            ("04E8", "6860"),   # Samsung Galaxy (MTP)
            ("04E8", "6861"),   # Samsung Galaxy (PTP)
            ("04E8", "6862"),   # Samsung ADB Interface
            ("04E8", "6863"),   # Samsung ADB + MTP
            ("04E8", "6864"),   # Samsung ADB + PTP
            ("04E8", "6866"),   # Samsung RNDIS
            ("04E8", "685D"),   # Samsung ADB Composite
            ("04E8", "685E"),   # Samsung ADB Modem
            ("04E8", "6601"),   # Samsung ODIN/Download mode
            ("04E8", "681C"),   # Samsung ADB (legacy S3/S4)
            ("04E8", "681D"),   # Samsung MTP (legacy)
            ("04E8", "6843"),   # Samsung Kies
            ("04E8", "684E"),   # Samsung USB Serial
            ("04E8", "6890"),   # Samsung CDC
        ],
    },
    "qualcomm": {
        "name": "Qualcomm (Snapdragon)",
        "description": "Qualcomm HS-USB / QDLoader 9008 / Diagnostic",
        "vid_pids": [
            ("05C6", "9008"),   # Qualcomm HS-USB QDLoader 9008
            ("05C6", "9025"),   # Qualcomm HS-USB Diagnostics
            ("05C6", "900E"),   # Qualcomm HS-USB NMEA
            ("05C6", "9091"),   # Qualcomm USB Composite Device
            ("05C6", "9092"),   # Qualcomm HS-USB Android DIAG
            ("05C6", "F003"),   # Qualcomm CDMA Technologies
            ("05C6", "9001"),   # Qualcomm HS-USB Android (generic)
            ("05C6", "9014"),   # Qualcomm HS-USB WWAN adapter
            ("05C6", "9018"),   # Qualcomm Sahara
            ("05C6", "901D"),   # Qualcomm HS-USB DIAG + ADB
            ("05C6", "9024"),   # Qualcomm HS-USB MultiPort
            ("05C6", "9026"),   # Qualcomm HS-USB ADB
            ("05C6", "9031"),   # Qualcomm HS-USB MDM Diagnostics
            ("05C6", "9039"),   # Qualcomm HS-USB Android DIAG 9039
            ("05C6", "9048"),   # Qualcomm HS-USB Modem
            ("05C6", "9057"),   # Qualcomm HS-USB QMI WWAN
            ("05C6", "9062"),   # Qualcomm EDL / Firehose
            ("05C6", "9079"),   # Qualcomm HS-USB SER4
            ("05C6", "9091"),   # Qualcomm Composite ADB
        ],
    },
    "mediatek": {
        "name": "MediaTek",
        "description": "MediaTek Preloader / VCOM / DA USB",
        "vid_pids": [
            ("0E8D", "0003"),   # MediaTek Preloader USB VCOM
            ("0E8D", "2000"),   # MediaTek PreLoader (SP Flash Tool)
            ("0E8D", "2001"),   # MediaTek DA USB VCOM
            ("0E8D", "2006"),   # MediaTek MT65xx Android Phone
            ("0E8D", "0023"),   # MediaTek VCOM v2
            ("0E8D", "0050"),   # MediaTek BROM (BootROM)
            ("0E8D", "3001"),   # MediaTek Composite Preloader
            ("0E8D", "201C"),   # MediaTek ADB Interface (generic)
            ("0E8D", "200B"),   # MediaTek Flash VCOM
            ("0E8D", "200C"),   # MediaTek Engineering DA
            ("0E8D", "20FF"),   # MediaTek CDC Serial
            ("0E8D", "2008"),   # MediaTek MT65xx
            ("0E8D", "6765"),   # MediaTek Helio G35/G37
            ("0E8D", "6768"),   # MediaTek Helio G85/G90
            ("0E8D", "6771"),   # MediaTek Helio P60/P70
            ("0E8D", "6785"),   # MediaTek Dimensity 700
            ("0E8D", "6833"),   # MediaTek Dimensity 1200
            ("0E8D", "6853"),   # MediaTek Dimensity 800/720
            ("0E8D", "6877"),   # MediaTek Dimensity 900
            ("0E8D", "6893"),   # MediaTek Dimensity 1200+
        ],
    },
    "intel": {
        "name": "Intel",
        "description": "Intel Android USB / Atom SoC / DnX / SoFIA",
        "vid_pids": [
            ("8087", "0A5F"),   # Intel Android ADB
            ("8087", "0A60"),   # Intel ADB Composite
            ("8087", "0A65"),   # Intel DnX Fastboot
            ("8087", "0A5D"),   # Intel DnX (Download and Execute)
            ("8087", "09EF"),   # Intel Android Bootloader Interface
            ("8087", "09F9"),   # Intel Merrifield ADB
            ("8087", "07EF"),   # Intel Tangier
            ("8087", "0716"),   # Intel SoFIA 3GR ADB
            ("8087", "0AA0"),   # Intel UEFI Generic
            ("8087", "0024"),   # Intel USB 3.0 Hub
            ("8086", "09EE"),   # Intel USB OTG Transceiver Driver
            ("8086", "0A65"),   # Intel Android Flash
            ("8086", "0A5F"),   # Intel ADB Interface (alt)
            ("8086", "E005"),   # Intel USB iCDK
        ],
    },
}

# Template for generating WinUSB .inf files for each chipset family
WINUSB_INF_TEMPLATE = r""";
; {driver_name} WinUSB Driver for ADB/USB communication
; Generated by ADB Toolkit
;
[Version]
Signature   = "$Windows NT$"
Class       = USBDevice
ClassGUID   = {{88BAE032-5A81-49f0-BC3D-A4FF138216D6}}
Provider    = %ProviderName%
DriverVer   = 02/24/2026,1.0.0.0
CatalogFile = {cat_name}

[Manufacturer]
%MfgName% = DeviceList,NTamd64,NTx86

[DeviceList.NTamd64]
{device_entries_amd64}

[DeviceList.NTx86]
{device_entries_x86}

[USB_Install]
Include = winusb.inf
Needs   = WINUSB.NT

[USB_Install.Services]
Include    = winusb.inf
AddService = WinUSB,0x00000002,WinUSB_ServiceInstall

[WinUSB_ServiceInstall]
DisplayName   = %SvcDesc%
ServiceType   = 1
StartType     = 3
ErrorControl  = 1
ServiceBinary = %12%\WinUSB.sys

[USB_Install.Wdf]
KmdfService = WINUSB,WinUSB_Install

[WinUSB_Install]
KmdfLibraryVersion = 1.11

[USB_Install.HW]
AddReg = Dev_AddReg

[Dev_AddReg]
HKR,,DeviceInterfaceGUIDs,0x10000,"{{CDB3B5AD-293B-4663-AA36-1AAE46463776}}"

[USB_Install.CoInstallers]
AddReg    = CoInstallers_AddReg
CopyFiles = CoInstallers_CopyFiles

[CoInstallers_AddReg]
HKR,,CoInstallers32,0x00010000,"WdfCoInstaller01011.dll,WdfCoInstaller","WinUSBCoInstaller2.dll"

[CoInstallers_CopyFiles]
WinUSBCoInstaller2.dll
WdfCoInstaller01011.dll

[DestinationDirs]
CoInstallers_CopyFiles = 11

[Strings]
ProviderName = "ADB Toolkit"
MfgName      = "{manufacturer}"
SvcDesc      = "{driver_name} WinUSB Service"
"""


# ---------------------------------------------------------------------------
# Driver Status
# ---------------------------------------------------------------------------
class DriverStatus:
    """Status of ADB drivers on the system."""

    def __init__(self):
        self.is_windows: bool = os.name == "nt"
        self.drivers_installed: bool = False
        self.driver_version: str = ""
        self.android_devices_detected: int = 0
        self.devices_needing_driver: List[Dict[str, str]] = []
        self.error: Optional[str] = None

    def __repr__(self):
        return (
            f"<DriverStatus installed={self.drivers_installed} "
            f"detected={self.android_devices_detected} "
            f"need_driver={len(self.devices_needing_driver)}>"
        )


# ---------------------------------------------------------------------------
# Driver Manager
# ---------------------------------------------------------------------------
class DriverManager:
    """Detects and installs ADB USB drivers on Windows."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent
        self.drivers_dir = self.base_dir / "drivers"
        self.drivers_dir.mkdir(parents=True, exist_ok=True)

    def is_admin(self) -> bool:
        """Check if running with admin privileges."""
        if os.name != "nt":
            return os.geteuid() == 0
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False

    def request_admin(self, script_path: Optional[str] = None):
        """Re-launch the current script with admin privileges (Windows UAC)."""
        if os.name != "nt":
            log.warning("Admin elevation only supported on Windows")
            return
        script = script_path or sys.argv[0]
        params = " ".join(sys.argv[1:])
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{script}" {params}', None, 1
        )

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------
    def check_driver_status(self) -> DriverStatus:
        """Detect whether ADB USB drivers are properly installed."""
        status = DriverStatus()

        if not status.is_windows:
            # Linux/macOS generally don't need separate drivers
            status.drivers_installed = True
            return status

        try:
            # Use pnputil to list drivers
            result = subprocess.run(
                ["pnputil", "/enum-drivers"],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            output = result.stdout.lower()
            adb_keywords = ["android", "adb", "usb\\vid_18d1", "winusb", "google"]
            status.drivers_installed = any(kw in output for kw in adb_keywords)

            # Check for Android devices via PowerShell (Get-CimInstance)
            ps_cmd = (
                "Get-CimInstance Win32_PnPEntity | "
                "Where-Object { $_.Caption -match 'Android|ADB' } | "
                "Select-Object Caption, DeviceID, Status | "
                "ConvertTo-Json -Compress"
            )
            ps_result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if ps_result.stdout.strip():
                import json as _json
                data = ps_result.stdout.strip()
                devices = _json.loads(data)
                if isinstance(devices, dict):
                    devices = [devices]
                for dev in devices:
                    status.android_devices_detected += 1
                    dev_info = {
                        "caption": str(dev.get("Caption", "")),
                        "device_id": str(dev.get("DeviceID", "")),
                        "status": str(dev.get("Status", "")),
                    }
                    if dev_info["status"].upper() != "OK":
                        status.devices_needing_driver.append(dev_info)

            # Also check for unknown USB devices with Android VIDs
            self._check_unknown_devices(status)

        except Exception as exc:
            log.warning("Driver detection error: %s", exc)
            status.error = str(exc)

        return status

    def _check_unknown_devices(self, status: DriverStatus):
        """Check Device Manager for unknown devices with Android vendor IDs."""
        try:
            ps_cmd = (
                "Get-CimInstance Win32_PnPEntity | "
                "Where-Object { $_.ConfigManagerErrorCode -ne 0 } | "
                "Select-Object Caption, DeviceID | "
                "ConvertTo-Json -Compress"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if not result.stdout.strip():
                return
            import json as _json
            data = result.stdout.strip()
            devices = _json.loads(data)
            if isinstance(devices, dict):
                devices = [devices]
            for dev in devices:
                device_id = str(dev.get("DeviceID", "")).upper()
                caption = str(dev.get("Caption", ""))
                for vid in ANDROID_USB_VID_LIST:
                    vid_clean = vid.replace("0x", "").upper()
                    if f"VID_{vid_clean}" in device_id:
                        status.devices_needing_driver.append({
                            "caption": caption,
                            "device_id": device_id,
                            "status": "needs_driver",
                        })
                        break
        except Exception as exc:
            log.debug("Unknown device check error: %s", exc)

    # ------------------------------------------------------------------
    # Installation
    # ------------------------------------------------------------------
    def install_google_usb_driver(
        self,
        progress_cb: Optional[Callable[[str, int], None]] = None,
    ) -> bool:
        """Download and install Google USB Driver (Windows only)."""
        if os.name != "nt":
            log.info("USB driver installation not needed on this platform")
            return True

        if not self.is_admin():
            log.warning("Admin privileges required for driver installation")
            return False

        try:
            # Step 1: Download
            if progress_cb:
                progress_cb("Downloading Google USB Driver...", 10)

            zip_path = self.drivers_dir / "google_usb_driver.zip"
            if not zip_path.exists():
                urllib.request.urlretrieve(GOOGLE_USB_DRIVER_URL, str(zip_path))

            if progress_cb:
                progress_cb("Extracting driver...", 30)

            # Step 2: Extract
            with zipfile.ZipFile(zip_path, "r") as zf:
                # Peek at the zip to find the actual root folder name
                top_dirs = {n.split('/')[0] for n in zf.namelist() if '/' in n}
                zf.extractall(str(self.drivers_dir))

            # The Google zip extracts to 'usb_driver/' by default
            extract_dir = self.drivers_dir / GOOGLE_USB_DRIVER_EXTRACT_NAME
            if not extract_dir.exists():
                # Fallback: try any directory that was extracted
                for d in top_dirs:
                    candidate = self.drivers_dir / d
                    if candidate.is_dir():
                        extract_dir = candidate
                        break

            if progress_cb:
                progress_cb("Installing driver...", 50)

            log.info("Driver extracted to: %s", extract_dir)

            # Step 3: Find .inf file — search recursively
            inf_file = self._find_inf_file(extract_dir)
            if not inf_file:
                # Search subdirectories
                for sub in extract_dir.rglob("*.inf"):
                    inf_file = sub
                    break

            if not inf_file:
                log.error("Could not find .inf file in driver package")
                return False

            # Step 4: Install via pnputil
            result = subprocess.run(
                ["pnputil", "/add-driver", str(inf_file), "/install"],
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            if progress_cb:
                progress_cb("Verifying installation...", 80)

            success = result.returncode == 0
            if success:
                log.info("Google USB Driver installed successfully")
            else:
                log.warning("Driver install returned %d: %s", result.returncode, result.stderr)

            if progress_cb:
                progress_cb("Complete" if success else "Failed", 100)

            return success

        except Exception as exc:
            log.exception("Driver installation failed: %s", exc)
            if progress_cb:
                progress_cb(f"Error: {exc}", 100)
            return False

    def install_universal_adb_driver(
        self,
        progress_cb: Optional[Callable[[str, int], None]] = None,
    ) -> bool:
        """Install WinUSB driver for Android devices using built-in Windows tools.

        Falls back to pnputil with the Google driver .inf if available,
        or uses PowerShell to attempt WinUSB association.
        """
        if os.name != "nt":
            return True

        try:
            if progress_cb:
                progress_cb("Configuring WinUSB driver via system tools...", 20)

            # Method 1: Check if Google driver .inf is already extracted
            google_dir = self.drivers_dir / GOOGLE_USB_DRIVER_EXTRACT_NAME
            inf_file = None
            if google_dir.exists():
                for f in google_dir.rglob("*.inf"):
                    inf_file = f
                    break

            if inf_file:
                if progress_cb:
                    progress_cb("Installing via pnputil...", 50)
                result = subprocess.run(
                    ["pnputil", "/add-driver", str(inf_file), "/install"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if result.returncode == 0:
                    log.info("WinUSB driver installed via pnputil")
                    if progress_cb:
                        progress_cb("Complete", 100)
                    return True
                log.warning("pnputil returned %d: %s", result.returncode, result.stderr)

            # Method 2: Force WinUSB via PowerShell + pnputil scan
            if progress_cb:
                progress_cb("Scanning for new hardware...", 70)

            subprocess.run(
                ["pnputil", "/scan-devices"],
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            if progress_cb:
                progress_cb("Complete", 100)

            # Verify
            status = self.check_driver_status()
            success = status.drivers_installed
            if not success:
                log.warning("Driver still not detected after installation attempts")
            return success

        except Exception as exc:
            log.exception("Universal driver installation failed: %s", exc)
            if progress_cb:
                progress_cb(f"Error: {exc}", 100)
            return False

    def auto_install_drivers(
        self,
        progress_cb: Optional[Callable[[str, int], None]] = None,
    ) -> bool:
        """Auto-detect and install appropriate drivers."""
        status = self.check_driver_status()

        if not status.is_windows:
            if progress_cb:
                progress_cb("Drivers not needed on this platform", 100)
            return True

        if status.drivers_installed and not status.devices_needing_driver:
            if progress_cb:
                progress_cb("Drivers already installed", 100)
            return True

        log.info("Attempting automatic driver installation...")

        # Detect which chipset families need drivers and install them
        chipsets_detected = self._detect_chipsets_from_devices(status)

        if chipsets_detected:
            log.info("Detected chipsets: %s", ", ".join(chipsets_detected))
            installed_any = False
            total = len(chipsets_detected) + 1  # +1 for Google driver
            step = 0

            # Try Google USB Driver first
            if progress_cb:
                progress_cb("Instalando Google USB Driver...", int(step / total * 80))
            if self.install_google_usb_driver(progress_cb=None):
                installed_any = True
            step += 1

            # Install chipset-specific drivers
            for chipset_key in chipsets_detected:
                chipset_name = CHIPSET_DRIVERS[chipset_key]["name"]
                if progress_cb:
                    pct = int(step / total * 80) + 10
                    progress_cb(f"Instalando driver {chipset_name}...", pct)
                if self.install_chipset_driver(chipset_key, progress_cb=None):
                    installed_any = True
                step += 1

            if progress_cb:
                progress_cb("Verificando...", 90)

            # Scan for new devices after all drivers installed
            subprocess.run(
                ["pnputil", "/scan-devices"],
                capture_output=True, text=True, timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            if progress_cb:
                msg = "Drivers instalados!" if installed_any else "Falha na instalação"
                progress_cb(msg, 100)
            return installed_any
        else:
            # No specific chipset detected, try Google + all major chipsets
            log.info("No specific chipset detected, installing all major drivers...")
            if progress_cb:
                progress_cb("Instalando Google USB Driver...", 10)
            google_ok = self.install_google_usb_driver(progress_cb=None)

            if progress_cb:
                progress_cb("Instalando drivers de chipsets...", 30)
            chipset_ok = self.install_all_chipset_drivers(progress_cb)

            if progress_cb:
                progress_cb("Concluído", 100)
            return google_ok or chipset_ok

    def _detect_chipsets_from_devices(
        self, status: DriverStatus
    ) -> List[str]:
        """Analyse devices_needing_driver to determine which chipset families
        are present so we can install the correct WinUSB .inf."""
        detected: set[str] = set()
        for dev in status.devices_needing_driver:
            dev_id = dev.get("device_id", "").upper()
            for chipset_key, info in CHIPSET_DRIVERS.items():
                for vid, _pid in info["vid_pids"]:
                    if f"VID_{vid.upper()}" in dev_id:
                        detected.add(chipset_key)
                        break
        return list(detected)

    # ------------------------------------------------------------------
    # Chipset-specific driver installation
    # ------------------------------------------------------------------
    def _generate_winusb_inf(self, chipset_key: str) -> Optional[Path]:
        """Generate a WinUSB .inf file for a specific chipset family."""
        if chipset_key not in CHIPSET_DRIVERS:
            log.error("Unknown chipset key: %s", chipset_key)
            return None

        info = CHIPSET_DRIVERS[chipset_key]
        vid_pids = info["vid_pids"]
        driver_name = info["name"]
        manufacturer = info["name"]

        entries_amd64 = []
        entries_x86 = []
        for i, (vid, pid) in enumerate(vid_pids):
            hw_id = f"USB\\VID_{vid}&PID_{pid}"
            label = f"%Dev{i:03d}%"
            entries_amd64.append(f'{label} = USB_Install, {hw_id}')
            entries_x86.append(f'{label} = USB_Install, {hw_id}')

        # Build Strings section labels
        string_defs = []
        for i, (vid, pid) in enumerate(vid_pids):
            string_defs.append(f'Dev{i:03d} = "{driver_name} USB Device (VID_{vid}&PID_{pid})"')

        inf_content = WINUSB_INF_TEMPLATE.format(
            driver_name=driver_name,
            cat_name=f"adb_toolkit_{chipset_key}.cat",
            device_entries_amd64="\n".join(entries_amd64),
            device_entries_x86="\n".join(entries_x86),
            manufacturer=manufacturer,
        )

        # Append device string definitions
        inf_content = inf_content.rstrip() + "\n" + "\n".join(string_defs) + "\n"

        # Write the .inf file
        chipset_dir = self.drivers_dir / f"chipset_{chipset_key}"
        chipset_dir.mkdir(parents=True, exist_ok=True)
        inf_path = chipset_dir / f"adb_toolkit_{chipset_key}.inf"
        inf_path.write_text(inf_content, encoding="utf-8")
        log.info("Generated WinUSB .inf for %s at %s", driver_name, inf_path)
        return inf_path

    def install_chipset_driver(
        self,
        chipset_key: str,
        progress_cb: Optional[Callable[[str, int], None]] = None,
    ) -> bool:
        """Generate and install a WinUSB driver for a specific chipset family.

        Supported keys: 'samsung', 'qualcomm', 'mediatek', 'intel'
        """
        if os.name != "nt":
            if progress_cb:
                progress_cb("Não necessário nesta plataforma", 100)
            return True

        if chipset_key not in CHIPSET_DRIVERS:
            log.error("Unknown chipset: %s", chipset_key)
            if progress_cb:
                progress_cb(f"Chipset desconhecido: {chipset_key}", 100)
            return False

        info = CHIPSET_DRIVERS[chipset_key]
        name = info["name"]

        try:
            if progress_cb:
                progress_cb(f"Gerando driver WinUSB para {name}...", 15)

            inf_path = self._generate_winusb_inf(chipset_key)
            if not inf_path:
                if progress_cb:
                    progress_cb(f"Falha ao gerar .inf para {name}", 100)
                return False

            if progress_cb:
                progress_cb(f"Instalando driver {name} via pnputil...", 40)

            # Install inf via pnputil
            result = subprocess.run(
                ["pnputil", "/add-driver", str(inf_path), "/install"],
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            if progress_cb:
                progress_cb(f"Verificando driver {name}...", 75)

            if result.returncode == 0:
                log.info("%s WinUSB driver installed successfully", name)
            else:
                # pnputil may return non-zero if already installed or no matching device
                log.warning(
                    "pnputil for %s returned %d: %s",
                    name, result.returncode, result.stderr.strip() or result.stdout.strip(),
                )

            # Also register via devcon-style rescan
            subprocess.run(
                ["pnputil", "/scan-devices"],
                capture_output=True, text=True, timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            if progress_cb:
                progress_cb(f"Driver {name} instalado!", 100)

            return result.returncode == 0

        except Exception as exc:
            log.exception("Chipset driver install failed (%s): %s", chipset_key, exc)
            if progress_cb:
                progress_cb(f"Erro: {exc}", 100)
            return False

    def install_samsung_driver(
        self, progress_cb: Optional[Callable[[str, int], None]] = None
    ) -> bool:
        """Install Samsung USB/ADB/ODIN drivers."""
        return self.install_chipset_driver("samsung", progress_cb)

    def install_qualcomm_driver(
        self, progress_cb: Optional[Callable[[str, int], None]] = None
    ) -> bool:
        """Install Qualcomm HS-USB / QDLoader 9008 drivers (Snapdragon)."""
        return self.install_chipset_driver("qualcomm", progress_cb)

    def install_mediatek_driver(
        self, progress_cb: Optional[Callable[[str, int], None]] = None
    ) -> bool:
        """Install MediaTek Preloader / VCOM / DA USB drivers."""
        return self.install_chipset_driver("mediatek", progress_cb)

    def install_intel_driver(
        self, progress_cb: Optional[Callable[[str, int], None]] = None
    ) -> bool:
        """Install Intel Android USB / DnX / SoFIA drivers."""
        return self.install_chipset_driver("intel", progress_cb)

    def install_all_chipset_drivers(
        self,
        progress_cb: Optional[Callable[[str, int], None]] = None,
    ) -> bool:
        """Install WinUSB drivers for ALL supported chipset families."""
        if os.name != "nt":
            return True

        chipsets = list(CHIPSET_DRIVERS.keys())
        total = len(chipsets)
        any_success = False

        for i, key in enumerate(chipsets):
            name = CHIPSET_DRIVERS[key]["name"]
            pct = int((i / total) * 90) + 5
            if progress_cb:
                progress_cb(f"Instalando {name}... ({i+1}/{total})", pct)
            if self.install_chipset_driver(key, progress_cb=None):
                any_success = True

        if progress_cb:
            progress_cb("Todos os drivers de chipset processados", 100)

        return any_success

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _find_inf_file(self, directory: Path) -> Optional[Path]:
        """Find the first .inf file in a directory."""
        if not directory.exists():
            return None
        for f in directory.iterdir():
            if f.suffix.lower() == ".inf":
                return f
        return None

    # ------------------------------------------------------------------
    # Apple / iOS driver support
    # ------------------------------------------------------------------
    def check_apple_driver_status(self) -> Dict[str, any]:
        """Check whether Apple Mobile Device drivers are installed (Windows).

        Returns dict with keys:
            itunes_installed, amds_running, driver_inf_found,
            ios_devices_detected, error
        """
        result: Dict[str, any] = {
            "itunes_installed": False,
            "amds_running": False,
            "driver_inf_found": False,
            "ios_devices_detected": 0,
            "error": None,
        }
        if os.name != "nt":
            result["itunes_installed"] = True  # macOS/Linux don't need iTunes
            result["amds_running"] = True
            result["driver_inf_found"] = True
            return result

        try:
            # 1. Check if iTunes / Apple Mobile Device Support is installed
            for path in APPLE_DRIVER_INF_PATHS:
                if Path(path).exists():
                    result["driver_inf_found"] = True
                    break

            # Check registry for iTunes
            try:
                reg_result = subprocess.run(
                    [
                        "reg", "query",
                        r"HKLM\SOFTWARE\Apple Inc.\Apple Mobile Device Support",
                        "/ve",
                    ],
                    capture_output=True, text=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if reg_result.returncode == 0:
                    result["itunes_installed"] = True
            except Exception:
                pass

            # Also check via WMI for iTunes in installed programs
            if not result["itunes_installed"]:
                try:
                    ps_cmd = (
                        "Get-CimInstance Win32_Product | "
                        "Where-Object { $_.Name -match 'iTunes|Apple Mobile Device' } | "
                        "Select-Object Name | ConvertTo-Json -Compress"
                    )
                    ps = subprocess.run(
                        ["powershell", "-NoProfile", "-Command", ps_cmd],
                        capture_output=True, text=True, timeout=30,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    if ps.stdout.strip() and ps.stdout.strip() != "null":
                        result["itunes_installed"] = True
                except Exception:
                    pass

            # 2. Check if Apple Mobile Device Service is running
            for svc in APPLE_SERVICE_NAMES:
                try:
                    sc_result = subprocess.run(
                        ["sc", "query", svc],
                        capture_output=True, text=True, timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    if "RUNNING" in sc_result.stdout.upper():
                        result["amds_running"] = True
                        break
                except Exception:
                    pass

            # 3. count iOS devices in Device Manager
            try:
                ps_cmd = (
                    "Get-CimInstance Win32_PnPEntity | "
                    "Where-Object { $_.DeviceID -match 'VID_05AC' -or "
                    "$_.Caption -match 'Apple|iPhone|iPad|iPod' } | "
                    "Measure-Object | Select-Object -ExpandProperty Count"
                )
                ps = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True, text=True, timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                cnt = ps.stdout.strip()
                if cnt.isdigit():
                    result["ios_devices_detected"] = int(cnt)
            except Exception:
                pass

        except Exception as exc:
            result["error"] = str(exc)
            log.warning("Apple driver check error: %s", exc)

        return result

    def install_apple_drivers(
        self,
        progress_cb: Optional[Callable[[str, int], None]] = None,
    ) -> bool:
        """Attempt to install Apple Mobile Device drivers on Windows.

        Strategy:
        1. If Apple driver .inf already exists, install via pnputil.
        2. If iTunes is installed but service not running, start it.
        3. If nothing found, download iTunes installer and launch it.
        """
        if os.name != "nt":
            if progress_cb:
                progress_cb("Apple drivers not needed on this platform", 100)
            return True

        try:
            status = self.check_apple_driver_status()

            # Case 1: Driver inf already on disk → pnputil install
            if status["driver_inf_found"]:
                if progress_cb:
                    progress_cb("Apple driver found — installing via pnputil...", 20)

                inf_installed = False
                for drv_dir in APPLE_DRIVER_INF_PATHS:
                    drv_path = Path(drv_dir)
                    if not drv_path.exists():
                        continue
                    for inf in drv_path.rglob("*.inf"):
                        try:
                            res = subprocess.run(
                                ["pnputil", "/add-driver", str(inf), "/install"],
                                capture_output=True, text=True, timeout=120,
                                creationflags=subprocess.CREATE_NO_WINDOW,
                            )
                            if res.returncode == 0:
                                inf_installed = True
                        except Exception:
                            pass

                if progress_cb:
                    progress_cb("Starting Apple Mobile Device Service...", 60)

                # Ensure the service is running
                self._start_apple_service()

                if progress_cb:
                    progress_cb("Scanning for new devices...", 80)

                subprocess.run(
                    ["pnputil", "/scan-devices"],
                    capture_output=True, text=True, timeout=60,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )

                if progress_cb:
                    progress_cb("Apple drivers installed!", 100)
                return inf_installed or status["amds_running"]

            # Case 2: iTunes installed, service not running
            if status["itunes_installed"] and not status["amds_running"]:
                if progress_cb:
                    progress_cb("Starting Apple Mobile Device Service...", 40)
                self._start_apple_service()
                if progress_cb:
                    progress_cb("Service started!", 100)
                return True

            # Case 3: Nothing found — download iTunes
            if progress_cb:
                progress_cb("Downloading iTunes installer...", 10)

            itunes_exe = self.drivers_dir / "iTunesSetup.exe"
            if not itunes_exe.exists():
                try:
                    urllib.request.urlretrieve(ITUNES_DOWNLOAD_URL, str(itunes_exe))
                except Exception as exc:
                    log.warning("iTunes download failed: %s", exc)
                    # Try Microsoft Store approach
                    if progress_cb:
                        progress_cb("Opening iTunes in Microsoft Store...", 50)
                    try:
                        subprocess.Popen(
                            ["start", "ms-windows-store://pdp/?ProductId=9PB2MZ1ZMB1S"],
                            shell=True,
                        )
                        if progress_cb:
                            progress_cb(
                                "Microsoft Store opened — install iTunes, then retry.",
                                100,
                            )
                        return False
                    except Exception:
                        if progress_cb:
                            progress_cb(f"Download failed: {exc}", 100)
                        return False

            if progress_cb:
                progress_cb("Launching iTunes installer...", 50)

            # Launch iTunes installer (user must complete)
            subprocess.Popen(
                [str(itunes_exe)],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )

            if progress_cb:
                progress_cb(
                    "iTunes installer launched — complete installation then retry.",
                    100,
                )
            return True  # installer launched

        except Exception as exc:
            log.exception("Apple driver install failed: %s", exc)
            if progress_cb:
                progress_cb(f"Error: {exc}", 100)
            return False

    def _start_apple_service(self):
        """Attempt to start Apple Mobile Device Service."""
        for svc in APPLE_SERVICE_NAMES:
            try:
                subprocess.run(
                    ["net", "start", svc],
                    capture_output=True, text=True, timeout=30,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception:
                pass

    def restart_adb_after_driver(self, adb_path: str):
        """Kill and restart ADB server after driver installation."""
        try:
            subprocess.run(
                [adb_path, "kill-server"],
                capture_output=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            import time
            time.sleep(2)
            subprocess.run(
                [adb_path, "start-server"],
                capture_output=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as exc:
            log.warning("Failed to restart ADB server: %s", exc)
