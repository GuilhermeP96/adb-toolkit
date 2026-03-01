# ADB Toolkit — Android & iOS Device Manager

Ferramenta completa para **backup**, **recuperação**, **transferência**, **limpeza**, **diagnóstico** e **controle remoto** de dispositivos Android e iOS, com interface gráfica moderna, suporte a **transferência cross-platform Android ↔ iOS**, **Android Agent companion app** com protocolo direto WiFi, **iOS Agent**, aceleração por GPU e i18n (PT-BR / EN).

---

## ✨ Funcionalidades

### 📱 Gerenciamento de Dispositivos
- Detecção automática de dispositivos Android (USB) e iOS
- Monitoramento em tempo real (connect/disconnect)
- Informações detalhadas: modelo, fabricante, Android/iOS version, bateria, armazenamento
- Cards de dispositivo com marca, modelo, espaço em disco e indicador de protocolo
- **Protocolo direto WiFi** — dispositivos pareados aparecem na lista mesmo sem ADB conectado (📡)
- Suporte multi-dispositivo simultâneo

### 🧹 Limpeza (Cleanup)
- **Deep Clean** — análise profunda de espaço desperdiçado
- **Dedup Cleaner** — detecção e remoção de arquivos duplicados
- Limpeza de caches por app
- Identificação de arquivos grandes e vazios
- Relatório antes de executar com confirmação

### 🧰 Toolbox
- Gerenciamento de apps (instalar, desinstalar, extrair APK)
- Informações do sistema (props, services, processos)
- Reinicialização (normal, recovery, bootloader, fastboot)
- Captura de screenshots e screen recording
- Shell interativo via ADB
- Logcat viewer com filtros

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
  - 💬 Apps de Mensagem (WhatsApp, Telegram, etc.) — com checkbox "incluir mídias"
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
- **Clone completo** — diálogo dedicado com info de armazenamento e filtros
- **Transferência streaming** — pull → push → cleanup em lotes para minimizar uso de disco local
- **Verificação de espaço** — checa espaço livre no destino antes de iniciar
- **Filtros inteligentes**: ignorar caches, thumbnails, LOST.DIR, etc.
- Detecção de apps de mensagem e apps com dados não sincronizados

### 🔀 Transferência Cross-Platform (Android ↔ iOS)
- **Detecção automática** de dispositivos iOS via `pymobiledevice3`
- **Dados transferíveis**: Fotos (HEIC → JPEG), Vídeos, Músicas, Documentos, Contatos (VCF), SMS, Calendário (ICS), WhatsApp (mídias)
- Guia para migração oficial do WhatsApp (conversas)
- Limitações transparentes

### 🤖 Android Agent (Companion App)
Aplicativo Android nativo com **5 abas** de navegação inferior:

| Aba | Recursos |
|-----|----------|
| **Dashboard** | Start/stop do serviço, info do dispositivo (bateria, storage, RAM), ações rápidas (screenshot, gravar tela, exportar contatos/SMS), log de conexões |
| **Arquivos** | Navegador de arquivos com path bar, diretórios rápidos (sdcard, DCIM, Download, Documents), criar pastas |
| **Apps** | Lista de apps com busca e filtro (Todos/Usuário/Sistema), menu de contexto (info, abrir, extrair APK, desinstalar, force stop, clear data) |
| **Terminal** | Execução de comandos shell, chips de comandos rápidos (getprop, df, ps, top, dumpsys, logcat), histórico |
| **Configurações** | Segurança/pareamento com biometria, permissões dinâmicas, Python runtime, token de autenticação, sobre |

**Arquitetura do Agent:**
- HTTP API (NanoHTTPD, porta 15555) com 9 endpoints: Device, Files, Apps, Contacts, SMS, Shell, Python, Peer, Orchestrator
- TCP transfer server (porta 15556) para transferência de arquivos de alta velocidade
- Segurança: token auth + pareamento ECDH + HMAC-SHA256 + verificação biométrica
- Foreground service com notificação persistente
- Suporte a Python on-device (Chaquopy)

### 🍎 iOS Agent
Aplicativo iOS em Swift com servidor HTTP embarcado:
- API para Device info, Files, Photos, Contacts, Peer discovery
- Transfer server para operações de alta velocidade
- Pairing manager com segurança P2P

### 📡 Protocolo Direto WiFi
- Conexão ao Agent via WiFi **sem necessidade de ADB** (após pareamento inicial)
- Auto-registro do IP WiFi ao conectar via ADB
- Persistência de dispositivos pareados em `data/direct_devices.json`
- Ping/refresh de dispositivos diretos
- Indicador de protocolo na GUI: `USB/ADB`, `WiFi Direct`, ou `USB/ADB + 📡WiFi`
- Permite desconectar ADB mantendo o controle via protocolo direto

### ⚡ Aceleração por GPU
- Detecção automática: Intel (OpenCL/oneAPI), NVIDIA (CUDA), AMD (OpenCL)
- Verificação de checksums acelerada por GPU
- Multi-GPU com ranking automático (VRAM + CUs + discrete bonus)
- Toggle na barra de status

### 🔧 Drivers USB (Windows)
- Detecção automática de drivers ADB
- Instalação do Google USB Driver e Universal ADB Driver
- Drivers por chipset: Samsung, Qualcomm, MediaTek, Intel
- Auto-detecção e instalação ao conectar dispositivo

### 🌐 Internacionalização (i18n)
- **Português (PT-BR)** e **English (EN)**
- Troca de idioma em tempo real nas configurações
- Todas as strings da GUI traduzidas

### ⚙️ Configurações
- ADB no PATH do sistema
- Toggles de aceleração GPU e virtualização
- Limpeza de cache do ADB
- Seleção de idioma
- Tema escuro nativo

---

## 🖥️ Interface — 10 Abas

| # | Aba | Descrição |
|---|-----|-----------|
| 1 | **Dispositivos** | Lista de dispositivos ADB + WiFi Direct + iOS, cards com detalhes |
| 2 | **Limpeza** | Deep clean, dedup, cache, análise de espaço |
| 3 | **Toolbox** | Ferramentas ADB: apps, sistema, shell, reboot, screenshot |
| 4 | **Backup** | Backup completo, seletivo ou customizado |
| 5 | **Restauração** | Restaurar backups por categoria |
| 6 | **Transferência** | Transfer direta, clone, streaming, cross-platform |
| 7 | **Drivers** | Gerenciamento de drivers USB (Windows) |
| 8 | **Agent** | Instalação, build, controle do Android Agent |
| 9 | **iOS** | Gerenciamento de dispositivos iOS |
| 10 | **Configurações** | ADB PATH, GPU, idioma, tema |

---

## 📋 Requisitos

- **Python** 3.9+
- **ADB** (baixado automaticamente se não encontrado)
- **Windows**: drivers USB (instalados automaticamente)
- **Linux/macOS**: geralmente não precisa de drivers adicionais

### Opcionais
- **GPU**: PyOpenCL para aceleração de checksums
- **iOS**: `pymobiledevice3` + `pillow-heif` para cross-platform
- **Agent Build**: JDK 17 + Android SDK (auto-instalados via DependencyManager)
- **cryptography**: para pareamento seguro ECDH + HMAC com o Agent

## 🚀 Instalação

```bash
# Clone o repositório
git clone https://github.com/GuilhermeP96/adb-toolkit.git
cd adb-toolkit

# Instale dependências
pip install -r requirements.txt

# (Opcional) Suporte iOS
pip install pymobiledevice3 pillow-heif

# (Opcional) Pareamento seguro com Agent
pip install cryptography
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

# Backup completo
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
├── main.py                     # Entry point
├── adb_toolkit.bat             # Windows launcher (UAC)
├── adb_toolkit.sh              # Linux/macOS launcher (sudo)
├── requirements.txt            # Dependências Python
├── config.json                 # Configurações (gerado automaticamente)
│
├── src/                        # Módulos Python
│   ├── gui.py                  # Interface gráfica (customtkinter, 10 abas)
│   ├── i18n.py                 # Internacionalização (PT-BR / EN)
│   ├── adb_core.py             # Interface ADB de baixo nível
│   ├── adb_base.py             # Base ADB utilities
│   ├── adb_adapter.py          # Adaptador ADB → DeviceInterface
│   ├── accelerator.py          # Aceleração GPU (OpenCL, CUDA, oneAPI)
│   ├── backup_manager.py       # Gerenciador de backup
│   ├── restore_manager.py      # Gerenciador de restauração
│   ├── transfer_manager.py     # Transferência streaming entre dispositivos
│   ├── cleanup_manager.py      # Limpeza e análise de espaço
│   ├── deep_cleaner.py         # Deep clean (cache, duplicados, temp)
│   ├── dedup_cleaner.py        # Detecção de arquivos duplicados
│   ├── toolbox_manager.py      # Ferramentas ADB (apps, sistema, shell)
│   ├── device_interface.py     # Interface abstrata multi-plataforma
│   ├── device_explorer.py      # Árvore de arquivos e detecção de apps
│   ├── driver_manager.py       # Detecção/instalação de drivers USB
│   ├── agent_manager.py        # Ciclo de vida do Android Agent + protocolo direto
│   ├── agent_bridge.py         # Operações aceleradas via Agent API
│   ├── companion_client.py     # Cliente HTTP/TCP para comunicação com Agent
│   ├── ios_manager.py          # Gerenciamento do iOS Agent
│   ├── ios_bridge.py           # Operações via iOS Agent
│   ├── ios_core.py             # Interface iOS via pymobiledevice3
│   ├── cross_transfer.py       # Orquestrador cross-platform Android ↔ iOS
│   ├── format_converter.py     # Conversores: VCF, SMS, ICS, HEIC
│   ├── whatsapp_transfer.py    # Transferência de mídias do WhatsApp
│   ├── config.py               # Configurações da aplicação
│   ├── log_setup.py            # Setup de logging
│   └── utils.py                # Utilitários + ADB PATH management
│
├── agent/                      # Android Agent (Kotlin)
│   ├── app/src/main/java/.../
│   │   ├── AgentApp.kt         # Application class
│   │   ├── ui/                 # 5 Fragments + MainActivity (bottom nav)
│   │   ├── api/                # 9 API handlers (Device, Files, Apps, etc.)
│   │   ├── server/             # NanoHTTPD server + API router
│   │   ├── services/           # Foreground service, boot receiver, accessibility
│   │   ├── security/           # BiometricGate, PairingManager, ECDH
│   │   ├── transfer/           # TCP transfer server
│   │   └── python/             # Python runtime (Chaquopy)
│   └── app/build.gradle.kts    # Build config (compileSdk 35, minSdk 26)
│
├── agent-ios/                  # iOS Agent (Swift)
│   ├── Sources/                # 14 Swift files
│   │   ├── AgentIOSApp.swift   # App entry point
│   │   ├── HTTPServer.swift    # HTTP server
│   │   ├── ApiRouter.swift     # API routing
│   │   ├── *Handler.swift      # API handlers
│   │   └── ...
│   └── Package.swift           # Swift Package definition
│
├── locales/                    # Traduções
│   ├── pt_BR.json              # Português (Brasil)
│   └── en.json                 # English
│
├── data/                       # Dados persistentes
│   └── direct_devices.json     # Dispositivos WiFi pareados
├── backups/                    # Backups salvos
├── transfers/                  # Dados temporários de transferência
├── drivers/                    # Drivers baixados
├── logs/                       # Logs da aplicação
└── platform-tools/             # ADB (baixado automaticamente)
```

---

## 🔒 Notas de Segurança

- **Depuração USB** deve estar ativada no dispositivo Android
- **iOS**: o iPhone deve estar desbloqueado e confiar no computador
- **Agent**: pareamento com verificação biométrica + código de confirmação visual
- **Protocolo direto**: token de autenticação + HMAC-SHA256 para cada request
- Backup de contatos/SMS pode exigir root em Android modernos
- Drivers são instalados apenas quando executado como Administrador
- Backups são armazenados localmente — proteja a pasta de backups
- **WhatsApp**: transferência cross-platform copia apenas mídias; para conversas, use a migração oficial

## 📝 Licença

MIT License
