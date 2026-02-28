"""
format_converter.py - Cross-platform data format converters.

Handles conversion between Android and iOS data formats:
  - Contacts: VCF ↔ SQLite (AddressBook)
  - SMS: Android XML/JSON ↔ iOS sms.db
  - Calendar: ICS (both platforms use it natively)
  - Photos/Videos: HEIC → JPEG (iOS → Android compatibility)

These converters allow TransferManager to move data between platforms
transparently.
"""

import json
import logging
import os
import re
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .device_interface import CalendarEvent, ContactEntry, SMSEntry

log = logging.getLogger("adb_toolkit.format_converter")


# ---------------------------------------------------------------------------
# VCF (vCard) Parser / Writer
# ---------------------------------------------------------------------------
class VCardConverter:
    """Parse and generate VCF 3.0 vCard files."""

    @staticmethod
    def parse_vcf(vcf_path: Path) -> List[ContactEntry]:
        """Read a VCF file and return a list of ContactEntry."""
        contacts: List[ContactEntry] = []
        if not vcf_path.exists():
            return contacts

        text = vcf_path.read_text(encoding="utf-8", errors="replace")
        # Split into individual vCards
        cards = re.split(r"(?=BEGIN:VCARD)", text)

        for card in cards:
            card = card.strip()
            if not card.startswith("BEGIN:VCARD"):
                continue

            entry = ContactEntry(raw_vcard=card)

            # FN (display name)
            m = re.search(r"FN[;:](.+)", card)
            if m:
                entry.display_name = m.group(1).strip()

            # N (structured name) — fallback
            if not entry.display_name:
                m = re.search(r"N[;:]([^;]*);([^;]*)", card)
                if m:
                    entry.display_name = f"{m.group(2)} {m.group(1)}".strip()

            # TEL lines
            for m in re.finditer(r"TEL[^:]*:(.+)", card):
                phone = m.group(1).strip()
                if phone:
                    entry.phones.append(phone)

            # EMAIL lines
            for m in re.finditer(r"EMAIL[^:]*:(.+)", card):
                email = m.group(1).strip()
                if email:
                    entry.emails.append(email)

            # ORG
            m = re.search(r"ORG[;:](.+)", card)
            if m:
                entry.organization = m.group(1).strip()

            # NOTE
            m = re.search(r"NOTE[;:](.+)", card)
            if m:
                entry.note = m.group(1).strip()

            contacts.append(entry)

        log.info("Parsed %d contacts from %s", len(contacts), vcf_path.name)
        return contacts

    @staticmethod
    def write_vcf(contacts: List[ContactEntry], out_path: Path) -> Path:
        """Write a list of ContactEntry objects to a VCF file."""
        lines: List[str] = []
        for c in contacts:
            # If we have raw vcard, use it as-is
            if c.raw_vcard:
                lines.append(c.raw_vcard)
                continue

            parts = c.display_name.split(None, 1)
            first = parts[0] if parts else ""
            last = parts[1] if len(parts) > 1 else ""

            vcard = "BEGIN:VCARD\n"
            vcard += "VERSION:3.0\n"
            vcard += f"N:{last};{first};;;\n"
            vcard += f"FN:{c.display_name}\n"
            if c.organization:
                vcard += f"ORG:{c.organization}\n"
            for ph in c.phones:
                vcard += f"TEL;TYPE=CELL:{ph}\n"
            for em in c.emails:
                vcard += f"EMAIL;TYPE=INTERNET:{em}\n"
            if c.note:
                vcard += f"NOTE:{c.note}\n"
            vcard += "END:VCARD"
            lines.append(vcard)

        out_path.write_text("\n".join(lines), encoding="utf-8")
        log.info("Wrote %d contacts to %s", len(contacts), out_path.name)
        return out_path


# ---------------------------------------------------------------------------
# SMS JSON ↔ iOS sms.db converter
# ---------------------------------------------------------------------------
class SMSConverter:
    """Convert SMS between Android JSON format and iOS sms.db."""

    # iOS epoch: 2001-01-01 00:00:00 UTC
    _IOS_EPOCH_OFFSET = 978307200

    @staticmethod
    def parse_android_json(json_path: Path) -> List[SMSEntry]:
        """Read Android SMS JSON and return SMSEntry list."""
        if not json_path.exists():
            return []
        data = json.loads(json_path.read_text(encoding="utf-8"))
        entries: List[SMSEntry] = []
        for msg in data:
            entries.append(SMSEntry(
                address=msg.get("address", ""),
                body=msg.get("body", ""),
                date_ms=int(msg.get("date", 0)),
                msg_type=int(msg.get("type", 1)),
                read=msg.get("read", "1") == "1",
                thread_id=int(msg.get("thread_id", 0)),
            ))
        return entries

    @staticmethod
    def write_android_json(entries: List[SMSEntry], out_path: Path) -> Path:
        """Write SMSEntry list to Android-compatible JSON."""
        data = []
        for e in entries:
            data.append({
                "address": e.address,
                "body": e.body,
                "date": str(e.date_ms),
                "type": str(e.msg_type),
                "read": "1" if e.read else "0",
                "thread_id": str(e.thread_id),
            })
        out_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return out_path

    @classmethod
    def parse_ios_sms_db(cls, sms_db_path: Path) -> List[SMSEntry]:
        """Read iOS sms.db (SQLite) and return SMSEntry list."""
        if not sms_db_path.exists():
            return []

        entries: List[SMSEntry] = []
        try:
            conn = sqlite3.connect(str(sms_db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT
                    h.id AS address,
                    m.text AS body,
                    m.date AS date,
                    m.is_from_me AS is_from_me,
                    m.is_read AS is_read
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                WHERE m.text IS NOT NULL AND m.text != ''
                ORDER BY m.date ASC
            """)
            for row in cursor:
                # iOS date: nanoseconds since 2001-01-01 (newer versions)
                # or seconds since 2001-01-01 (older)
                raw_date = row["date"] or 0
                if raw_date > 1_000_000_000_000:
                    # Nanoseconds
                    unix_s = raw_date / 1e9 + cls._IOS_EPOCH_OFFSET
                else:
                    unix_s = raw_date + cls._IOS_EPOCH_OFFSET

                entries.append(SMSEntry(
                    address=row["address"] or "",
                    body=row["body"] or "",
                    date_ms=int(unix_s * 1000),
                    msg_type=2 if row["is_from_me"] else 1,
                    read=bool(row["is_read"]),
                ))
            conn.close()
        except Exception as exc:
            log.warning("Failed to parse iOS sms.db: %s", exc)
        return entries

    @classmethod
    def ios_sms_to_android_json(cls, sms_db_path: Path, out_path: Path) -> Optional[Path]:
        """Convert iOS sms.db directly to Android JSON format."""
        entries = cls.parse_ios_sms_db(sms_db_path)
        if not entries:
            return None
        return cls.write_android_json(entries, out_path)

    @classmethod
    def android_json_to_entries(cls, json_path: Path) -> List[SMSEntry]:
        """Alias for parse_android_json."""
        return cls.parse_android_json(json_path)


# ---------------------------------------------------------------------------
# Calendar ICS converter
# ---------------------------------------------------------------------------
class CalendarConverter:
    """Parse and generate ICS (iCalendar) files.

    Both Android and iOS support ICS natively, so this is mostly about
    reading/writing the standard format.
    """

    @staticmethod
    def parse_ics(ics_path: Path) -> List[CalendarEvent]:
        """Parse an ICS file into CalendarEvent list."""
        if not ics_path.exists():
            return []
        text = ics_path.read_text(encoding="utf-8", errors="replace")
        events: List[CalendarEvent] = []

        # Split by VEVENT blocks
        blocks = re.findall(
            r"BEGIN:VEVENT(.*?)END:VEVENT",
            text, re.DOTALL,
        )
        for block in blocks:
            ev = CalendarEvent()

            def _get(key: str) -> str:
                m = re.search(rf"{key}[^:]*:(.*)", block)
                return m.group(1).strip() if m else ""

            ev.uid = _get("UID")
            ev.summary = _get("SUMMARY")
            ev.description = _get("DESCRIPTION")
            ev.dtstart = _get("DTSTART")
            ev.dtend = _get("DTEND")
            ev.location = _get("LOCATION")
            ev.raw_ics = f"BEGIN:VEVENT{block}END:VEVENT"
            events.append(ev)

        return events

    @staticmethod
    def write_ics(events: List[CalendarEvent], out_path: Path) -> Path:
        """Write CalendarEvent list to ICS file."""
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//ADB Toolkit//Cross-Platform Transfer//PT",
        ]
        for ev in events:
            if ev.raw_ics:
                lines.append(ev.raw_ics)
            else:
                lines.append("BEGIN:VEVENT")
                if ev.uid:
                    lines.append(f"UID:{ev.uid}")
                if ev.summary:
                    lines.append(f"SUMMARY:{ev.summary}")
                if ev.description:
                    lines.append(f"DESCRIPTION:{ev.description}")
                if ev.dtstart:
                    lines.append(f"DTSTART:{ev.dtstart}")
                if ev.dtend:
                    lines.append(f"DTEND:{ev.dtend}")
                if ev.location:
                    lines.append(f"LOCATION:{ev.location}")
                lines.append("END:VEVENT")
        lines.append("END:VCALENDAR")
        out_path.write_text("\n".join(lines), encoding="utf-8")
        return out_path


# ---------------------------------------------------------------------------
# HEIC → JPEG converter (iOS photos → Android)
# ---------------------------------------------------------------------------
class PhotoConverter:
    """Convert photo formats between platforms.

    iOS uses HEIC by default since iPhone 7 / iOS 11.
    Android can read HEIC on newer versions but older devices need JPEG.
    """

    _PILLOW_HEIF_AVAILABLE: Optional[bool] = None

    @classmethod
    def _check_pillow_heif(cls) -> bool:
        if cls._PILLOW_HEIF_AVAILABLE is None:
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
                cls._PILLOW_HEIF_AVAILABLE = True
            except ImportError:
                cls._PILLOW_HEIF_AVAILABLE = False
        return cls._PILLOW_HEIF_AVAILABLE

    @classmethod
    def heic_to_jpeg(cls, heic_path: Path, jpeg_path: Optional[Path] = None) -> Optional[Path]:
        """Convert a HEIC image to JPEG.

        Returns the JPEG path, or None if conversion failed.
        Requires: pip install pillow-heif
        """
        if not cls._check_pillow_heif():
            log.warning("pillow-heif not installed — cannot convert HEIC to JPEG")
            return None

        if jpeg_path is None:
            jpeg_path = heic_path.with_suffix(".jpg")

        try:
            from PIL import Image
            img = Image.open(heic_path)
            img.save(jpeg_path, "JPEG", quality=95)
            return jpeg_path
        except Exception as exc:
            log.warning("HEIC → JPEG conversion failed for %s: %s", heic_path.name, exc)
            return None

    @classmethod
    def needs_conversion(cls, filename: str, target_platform: str) -> bool:
        """Check if a file needs format conversion for the target platform."""
        ext = Path(filename).suffix.lower()
        if target_platform == "android" and ext in (".heic", ".heif"):
            return True
        # Android → iOS: JPEGs work fine on iOS, no conversion needed
        return False

    @classmethod
    def convert_if_needed(
        cls,
        file_path: Path,
        target_platform: str,
        out_dir: Optional[Path] = None,
    ) -> Path:
        """Convert a media file if the target platform needs it.

        Returns the (possibly converted) file path.
        """
        if not cls.needs_conversion(file_path.name, target_platform):
            return file_path

        out = out_dir or file_path.parent
        out.mkdir(parents=True, exist_ok=True)

        if file_path.suffix.lower() in (".heic", ".heif"):
            result = cls.heic_to_jpeg(file_path, out / (file_path.stem + ".jpg"))
            if result:
                return result

        return file_path  # fallback: return original
