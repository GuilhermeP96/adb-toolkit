#!/bin/bash
# ADB Toolkit - Unified Launcher (elevation with fallback)
# Tries root/sudo first; falls back to normal mode with a warning.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ---- Colors ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

ELEVATED=false

# ---- Check if already root ----
if [ "$(id -u)" -eq 0 ]; then
    ELEVATED=true
fi

# ---- If not root, try elevation ----
if [ "$ELEVATED" = false ] && [ "${1}" != "--no-elevate" ]; then
    echo -e "${CYAN}============================================${NC}"
    echo -e "${CYAN}   ADB Toolkit - Solicitando Elevação${NC}"
    echo -e "${CYAN}============================================${NC}"
    echo

    ELEVATE_CMD=""
    if command -v pkexec &>/dev/null && [ -n "$DISPLAY" ]; then
        ELEVATE_CMD="pkexec --disable-internal-agent"
    elif command -v sudo &>/dev/null; then
        ELEVATE_CMD="sudo"
    elif command -v doas &>/dev/null; then
        ELEVATE_CMD="doas"
    fi

    if [ -n "$ELEVATE_CMD" ]; then
        echo -e "${YELLOW}Solicitando privilégios de root...${NC}"
        exec $ELEVATE_CMD "$0" "$@" 2>/dev/null
        # If exec fails, we continue below in fallback mode
    fi

    # ---- Fallback: no elevation ----
    echo
    echo -e "${YELLOW}############################################${NC}"
    echo -e "${YELLOW}# AVISO: Executando SEM privilégios root   #${NC}"
    echo -e "${YELLOW}# - Instalação de drivers indisponível     #${NC}"
    echo -e "${YELLOW}# - Regras udev não serão configuradas     #${NC}"
    echo -e "${YELLOW}# - Adicionar ADB ao PATH indisponível     #${NC}"
    echo -e "${YELLOW}############################################${NC}"
    echo
else
    ELEVATED=true
fi

if [ "$ELEVATED" = true ]; then
    echo -e "${CYAN}============================================${NC}"
    echo -e "${CYAN}   ADB Toolkit - Modo Administrador${NC}"
    echo -e "${GREEN}   Privilégios elevados: ATIVO (uid=$(id -u))${NC}"
    echo -e "${CYAN}============================================${NC}"
    echo
fi

# ---- Check Python ----
PYTHON=""
if command -v python3 &>/dev/null; then
    PYTHON="python3"
elif command -v python &>/dev/null; then
    PYTHON="python"
else
    echo -e "${RED}[ERRO] Python3 não encontrado.${NC}"
    echo "Instale: sudo apt install python3 python3-pip  (Debian/Ubuntu)"
    echo "         sudo dnf install python3 python3-pip  (Fedora)"
    echo "         brew install python3                   (macOS)"
    exit 1
fi

echo -e "Python encontrado: $($PYTHON --version)"
echo

# ---- Determine pip ----
PIP=""
if command -v pip3 &>/dev/null; then
    PIP="pip3"
elif command -v pip &>/dev/null; then
    PIP="pip"
else
    PIP="$PYTHON -m pip"
fi

# ---- Check/install dependencies ----
if ! $PYTHON -c "import customtkinter" 2>/dev/null; then
    echo -e "${YELLOW}Instalando dependências...${NC}"
    $PIP install -r "$SCRIPT_DIR/requirements.txt"
    echo
fi

# ---- Set up udev rules for ADB (Linux only, requires root) ----
if [ "$ELEVATED" = true ] && [ "$(uname)" = "Linux" ]; then
    UDEV_RULE="/etc/udev/rules.d/51-android.rules"
    if [ ! -f "$UDEV_RULE" ]; then
        echo -e "${YELLOW}Configurando regras udev para dispositivos Android...${NC}"
        cat > "$UDEV_RULE" << 'UDEV_EOF'
# Google
SUBSYSTEM=="usb", ATTR{idVendor}=="18d1", MODE="0666", GROUP="plugdev"
# Samsung
SUBSYSTEM=="usb", ATTR{idVendor}=="04e8", MODE="0666", GROUP="plugdev"
# Xiaomi
SUBSYSTEM=="usb", ATTR{idVendor}=="2717", MODE="0666", GROUP="plugdev"
# Motorola
SUBSYSTEM=="usb", ATTR{idVendor}=="22b8", MODE="0666", GROUP="plugdev"
# HTC
SUBSYSTEM=="usb", ATTR{idVendor}=="0bb4", MODE="0666", GROUP="plugdev"
# Huawei
SUBSYSTEM=="usb", ATTR{idVendor}=="12d1", MODE="0666", GROUP="plugdev"
# OnePlus
SUBSYSTEM=="usb", ATTR{idVendor}=="2a70", MODE="0666", GROUP="plugdev"
# LG
SUBSYSTEM=="usb", ATTR{idVendor}=="1004", MODE="0666", GROUP="plugdev"
# Sony
SUBSYSTEM=="usb", ATTR{idVendor}=="0fce", MODE="0666", GROUP="plugdev"
# Qualcomm
SUBSYSTEM=="usb", ATTR{idVendor}=="05c6", MODE="0666", GROUP="plugdev"
# MediaTek
SUBSYSTEM=="usb", ATTR{idVendor}=="0e8d", MODE="0666", GROUP="plugdev"
# Asus
SUBSYSTEM=="usb", ATTR{idVendor}=="0b05", MODE="0666", GROUP="plugdev"
# ZTE
SUBSYSTEM=="usb", ATTR{idVendor}=="19d2", MODE="0666", GROUP="plugdev"
# Meizu
SUBSYSTEM=="usb", ATTR{idVendor}=="2a45", MODE="0666", GROUP="plugdev"
UDEV_EOF
        chmod 644 "$UDEV_RULE"
        udevadm control --reload-rules 2>/dev/null || true
        udevadm trigger 2>/dev/null || true
        echo -e "${GREEN}Regras udev instaladas em $UDEV_RULE${NC}"
        echo
    fi

    # Ensure current user is in plugdev group
    REAL_USER="${SUDO_USER:-$USER}"
    if ! groups "$REAL_USER" 2>/dev/null | grep -q plugdev; then
        if getent group plugdev &>/dev/null; then
            usermod -aG plugdev "$REAL_USER" 2>/dev/null || true
            echo -e "${YELLOW}Usuário '$REAL_USER' adicionado ao grupo plugdev${NC}"
        fi
    fi
fi

# ---- Filter out internal flags ----
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --no-elevate|--elevated) ;;
        *) ARGS+=("$arg") ;;
    esac
done

# ---- Run application ----
echo -e "${CYAN}Iniciando ADB Toolkit...${NC}"
echo
$PYTHON main.py "${ARGS[@]}"

EXIT_CODE=$?
if [ $EXIT_CODE -ne 0 ]; then
    echo
    echo -e "${RED}[ERRO] A aplicação encerrou com erro (código: $EXIT_CODE).${NC}"
fi

exit $EXIT_CODE
