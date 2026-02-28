# ADB Toolkit — Backup, Recovery & Transfer

Ferramenta completa para **backup**, **recuperação** e **transferência** de dados entre dispositivos Android via **ADB**, com detecção e instalação automática de drivers USB, aceleração por GPU e transferência streaming.

---

## ✨ Funcionalidades

### 📱 Gerenciamento de Dispositivos
- Detecção automática de dispositivos conectados via USB
- Monitoramento em tempo real (connect/disconnect)
- Informações detalhadas: modelo, fabricante, Android, bateria, armazenamento
- Cards de dispositivo com marca, modelo e espaço em disco

### 💾 Backup
- **Backup Completo** — via `adb backup` (apps + dados + shared storage)
- **Backup Seletivo** — escolha categorias:
  - 📦 Aplicativos (APKs + split APKs)
  - 📷 Fotos (DCIM, Pictures)
  - 🎬 Vídeos (Movies)
  - 🎵 Músicas (Music)
  - 📄 Documentos (Documents, Download)
  - 👤 Contatos (VCF)
  - 💬 SMS (JSON)
  - 💬 Apps de Mensagem (WhatsApp, Telegram, etc.)
  - 📦 Outros Apps com dados locais (detecção automática)
- **Backup Customizado** — navegue a árvore de arquivos do dispositivo
- Catálogo de backups com manifesto JSON
- Progresso em tempo real com velocidade e ETA

### ♻️ Restauração
- Restauração completa ou seletiva
- Reinstalação de APKs (inclusive split APKs)
- Restauração de arquivos por categoria
- Detecção automática do tipo de backup

### 🔄 Transferência entre Dispositivos
- Transferência direta: **Dispositivo A → Dispositivo B**
- Seleção de categorias a transferir
- **Clone completo** — diálogo dedicado para selecionar origem/destino, com info de armazenamento e filtros integrados
- **Transferência streaming** — pull → push → cleanup em lotes para minimizar uso de disco local
- **Verificação de espaço** — checa espaço livre no destino antes de iniciar
- **Filtros inteligentes**:
  - 🗑️ Ignorar Caches (cache, Cache, CACHE, preload, PreLoad, code_cache, GlideCache, OkHttp, etc.)
  - 🖼️ Ignorar Dumps/Thumbnails (.thumbnails, LOST.DIR, .Trash, thumbs.db, .dmp, etc.)
- Suporte a Wi-Fi credentials (com root)
- Detecção de apps de mensagem e apps com dados não sincronizados

### ⚡ Aceleração por GPU
- Detecção automática de GPUs: Intel (OpenCL/oneAPI), NVIDIA (CUDA), AMD (OpenCL)
- Verificação de checksums acelerada por GPU
- Multi-GPU com ranking automático (VRAM + CUs + discrete bonus)
- Toggle na barra de status para ativar/desativar em tempo real
- Fallback transparente para CPU

### 🔧 Drivers USB (Windows)
- Detecção automática de drivers ADB
- Instalação do **Google USB Driver**
- Instalação do **Universal ADB Driver**
- Drivers por chipset: Samsung, Qualcomm, MediaTek, Intel
- Auto-detecção e instalação ao conectar dispositivo

### 🛡️ Segurança & Controle
- **Cancelamento global** — o botão cancelar encerra todo o processo (backup, restore, transferência e sub-operações)
- **Bloqueio de UI** — durante operações, toda a interface fica desabilitada exceto o botão cancelar, impedindo ações conflitantes
- **Dupla confirmação** para operações destrutivas (clone)
- Elevação automática (UAC/sudo) com fallback

### ⚙️ Configurações
- Gerenciamento de ADB no PATH do sistema
- Toggles de aceleração GPU e virtualização
- Limpeza de cache do ADB
- Tema escuro nativo

---

## 📋 Requisitos

- **Python** 3.9+
- **ADB** (baixado automaticamente se não encontrado)
- **Windows**: drivers USB (instalados automaticamente)
- **Linux/macOS**: geralmente não precisa de drivers adicionais
- **GPU** (opcional): PyOpenCL para aceleração — detectado automaticamente

## 🚀 Instalação

```bash
# Clone o repositório
git clone https://github.com/GuilhermeP96/adb-toolkit.git
cd adb-toolkit

# Instale dependências
pip install -r requirements.txt
```

## ▶️ Uso

### Interface Gráfica (padrão)
```bash
# Windows (com elevação automática)
adb_toolkit.bat

# Linux/macOS (com elevação automática)
./adb_toolkit.sh

# Ou diretamente
python main.py
```

### Linha de Comando

```bash
# Listar dispositivos
python main.py --list-devices

# Backup completo de um dispositivo
python main.py --backup <SERIAL>

# Restaurar backup
python main.py --restore <SERIAL> <BACKUP_ID>

# Transferir entre dispositivos
python main.py --transfer <SERIAL_ORIGEM> <SERIAL_DESTINO>

# Listar backups existentes
python main.py --list-backups

# Instalar drivers (Windows)
python main.py --install-drivers

# Modo verbose
python main.py -v --list-devices
```

---

## 📁 Estrutura do Projeto

```
adb-toolkit/
├── main.py                  # Entry point
├── adb_toolkit.bat          # Windows launcher (UAC + fallback)
├── adb_toolkit.sh           # Linux/macOS launcher (sudo + fallback)
├── requirements.txt         # Dependências Python
├── config.json              # Configurações (gerado automaticamente)
├── src/
│   ├── __init__.py
│   ├── adb_core.py          # Interface ADB de baixo nível
│   ├── accelerator.py       # Aceleração GPU (OpenCL, CUDA, oneAPI)
│   ├── backup_manager.py    # Gerenciador de backup
│   ├── restore_manager.py   # Gerenciador de restauração
│   ├── transfer_manager.py  # Transferência streaming entre dispositivos
│   ├── driver_manager.py    # Detecção/instalação de drivers
│   ├── device_explorer.py   # Árvore de arquivos e detecção de apps
│   ├── gui.py               # Interface gráfica (customtkinter)
│   ├── config.py            # Configurações da aplicação
│   ├── log_setup.py         # Setup de logging
│   └── utils.py             # Utilitários + ADB PATH management
├── backups/                  # Backups salvos
├── transfers/                # Dados temporários de transferência
├── drivers/                  # Drivers baixados
├── logs/                     # Logs da aplicação
└── platform-tools/           # ADB (baixado automaticamente)
```

## 🔒 Notas de Segurança

- **Depuração USB** deve estar ativada no dispositivo Android
- Backup de contatos/SMS pode exigir root em Android modernos
- Drivers são instalados apenas quando executado como Administrador
- Backups são armazenados localmente — proteja a pasta de backups

## 📝 Licença

MIT License
