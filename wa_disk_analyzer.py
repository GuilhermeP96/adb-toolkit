#!/usr/bin/env python3
"""
WhatsApp Media Disk Analyzer — ADB Toolkit
=============================================
Disk-usage analyzer for WhatsApp media on Android device.
Shows exactly where space is going: by folder, size bracket,
timeline, largest files, and Sent/ vs Received analysis.

Output: interactive console report + detailed files in toolbox_output/
"""

import subprocess
import sys
import os
import re
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta

# ─── Configuration ───────────────────────────────────────────────────
ADB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform-tools", "adb.exe")
WA_MEDIA_DIR = "/sdcard/Android/media/com.whatsapp/WhatsApp/Media"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "toolbox_output")

# Size brackets for distribution
SIZE_BRACKETS = [
    (0,           1 << 20,  "< 1 MB"),
    (1 << 20,     5 << 20,  "1–5 MB"),
    (5 << 20,    10 << 20,  "5–10 MB"),
    (10 << 20,   25 << 20,  "10–25 MB"),
    (25 << 20,   50 << 20,  "25–50 MB"),
    (50 << 20,  100 << 20,  "50–100 MB"),
    (100 << 20, 250 << 20,  "100–250 MB"),
    (250 << 20, 500 << 20,  "250–500 MB"),
    (500 << 20, 1 << 30,    "500 MB–1 GB"),
    (1 << 30,   999 << 30,  "> 1 GB"),
]


def adb_shell(cmd: str, timeout: int = 300) -> str:
    result = subprocess.run(
        [ADB, "shell", cmd],
        capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace"
    )
    return result.stdout.strip()


def adb_shell_lines(cmd: str, timeout: int = 600) -> list[str]:
    out = adb_shell(cmd, timeout)
    return [l for l in out.splitlines() if l.strip()]


# Regex for WhatsApp filename date: VID-20250814-WA0041.mp4 → 2025-08-14
_WA_DATE_RE = re.compile(r"(?:VID|IMG|AUD|DOC|STK|PTT)-?(\d{4})(\d{2})(\d{2})-WA")


def parse_filename_date(basename: str) -> datetime | None:
    """Extract real date from WhatsApp filename convention."""
    m = _WA_DATE_RE.search(basename)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


class FileEntry:
    __slots__ = ("path", "size", "mtime", "folder", "basename", "real_date")

    def __init__(self, path: str, size: int, mtime: int):
        self.path = path
        self.size = size
        self.mtime = mtime
        self.basename = os.path.basename(path)
        self.real_date: datetime | None = parse_filename_date(self.basename)
        # Classify folder — aggregate by media type + subfolder
        rel = path.replace(WA_MEDIA_DIR + "/", "")
        parts = rel.split("/")
        if len(parts) >= 3:
            # e.g. WhatsApp Video/Sent/VID-xxx.mp4 → "WhatsApp Video/Sent"
            self.folder = parts[0] + "/" + parts[1]
        elif len(parts) == 2:
            # e.g. WhatsApp Video/VID-xxx.mp4 → "WhatsApp Video"
            self.folder = parts[0]
        else:
            self.folder = parts[0]


def format_bytes(b: int) -> str:
    if b >= 1 << 30:
        return f"{b / (1 << 30):.2f} GB"
    if b >= 1 << 20:
        return f"{b / (1 << 20):.1f} MB"
    if b >= 1 << 10:
        return f"{b / (1 << 10):.1f} KB"
    return f"{b} B"


def format_bar(fraction: float, width: int = 40) -> str:
    filled = int(fraction * width)
    return "█" * filled + "░" * (width - filled)


def scan_all_files() -> list[FileEntry]:
    """Scan entire WhatsApp Media tree with stat."""
    print("\n  Escaneando toda a árvore WhatsApp Media...")
    cmd = (
        f'find "{WA_MEDIA_DIR}" -type f '
        f'-exec stat -c "%s|%Y|%n" {{}} + 2>/dev/null'
    )
    lines = adb_shell_lines(cmd, timeout=180)

    files = []
    for line in lines:
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        try:
            size = int(parts[0])
            mtime = int(parts[1])
        except ValueError:
            continue
        files.append(FileEntry(parts[2], size, mtime))

    print(f"  Total: {len(files)} arquivos")
    return files


# ═════════════════════════════════════════════════════════════════════
#  Analysis modules
# ═════════════════════════════════════════════════════════════════════

def analyze_folder_tree(files: list[FileEntry]) -> list[str]:
    """TreeSize-style folder breakdown — aggregated by media type."""
    lines = []
    lines.append("┌─────────────────────────────────────────────────────────────────────────────────┐")
    lines.append("│  1. MAPA DE DISCO — Árvore de Pastas (estilo TreeSize)                         │")
    lines.append("└─────────────────────────────────────────────────────────────────────────────────┘")
    lines.append("")

    # Group by media type folder (aggregated)
    folder_stats: dict[str, dict] = defaultdict(lambda: {"count": 0, "size": 0})
    total_size = sum(f.size for f in files)

    for f in files:
        folder_stats[f.folder]["count"] += 1
        folder_stats[f.folder]["size"] += f.size

    # Sort by size descending
    sorted_folders = sorted(folder_stats.items(), key=lambda x: -x[1]["size"])

    lines.append(f"  {'Pasta':<45} {'Arquivos':>8}  {'Tamanho':>12}  {'%':>6}  Barra")
    lines.append("  " + "─" * 100)

    for folder, stats in sorted_folders:
        count = stats["count"]
        size = stats["size"]
        pct = size * 100 / total_size if total_size else 0
        bar = format_bar(size / total_size if total_size else 0, 30)
        lines.append(f"  {folder:<45} {count:>8,}  {format_bytes(size):>12}  {pct:>5.1f}%  {bar}")

    lines.append("  " + "─" * 100)
    lines.append(f"  {'TOTAL':<45} {len(files):>8,}  {format_bytes(total_size):>12}  100.0%")
    lines.append("")

    return lines


def analyze_video_subfolders(files: list[FileEntry]) -> list[str]:
    """Deep breakdown of WhatsApp Video specifically."""
    lines = []
    lines.append("┌─────────────────────────────────────────────────────────────────────────────────┐")
    lines.append("│  2. ANÁLISE DETALHADA — WhatsApp Video                                         │")
    lines.append("└─────────────────────────────────────────────────────────────────────────────────┘")
    lines.append("")

    video_files = [f for f in files if "WhatsApp Video" in f.folder]
    if not video_files:
        lines.append("  Nenhum vídeo encontrado.")
        return lines

    # Classify: root (received), Sent, Private
    received = [f for f in video_files if "/Sent" not in f.path and "/Private" not in f.path]
    sent = [f for f in video_files if "/Sent/" in f.path]
    private = [f for f in video_files if "/Private/" in f.path]

    total_size = sum(f.size for f in video_files)

    categories = [
        ("📥 Recebidos", received),
        ("📤 Enviados (Sent/)", sent),
        ("🔒 Privados (Private/)", private),
    ]

    for label, group in categories:
        if not group:
            continue
        g_size = sum(f.size for f in group)
        g_pct = g_size * 100 / total_size if total_size else 0
        avg = g_size // len(group) if group else 0
        biggest = max(group, key=lambda f: f.size)
        smallest_real = [f for f in group if f.size > 100]  # skip .nomedia
        smallest = min(smallest_real, key=lambda f: f.size) if smallest_real else min(group, key=lambda f: f.size)

        # Use real dates from filenames
        dated = [f for f in group if f.real_date]
        oldest_date = min(dated, key=lambda f: f.real_date) if dated else None
        newest_date = max(dated, key=lambda f: f.real_date) if dated else None

        lines.append(f"  {label}")
        lines.append(f"    Arquivos:   {len(group):>8,}")
        lines.append(f"    Tamanho:    {format_bytes(g_size):>12}  ({g_pct:.1f}% do total de vídeos)")
        lines.append(f"    Média:      {format_bytes(avg):>12} por arquivo")
        lines.append(f"    Maior:      {format_bytes(biggest.size):>12}  — {biggest.basename}")
        lines.append(f"    Menor:      {format_bytes(smallest.size):>12}  — {smallest.basename}")
        if oldest_date:
            lines.append(f"    Mais antigo:{oldest_date.real_date.strftime('%Y-%m-%d'):>13}  — {oldest_date.basename}")
        if newest_date:
            lines.append(f"    Mais recente:{newest_date.real_date.strftime('%Y-%m-%d'):>12}  — {newest_date.basename}")
        if dated:
            lines.append(f"    Datas válidas: {len(dated)}/{len(group)} arquivos")
        lines.append("")

    # Sent analysis: how much of Sent is also in Received? (same basename pattern)
    if sent and received:
        lines.append("  📊 Análise Sent/ vs Recebidos:")
        # Check sent files that have same basename in received (VID-YYYYMMDD-WANNN.mp4)
        received_basenames = {f.basename for f in received}
        sent_also_received = [f for f in sent if f.basename in received_basenames]
        sent_only = [f for f in sent if f.basename not in received_basenames]

        sar_size = sum(f.size for f in sent_also_received)
        so_size = sum(f.size for f in sent_only)
        lines.append(f"    Sent com mesmo nome no Recebidos: {len(sent_also_received):>6,} arquivos = {format_bytes(sar_size)}")
        lines.append(f"    Sent sem correspondência:         {len(sent_only):>6,} arquivos = {format_bytes(so_size)}")
        lines.append(f"    ⚠ Esses {len(sent_also_received)} na Sent/ podem ser cópias de encaminhamento")
        lines.append("")

    return lines


def analyze_size_distribution(files: list[FileEntry], label: str = "Todos") -> list[str]:
    """Distribution by file size brackets."""
    lines = []
    lines.append("┌─────────────────────────────────────────────────────────────────────────────────┐")
    lines.append(f"│  3. DISTRIBUIÇÃO POR TAMANHO — {label:<47} │")
    lines.append("└─────────────────────────────────────────────────────────────────────────────────┘")
    lines.append("")

    video_files = [f for f in files if "WhatsApp Video" in f.folder]
    if not video_files:
        return lines

    total_size = sum(f.size for f in video_files)
    total_count = len(video_files)

    lines.append(f"  {'Faixa':<16} {'Arquivos':>8} {'%Arq':>6}  {'Tamanho':>12} {'%Tam':>6}  Barra (por tamanho)")
    lines.append("  " + "─" * 90)

    for lo, hi, label_bracket in SIZE_BRACKETS:
        bracket_files = [f for f in video_files if lo <= f.size < hi]
        if not bracket_files:
            continue
        count = len(bracket_files)
        size = sum(f.size for f in bracket_files)
        pct_count = count * 100 / total_count if total_count else 0
        pct_size = size * 100 / total_size if total_size else 0
        bar = format_bar(size / total_size if total_size else 0, 30)
        lines.append(f"  {label_bracket:<16} {count:>8,} {pct_count:>5.1f}%  {format_bytes(size):>12} {pct_size:>5.1f}%  {bar}")

    lines.append("  " + "─" * 90)
    lines.append(f"  {'TOTAL':<16} {total_count:>8,}        {format_bytes(total_size):>12}")
    lines.append("")

    # Same for Sent/ only
    sent_files = [f for f in video_files if "/Sent/" in f.path]
    if sent_files:
        sent_total = sum(f.size for f in sent_files)
        sent_count = len(sent_files)

        lines.append(f"  → Apenas Sent/:")
        lines.append(f"  {'Faixa':<16} {'Arquivos':>8} {'%Arq':>6}  {'Tamanho':>12} {'%Tam':>6}")
        lines.append("  " + "─" * 60)

        for lo, hi, label_bracket in SIZE_BRACKETS:
            bracket_files = [f for f in sent_files if lo <= f.size < hi]
            if not bracket_files:
                continue
            count = len(bracket_files)
            size = sum(f.size for f in bracket_files)
            pct_count = count * 100 / sent_count if sent_count else 0
            pct_size = size * 100 / sent_total if sent_total else 0
            lines.append(f"  {label_bracket:<16} {count:>8,} {pct_count:>5.1f}%  {format_bytes(size):>12} {pct_size:>5.1f}%")

        lines.append("  " + "─" * 60)
        lines.append(f"  {'TOTAL Sent/':<16} {sent_count:>8,}        {format_bytes(sent_total):>12}")
        lines.append("")

    return lines


def analyze_timeline(files: list[FileEntry]) -> list[str]:
    """Monthly timeline of video accumulation."""
    lines = []
    lines.append("┌─────────────────────────────────────────────────────────────────────────────────┐")
    lines.append("│  4. TIMELINE — Acúmulo Mensal de Vídeos                                        │")
    lines.append("└─────────────────────────────────────────────────────────────────────────────────┘")
    lines.append("")

    video_files = [f for f in files if "WhatsApp Video" in f.folder]
    if not video_files:
        return lines

    # Group by YYYY-MM using REAL date from filename
    monthly: dict[str, dict] = defaultdict(lambda: {
        "count": 0, "size": 0,
        "sent_count": 0, "sent_size": 0,
        "recv_count": 0, "recv_size": 0,
    })

    no_date = 0
    for f in video_files:
        if f.real_date:
            key = f.real_date.strftime("%Y-%m")
        else:
            no_date += 1
            continue
        monthly[key]["count"] += 1
        monthly[key]["size"] += f.size
        if "/Sent/" in f.path:
            monthly[key]["sent_count"] += 1
            monthly[key]["sent_size"] += f.size
        else:
            monthly[key]["recv_count"] += 1
            monthly[key]["recv_size"] += f.size

    if no_date:
        lines.append(f"  (!) {no_date} arquivos sem data no nome foram ignorados")
        lines.append("")

    total_size = sum(f.size for f in video_files)
    max_month_size = max(m["size"] for m in monthly.values()) if monthly else 1

    lines.append(f"  {'Mês':<10} {'Arq':>6} {'Tamanho':>10} {'Recv':>6} {'Recv GB':>8} {'Sent':>6} {'Sent GB':>8}  Barra")
    lines.append("  " + "─" * 95)

    cumulative = 0
    for key in sorted(monthly.keys()):
        m = monthly[key]
        cumulative += m["size"]
        bar = format_bar(m["size"] / max_month_size if max_month_size else 0, 25)
        lines.append(
            f"  {key:<10} {m['count']:>6,} {format_bytes(m['size']):>10} "
            f"{m['recv_count']:>6,} {format_bytes(m['recv_size']):>8} "
            f"{m['sent_count']:>6,} {format_bytes(m['sent_size']):>8}  {bar}"
        )

    lines.append("  " + "─" * 95)
    lines.append(f"  Acumulado total: {format_bytes(cumulative)}")
    lines.append("")

    return lines


def analyze_top_files(files: list[FileEntry], top_n: int = 50) -> list[str]:
    """Top N largest files."""
    lines = []
    lines.append("┌─────────────────────────────────────────────────────────────────────────────────┐")
    lines.append(f"│  5. TOP {top_n} MAIORES ARQUIVOS                                                    │")
    lines.append("└─────────────────────────────────────────────────────────────────────────────────┘")
    lines.append("")

    video_files = sorted(
        [f for f in files if "WhatsApp Video" in f.folder],
        key=lambda f: -f.size
    )[:top_n]

    total_top = sum(f.size for f in video_files)
    lines.append(f"  Top {top_n} totalizam: {format_bytes(total_top)}")
    lines.append("")

    lines.append(f"  {'#':>4} {'Tamanho':>12} {'Data Real':>12} {'Tipo':>6} {'Arquivo'}")
    lines.append("  " + "─" * 100)

    for idx, f in enumerate(video_files, 1):
        date = f.real_date.strftime("%Y-%m-%d") if f.real_date else "????"
        tipo = "SENT" if "/Sent/" in f.path else "RECV"
        lines.append(f"  {idx:>4} {format_bytes(f.size):>12} {date:>12} {tipo:>6} {f.basename}")

    lines.append("")

    return lines


def analyze_sent_deep(files: list[FileEntry]) -> list[str]:
    """Deep analysis of Sent/ folder — candidates for cleanup."""
    lines = []
    lines.append("┌─────────────────────────────────────────────────────────────────────────────────┐")
    lines.append("│  6. DEEP DIVE — Pasta Sent/ (candidatos a limpeza)                             │")
    lines.append("└─────────────────────────────────────────────────────────────────────────────────┘")
    lines.append("")

    sent = sorted(
        [f for f in files if "/Sent/" in f.path and "WhatsApp Video" in f.folder],
        key=lambda f: -f.size
    )

    if not sent:
        lines.append("  Nenhum vídeo em Sent/.")
        return lines

    total_sent = sum(f.size for f in sent)
    received = [f for f in files if "WhatsApp Video" in f.folder and "/Sent/" not in f.path and "/Private/" not in f.path]
    received_basenames = {f.basename: f for f in received}

    # Categorize sent files
    forwarded = []           # same filename exists in received
    large_originals = []     # >50MB, not duplicated
    medium = []              # 10-50MB
    small = []               # <10MB

    for f in sent:
        if f.basename in received_basenames and f.basename != '.nomedia':
            forwarded.append((f, received_basenames[f.basename]))
        elif f.size > 50 << 20:
            large_originals.append(f)
        elif f.size > 10 << 20:
            medium.append(f)
        else:
            small.append(f)

    fwd_size = sum(f.size for f, _ in forwarded)
    large_size = sum(f.size for f in large_originals)
    medium_size = sum(f.size for f in medium)
    small_size = sum(f.size for f in small)

    lines.append(f"  Total Sent/: {len(sent):,} arquivos = {format_bytes(total_sent)}")
    lines.append("")
    lines.append(f"  📋 Categorias:")
    lines.append(f"    🔄 Reencaminhados (existem no Recebidos):  {len(forwarded):>6,} arq = {format_bytes(fwd_size):>10}")
    lines.append(f"    📦 Originais grandes (>50MB):              {len(large_originals):>6,} arq = {format_bytes(large_size):>10}")
    lines.append(f"    📄 Originais médios (10–50MB):             {len(medium):>6,} arq = {format_bytes(medium_size):>10}")
    lines.append(f"    📎 Pequenos (<10MB):                       {len(small):>6,} arq = {format_bytes(small_size):>10}")
    lines.append("")

    # Show forwarded files (same name in Sent and Received)
    if forwarded:
        lines.append(f"  🔄 Detalhes — Reencaminhados ({len(forwarded)} arquivos = {format_bytes(fwd_size)}):")
        lines.append(f"     Esses arquivos têm o MESMO nome na pasta de recebidos.")
        lines.append(f"     Se SHA-256 for idêntico, a versão em Sent/ pode ser removida com segurança.")
        lines.append("")

        forwarded.sort(key=lambda x: -x[0].size)
        for i, (sf, rf) in enumerate(forwarded[:30], 1):
            s_date = datetime.fromtimestamp(sf.mtime).strftime("%Y-%m-%d")
            r_date = datetime.fromtimestamp(rf.mtime).strftime("%Y-%m-%d")
            size_match = "✓ Mesmo tamanho" if sf.size == rf.size else f"✗ Diff: Sent={format_bytes(sf.size)} vs Recv={format_bytes(rf.size)}"
            lines.append(f"     {i:>3}. {sf.basename}")
            lines.append(f"          Sent: {format_bytes(sf.size):>10} ({s_date})  |  Recv: {format_bytes(rf.size):>10} ({r_date})  |  {size_match}")

        if len(forwarded) > 30:
            lines.append(f"     ... e mais {len(forwarded) - 30} arquivos")
        lines.append("")

    # Large originals in Sent/ (camera videos sent)
    if large_originals:
        lines.append(f"  📦 Top 20 — Originais grandes em Sent/ (>50MB):")
        for i, f in enumerate(large_originals[:20], 1):
            date = datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d")
            lines.append(f"     {i:>3}. {format_bytes(f.size):>12}  {date}  {f.basename}")
        if len(large_originals) > 20:
            rest_size = sum(f.size for f in large_originals[20:])
            lines.append(f"     ... e mais {len(large_originals)-20} arquivos ({format_bytes(rest_size)})")
        lines.append("")

    # Cleanup recommendations
    lines.append("  💡 RECOMENDAÇÕES DE LIMPEZA (Sent/):")
    lines.append("")

    # 1. Forwarded with same size (safe to delete Sent copy)
    same_size_fwd = [(s, r) for s, r in forwarded if s.size == r.size]
    same_size_total = sum(s.size for s, _ in same_size_fwd)
    if same_size_fwd:
        lines.append(f"    ✅ SEGURO: {len(same_size_fwd)} reencaminhados com tamanho idêntico ao recebido")
        lines.append(f"       → Remover versão Sent/ economiza {format_bytes(same_size_total)}")
        lines.append(f"       (Confirmar SHA-256 antes para 100% certeza)")
        lines.append("")

    # 2. All Sent/ files (aggressive)
    lines.append(f"    ⚠️  AGRESSIVO: Remover TODA a pasta Sent/ economiza {format_bytes(total_sent)}")
    lines.append(f"       (Os vídeos enviados ficam no chat, e o WhatsApp re-baixa se necessário)")
    lines.append("")

    # 3. Old Sent/ files — by REAL filename date
    now = datetime.now()
    old_90 = [f for f in sent if f.real_date and (now - f.real_date).days > 90]
    old_90_size = sum(f.size for f in old_90)
    old_180 = [f for f in sent if f.real_date and (now - f.real_date).days > 180]
    old_180_size = sum(f.size for f in old_180)
    old_365 = [f for f in sent if f.real_date and (now - f.real_date).days > 365]
    old_365_size = sum(f.size for f in old_365)

    if old_90:
        lines.append(f"    🕐 MODERADO: {len(old_90):,} vídeos Sent/ > 90 dias (pela data no nome do arquivo)")
        lines.append(f"       → Remover economiza {format_bytes(old_90_size)}")
    if old_180:
        lines.append(f"    🕐 MODERADO+: {len(old_180):,} vídeos Sent/ > 180 dias")
        lines.append(f"       → Remover economiza {format_bytes(old_180_size)}")
    if old_365:
        lines.append(f"    🕐 ANTIGOS: {len(old_365):,} vídeos Sent/ > 1 ano")
        lines.append(f"       → Remover economiza {format_bytes(old_365_size)}")
    lines.append("")

    return lines


def analyze_cleanup_summary(files: list[FileEntry]) -> list[str]:
    """Final cleanup recommendations with total savings."""
    lines = []
    lines.append("┌─────────────────────────────────────────────────────────────────────────────────┐")
    lines.append("│  7. RESUMO — Opções de Limpeza                                                 │")
    lines.append("└─────────────────────────────────────────────────────────────────────────────────┘")
    lines.append("")

    video_files = [f for f in files if "WhatsApp Video" in f.folder]
    total = sum(f.size for f in video_files)
    sent = [f for f in video_files if "/Sent/" in f.path]
    received = [f for f in video_files if "/Sent/" not in f.path and "/Private/" not in f.path]
    private = [f for f in video_files if "/Private/" in f.path]

    sent_size = sum(f.size for f in sent)
    recv_size = sum(f.size for f in received)
    priv_size = sum(f.size for f in private)

    # Check forwarded (same name in both)
    recv_names = {f.basename: f for f in received}
    fwd_same_size = [(s, recv_names[s.basename]) for s in sent
                     if s.basename in recv_names and s.size == recv_names[s.basename].size]
    fwd_ss_total = sum(s.size for s, _ in fwd_same_size)

    now = datetime.now()
    old_sent_90 = sum(f.size for f in sent if f.real_date and (now - f.real_date).days > 90)
    old_sent_180 = sum(f.size for f in sent if f.real_date and (now - f.real_date).days > 180)
    old_sent_365 = sum(f.size for f in sent if f.real_date and (now - f.real_date).days > 365)
    old_recv_180 = sum(f.size for f in received if f.real_date and (now - f.real_date).days > 180)
    n_old_sent_90 = len([f for f in sent if f.real_date and (now - f.real_date).days > 90])
    n_old_sent_180 = len([f for f in sent if f.real_date and (now - f.real_date).days > 180])
    n_old_sent_365 = len([f for f in sent if f.real_date and (now - f.real_date).days > 365])

    lines.append(f"  Espaço atual: {format_bytes(total)} em {len(video_files):,} vídeos")
    lines.append(f"    Recebidos: {format_bytes(recv_size)} ({len(received):,})  |  Sent: {format_bytes(sent_size)} ({len(sent):,})  |  Private: {format_bytes(priv_size)} ({len(private):,})")
    lines.append("")

    options = [
        (
            "A",
            "Duplicatas exatas (SHA-256)",
            "450 MB (já calculado no dedup v2)",
            "Sem risco — arquivos idênticos bit a bit",
            "✅"
        ),
        (
            "B",
            f"Sent/ > 1 ano ({n_old_sent_365:,} arquivos)",
            format_bytes(old_sent_365),
            "Baixo risco — vídeos enviados há mais de 1 ano",
            "✅"
        ),
        (
            "C",
            f"Sent/ > 6 meses ({n_old_sent_180:,} arquivos)",
            format_bytes(old_sent_180),
            "Moderado — vídeos enviados há +6 meses",
            "⚠️"
        ),
        (
            "D",
            f"Sent/ > 90 dias ({n_old_sent_90:,} arquivos)",
            format_bytes(old_sent_90),
            "Moderado — vídeos enviados há +3 meses",
            "⚠️"
        ),
        (
            "E",
            f"Toda a pasta Sent/",
            format_bytes(sent_size),
            "Agressivo — remove TODOS os vídeos enviados",
            "⚠️"
        ),
        (
            "F",
            f"Tudo: Sent/ + Recebidos > 180 dias",
            format_bytes(old_sent_180 + old_recv_180),
            "Muito agressivo — tudo antigo",
            "🔴"
        ),
    ]

    lines.append(f"  {'Op':>4} {'Risk':>4}  {'Economia':>12}  {'Ação':<50}")
    lines.append("  " + "─" * 85)
    for op, desc, saving, risk, icon in options:
        lines.append(f"    {op}   {icon}   {saving:>12}  {desc}")
        lines.append(f"                          ({risk})")

    lines.append("")
    lines.append(f"  💡 Recomendação:")
    lines.append(f"     Seguro: A + B (duplicatas + Sent/>1ano) = ~{format_bytes(450*(1<<20) + old_sent_365)}")
    lines.append(f"     Moderado: A + C (duplicatas + Sent/>6m) = ~{format_bytes(450*(1<<20) + old_sent_180)}")
    lines.append(f"     Máximo: A + E (duplicatas + all Sent/) = ~{format_bytes(450*(1<<20) + sent_size)}")
    lines.append("")

    return lines


# ═════════════════════════════════════════════════════════════════════
#  Main
# ═════════════════════════════════════════════════════════════════════

def main():
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║  WhatsApp Media Disk Analyzer — ADB Toolkit                    ║")
    print("║  Análise completa de uso de disco (estilo TreeSize)            ║")
    print("╚══════════════════════════════════════════════════════════════════╝")

    # Check device
    result = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=10)
    device_lines = [l for l in result.stdout.splitlines() if "\tdevice" in l]
    if not device_lines:
        print("\nERRO: Nenhum dispositivo ADB conectado!")
        sys.exit(1)
    device_id = device_lines[0].split("\t")[0]
    print(f"\nDispositivo: {device_id}")

    t0 = time.time()

    # Scan
    all_files = scan_all_files()
    if not all_files:
        print("Nenhum arquivo encontrado!")
        return

    # Run all analyses
    report_lines = []
    report_lines.append("=" * 90)
    report_lines.append("  WHATSAPP MEDIA DISK ANALYZER — Relatório Completo")
    report_lines.append(f"  Dispositivo: {device_id}")
    report_lines.append(f"  Data: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report_lines.append("=" * 90)

    analyses = [
        analyze_folder_tree,
        analyze_video_subfolders,
        analyze_size_distribution,
        analyze_timeline,
        analyze_top_files,
        analyze_sent_deep,
        analyze_cleanup_summary,
    ]

    for func in analyses:
        section = func(all_files)
        report_lines.extend(section)
        # Print to console too
        for line in section:
            print(line)

    elapsed = time.time() - t0
    report_lines.append(f"\nAnálise concluída em {elapsed:.0f}s")
    print(f"\n  Tempo: {elapsed:.0f}s")

    # Save
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    report_path = os.path.join(OUTPUT_DIR, "wa_disk_analysis.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))
    print(f"  Relatório: {report_path}")

    # JSON summary
    video_files = [f for f in all_files if "WhatsApp Video" in f.folder]
    sent = [f for f in video_files if "/Sent/" in f.path]
    received = [f for f in video_files if "/Sent/" not in f.path and "/Private/" not in f.path]

    json_data = {
        "generated": datetime.now().isoformat(),
        "device": device_id,
        "total_files": len(all_files),
        "total_bytes": sum(f.size for f in all_files),
        "video_files": len(video_files),
        "video_bytes": sum(f.size for f in video_files),
        "sent_files": len(sent),
        "sent_bytes": sum(f.size for f in sent),
        "received_files": len(received),
        "received_bytes": sum(f.size for f in received),
        "top50_largest": [
            {
                "path": f.path,
                "size": f.size,
                "size_human": format_bytes(f.size),
                "mtime": f.mtime,
                "date": datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d"),
                "type": "SENT" if "/Sent/" in f.path else "RECV"
            }
            for f in sorted(video_files, key=lambda f: -f.size)[:50]
        ]
    }

    json_path = os.path.join(OUTPUT_DIR, "wa_disk_analysis.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False)
    print(f"  JSON:      {json_path}")


if __name__ == "__main__":
    main()
