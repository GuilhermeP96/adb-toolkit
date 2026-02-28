# ADB Toolkit — Backup, Recovery & Transfer

Ferramenta completa para **backup**, **recuperação** e **transferência** de dados entre dispositivos Android via **ADB**, com detecção e instalação automática de drivers USB.

---

## ✨ Funcionalidades

### 📱 Gerenciamento de Dispositivos
- Detecção automática de dispositivos conectados via USB
- Monitoramento em tempo real (connect/disconnect)
- Informações detalhadas: modelo, fabricante, Android, bateria, armazenamento

### 💾 Backup
- **Backup Completo** — via `adb backup` (apps + dados + shared storage)
- **Backup Seletivo** — escolha categorias:
  - 📦 Aplicativos (APKs)
  - 📷 Fotos (DCIM, Pictures)
  - 🎬 Vídeos (Movies)
  - 🎵 Músicas (Music)
  - 📄 Documentos (Documents, Download)
  - 👤 Contatos
  - 💬 SMS
- Catálogo de backups com manifesto JSON
- Progresso em tempo real com velocidade e ETA

### ♻️ Restauração
- Restauração completa ou seletiva
- Reinstalação de APKs
- Restauração de arquivos por categoria
- Detecção automática do tipo de backup

### 🔄 Transferência entre Dispositivos
- Transferência direta: **Dispositivo A → Dispositivo B**
- Seleção de categorias a transferir
- **Clone completo** — copia tudo de um dispositivo para outro
- Suporte a Wi-Fi credentials (com root)

### 🔧 Drivers USB (Windows)
- Detecção automática de drivers ADB
- Instalação do **Google USB Driver**
- Instalação do **Universal ADB Driver**
- Auto-detecção e instalação ao conectar dispositivo
- Listagem de devices com problemas no Device Manager

---

## 📋 Requisitos

- **Python** 3.9+
- **ADB** (baixado automaticamente se não encontrado)
- **Windows**: drivers USB (instalados automaticamente)
- **Linux/macOS**: geralmente não precisa de drivers adicionais

## 🚀 Instalação

```bash
# Clone o repositório
git clone <repo-url>
cd adb-toolkit

# Instale dependências
pip install -r requirements.txt
```

## ▶️ Uso

### Interface Gráfica (padrão)
```bash
# Windows
adb_toolkit.bat

# Linux/macOS
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
├── adb_toolkit.bat          # Windows launcher
├── adb_toolkit.sh           # Linux/macOS launcher
├── requirements.txt         # Dependências Python
├── config.json              # Configurações (gerado automaticamente)
├── src/
│   ├── __init__.py
│   ├── adb_core.py          # Interface ADB de baixo nível
│   ├── backup_manager.py    # Gerenciador de backup
│   ├── restore_manager.py   # Gerenciador de restauração
│   ├── transfer_manager.py  # Transferência entre dispositivos
│   ├── driver_manager.py    # Detecção/instalação de drivers
│   ├── gui.py               # Interface gráfica (customtkinter)
│   ├── config.py            # Configurações da aplicação
│   ├── log_setup.py         # Setup de logging
│   └── utils.py             # Utilitários
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
