#!/bin/bash
# ADB Toolkit - Launcher for Linux/macOS

echo "============================================"
echo "   ADB Toolkit - Backup, Recovery & Transfer"
echo "============================================"
echo

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "[ERRO] Python3 não encontrado."
    echo "Instale: sudo apt install python3 python3-pip"
    exit 1
fi

# Check/install deps
python3 -c "import customtkinter" 2>/dev/null || {
    echo "Instalando dependências..."
    pip3 install -r requirements.txt
}

# Run
python3 main.py "$@"
