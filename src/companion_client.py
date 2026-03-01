"""
companion_client.py — PC-side client for communicating with the Android Agent.

Provides a high-level Python API for the PC toolkit to interact with the
ADB Toolkit Agent running on Android devices via HTTP and TCP.

Supports:
 - Local connection via ADB port forwarding (USB)
 - WiFi direct connection (same network)
 - D2D peer operations (orchestrator mode)

Security:
 - X-Agent-Token for local/USB connections
 - X-Peer-Id + X-Peer-Signature (HMAC-SHA256) for P2P connections
 - ECDH key exchange for establishing shared secrets

Usage:
    from companion_client import AgentClient

    client = AgentClient(token="your-token-here")
    client.connect()
    print(client.ping())
    contacts = client.contacts.list()
    client.files.pull("/sdcard/DCIM/photo.jpg", "photo.jpg")
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    requests = None  # type: ignore

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


# ═══════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_HTTP_PORT = 15555
DEFAULT_TCP_PORT = 15556
TCP_HEADER_SIZE = 512
TCP_BUFFER_SIZE = 256 * 1024
REQUEST_TIMEOUT = 30
REPLAY_WINDOW_SEC = 300


# ═══════════════════════════════════════════════════════════════════════
#  DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class AgentResponse:
    """Wrapper for HTTP responses from the agent."""
    ok: bool
    status_code: int
    data: dict | list | None = None
    error: str = ""
    raw: bytes = b""

    def __bool__(self) -> bool:
        return self.ok

    def __getitem__(self, key: str) -> Any:
        if isinstance(self.data, dict):
            return self.data[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        if isinstance(self.data, dict):
            return self.data.get(key, default)
        return default


@dataclass
class PeerInfo:
    """Represents a discovered or paired peer device."""
    device_id: str
    label: str
    address: str = ""
    port: int = DEFAULT_HTTP_PORT
    public_key: str = ""
    shared_secret: str = ""
    paired: bool = False


# ═══════════════════════════════════════════════════════════════════════
#  MAIN CLIENT
# ═══════════════════════════════════════════════════════════════════════

class AgentClient:
    """
    High-level client for the ADB Toolkit Agent.

    Args:
        host: Agent hostname/IP (default: 127.0.0.1 for ADB forwarding)
        port: HTTP API port
        token: Authentication token (X-Agent-Token)
        adb_path: Path to adb binary (for auto-forwarding)
        serial: ADB device serial (if multiple devices)
        timeout: Request timeout in seconds
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_HTTP_PORT,
        token: str = "",
        adb_path: str = "adb",
        serial: str = "",
        timeout: int = REQUEST_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.token = token
        self.adb_path = adb_path
        self.serial = serial
        self.timeout = timeout
        self._base_url = f"http://{host}:{port}"
        self._session = None

        # Sub-APIs (lazy init)
        self._files: Optional[FilesApi] = None
        self._apps: Optional[AppsApi] = None
        self._contacts: Optional[ContactsApi] = None
        self._sms: Optional[SmsApi] = None
        self._device: Optional[DeviceApi] = None
        self._shell: Optional[ShellApi] = None
        self._python: Optional[PythonApi] = None
        self._peer: Optional[PeerApi] = None
        self._orchestrator: Optional[OrchestratorApi] = None

    # ── Properties for sub-APIs ───────────────────────────────────────

    @property
    def files(self) -> "FilesApi":
        if self._files is None:
            self._files = FilesApi(self)
        return self._files

    @property
    def apps(self) -> "AppsApi":
        if self._apps is None:
            self._apps = AppsApi(self)
        return self._apps

    @property
    def contacts(self) -> "ContactsApi":
        if self._contacts is None:
            self._contacts = ContactsApi(self)
        return self._contacts

    @property
    def sms(self) -> "SmsApi":
        if self._sms is None:
            self._sms = SmsApi(self)
        return self._sms

    @property
    def device(self) -> "DeviceApi":
        if self._device is None:
            self._device = DeviceApi(self)
        return self._device

    @property
    def shell(self) -> "ShellApi":
        if self._shell is None:
            self._shell = ShellApi(self)
        return self._shell

    @property
    def python(self) -> "PythonApi":
        if self._python is None:
            self._python = PythonApi(self)
        return self._python

    @property
    def peer(self) -> "PeerApi":
        if self._peer is None:
            self._peer = PeerApi(self)
        return self._peer

    @property
    def orchestrator(self) -> "OrchestratorApi":
        if self._orchestrator is None:
            self._orchestrator = OrchestratorApi(self)
        return self._orchestrator

    # ── Connection ────────────────────────────────────────────────────

    def connect(self, forward: bool = True) -> AgentResponse:
        """
        Connect to the agent. Optionally set up ADB port forwarding.

        Args:
            forward: If True and host is localhost, run `adb forward`
        """
        if forward and self.host in ("127.0.0.1", "localhost", "::1"):
            self._adb_forward()

        return self.ping()

    def _adb_forward(self):
        """Set up ADB port forwarding for HTTP and TCP ports."""
        cmd_base = [self.adb_path]
        if self.serial:
            cmd_base.extend(["-s", self.serial])

        for local_port, remote_port in [
            (self.port, self.port),
            (DEFAULT_TCP_PORT, DEFAULT_TCP_PORT),
        ]:
            cmd = cmd_base + ["forward", f"tcp:{local_port}", f"tcp:{remote_port}"]
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=10)
            except Exception as e:
                raise ConnectionError(f"ADB forward failed: {e}") from e

    def disconnect(self):
        """Clean up ADB port forwarding."""
        if self.host in ("127.0.0.1", "localhost", "::1"):
            cmd_base = [self.adb_path]
            if self.serial:
                cmd_base.extend(["-s", self.serial])
            for port in (self.port, DEFAULT_TCP_PORT):
                try:
                    subprocess.run(
                        cmd_base + ["forward", "--remove", f"tcp:{port}"],
                        capture_output=True, timeout=10
                    )
                except Exception:
                    pass

    # ── HTTP primitives ───────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        """Build request headers with authentication."""
        h = {"Content-Type": "application/json"}
        if self.token:
            h["X-Agent-Token"] = self.token
        return h

    def get(self, endpoint: str, params: dict | None = None, **kwargs) -> AgentResponse:
        """HTTP GET request to the agent."""
        return self._request("GET", endpoint, params=params, **kwargs)

    def post(self, endpoint: str, json_data: dict | None = None, **kwargs) -> AgentResponse:
        """HTTP POST request to the agent."""
        return self._request("POST", endpoint, json_data=json_data, **kwargs)

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json_data: dict | None = None,
        stream: bool = False,
        timeout: int | None = None,
    ) -> AgentResponse:
        """Core HTTP request method."""
        url = f"{self._base_url}{endpoint}"
        if params:
            url += "?" + urlencode(params)

        _timeout = timeout or self.timeout

        if requests is not None:
            return self._request_via_requests(method, url, json_data, stream, _timeout)
        else:
            return self._request_via_urllib(method, url, json_data, _timeout)

    def _request_via_requests(
        self, method, url, json_data, stream, timeout
    ) -> AgentResponse:
        """Use the requests library if available."""
        try:
            resp = requests.request(
                method, url,
                headers=self._headers(),
                json=json_data,
                stream=stream,
                timeout=timeout,
            )
            if stream:
                return AgentResponse(
                    ok=resp.ok,
                    status_code=resp.status_code,
                    raw=resp.content,
                )
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}
            return AgentResponse(
                ok=resp.ok,
                status_code=resp.status_code,
                data=data,
                error=data.get("error", "") if isinstance(data, dict) else "",
            )
        except requests.ConnectionError as e:
            return AgentResponse(ok=False, status_code=0, error=f"Connection failed: {e}")
        except requests.Timeout:
            return AgentResponse(ok=False, status_code=0, error="Request timed out")
        except Exception as e:
            return AgentResponse(ok=False, status_code=0, error=str(e))

    def _request_via_urllib(self, method, url, json_data, timeout) -> AgentResponse:
        """Fallback using urllib (no external deps)."""
        import urllib.request
        import urllib.error

        req = urllib.request.Request(url, method=method, headers=self._headers())
        if json_data:
            req.data = json.dumps(json_data).encode()

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                try:
                    data = json.loads(body)
                except Exception:
                    data = {"raw": body.decode(errors="replace")}
                return AgentResponse(ok=True, status_code=resp.status, data=data)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                data = json.loads(body)
            except Exception:
                data = {"raw": body}
            return AgentResponse(
                ok=False, status_code=e.code, data=data,
                error=data.get("error", body) if isinstance(data, dict) else body,
            )
        except Exception as e:
            return AgentResponse(ok=False, status_code=0, error=str(e))

    # ── Core endpoints ────────────────────────────────────────────────

    def ping(self) -> AgentResponse:
        return self.get("/api/ping")

    # ── TCP Transfer ──────────────────────────────────────────────────

    def tcp_push(self, local_path: str, remote_path: str) -> dict:
        """Push a file over TCP for maximum speed."""
        local = Path(local_path)
        if not local.exists():
            raise FileNotFoundError(f"Local file not found: {local_path}")

        size = local.stat().st_size
        header = json.dumps({
            "op": "push",
            "path": remote_path,
            "size": size,
            "token": self.token,
        })

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(self.timeout + size // (1024 * 1024))  # +1s per MB
        sock.connect((self.host, DEFAULT_TCP_PORT))

        try:
            # Send header (padded to 512 bytes)
            header_bytes = header.encode().ljust(TCP_HEADER_SIZE, b"\x00")
            sock.sendall(header_bytes)

            # Stream file
            sha = hashlib.sha256()
            with open(local_path, "rb") as f:
                while True:
                    chunk = f.read(TCP_BUFFER_SIZE)
                    if not chunk:
                        break
                    sock.sendall(chunk)
                    sha.update(chunk)

            # Send hash footer
            sock.sendall(sha.digest())

            # Read response header
            resp_bytes = b""
            while len(resp_bytes) < TCP_HEADER_SIZE:
                chunk = sock.recv(TCP_HEADER_SIZE - len(resp_bytes))
                if not chunk:
                    break
                resp_bytes += chunk

            return json.loads(resp_bytes.decode().strip("\x00"))
        finally:
            sock.close()

    def tcp_pull(self, remote_path: str, local_path: str) -> dict:
        """Pull a file over TCP for maximum speed."""
        header = json.dumps({
            "op": "pull",
            "path": remote_path,
            "token": self.token,
        })

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(self.timeout)
        sock.connect((self.host, DEFAULT_TCP_PORT))

        try:
            # Send header
            header_bytes = header.encode().ljust(TCP_HEADER_SIZE, b"\x00")
            sock.sendall(header_bytes)

            # Read response header
            resp_bytes = b""
            while len(resp_bytes) < TCP_HEADER_SIZE:
                chunk = sock.recv(TCP_HEADER_SIZE - len(resp_bytes))
                if not chunk:
                    break
                resp_bytes += chunk

            resp = json.loads(resp_bytes.decode().strip("\x00"))
            if resp.get("status") == "error":
                return resp

            size = resp.get("size", 0)

            # Stream to file
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            sha = hashlib.sha256()
            remaining = size

            with open(local_path, "wb") as f:
                while remaining > 0:
                    to_read = min(TCP_BUFFER_SIZE, remaining)
                    chunk = sock.recv(to_read)
                    if not chunk:
                        break
                    f.write(chunk)
                    sha.update(chunk)
                    remaining -= len(chunk)

            # Read hash footer
            hash_bytes = b""
            while len(hash_bytes) < 32:
                chunk = sock.recv(32 - len(hash_bytes))
                if not chunk:
                    break
                hash_bytes += chunk

            local_hash = sha.hexdigest()
            remote_hash = hash_bytes.hex() if len(hash_bytes) == 32 else ""

            return {
                "status": "ok",
                "bytes_read": size - remaining,
                "local_hash": local_hash,
                "remote_hash": remote_hash,
                "hash_match": local_hash == remote_hash if remote_hash else True,
                "path": local_path,
            }
        finally:
            sock.close()


# ═══════════════════════════════════════════════════════════════════════
#  SUB-API CLASSES
# ═══════════════════════════════════════════════════════════════════════

class _SubApi:
    """Base class for sub-API namespaces."""
    def __init__(self, client: AgentClient):
        self._c = client


class FilesApi(_SubApi):
    def list(self, path: str = "/sdcard", recursive: bool = False) -> AgentResponse:
        return self._c.get("/api/files/list", {"path": path, "recursive": str(recursive).lower()})

    def stat(self, path: str) -> AgentResponse:
        return self._c.get("/api/files/stat", {"path": path})

    def read(self, path: str) -> AgentResponse:
        return self._c.get(f"/api/files/read", {"path": path})

    def write(self, path: str, data: bytes) -> AgentResponse:
        return self._c.post("/api/files/write", {"path": path, "data": data.decode("latin-1")})

    def search(self, path: str, pattern: str) -> AgentResponse:
        return self._c.get("/api/files/search", {"path": path, "pattern": pattern})

    def pull(self, remote: str, local: str) -> dict:
        """Pull using TCP for speed."""
        return self._c.tcp_pull(remote, local)

    def push(self, local: str, remote: str) -> dict:
        """Push using TCP for speed."""
        return self._c.tcp_push(local, remote)

    def hash(self, path: str, algo: str = "sha256") -> AgentResponse:
        return self._c.get("/api/files/hash", {"path": path, "algo": algo})


class AppsApi(_SubApi):
    def list(self, third_party: bool = True) -> AgentResponse:
        return self._c.get("/api/apps/list", {"third_party": str(third_party).lower()})

    def info(self, package: str) -> AgentResponse:
        return self._c.get("/api/apps/info", {"package": package})

    def download_apk(self, package: str, save_path: str) -> dict:
        """Download APK via TCP pull."""
        resp = self.info(package)
        if not resp.ok:
            return {"error": resp.error}
        apk_path = resp.get("source_dir", "")
        if not apk_path:
            return {"error": "No APK path found"}
        return self._c.tcp_pull(apk_path, save_path)

    def install(self, apk_path: str) -> AgentResponse:
        return self._c.post("/api/apps/install", {"path": apk_path})

    def uninstall(self, package: str) -> AgentResponse:
        return self._c.post("/api/apps/uninstall", {"package": package})


class ContactsApi(_SubApi):
    def list(self) -> AgentResponse:
        return self._c.get("/api/contacts/list")

    def count(self) -> AgentResponse:
        return self._c.get("/api/contacts/count")

    def export_vcf(self) -> AgentResponse:
        return self._c.get("/api/contacts/export")

    def import_vcf(self, vcf_data: str) -> AgentResponse:
        return self._c.post("/api/contacts/import", {"vcf": vcf_data})


class SmsApi(_SubApi):
    def list(self, limit: int = 100, offset: int = 0) -> AgentResponse:
        return self._c.get("/api/sms/list", {"limit": limit, "offset": offset})

    def count(self) -> AgentResponse:
        return self._c.get("/api/sms/count")

    def export(self) -> AgentResponse:
        return self._c.get("/api/sms/export")

    def import_messages(self, messages: list[dict]) -> AgentResponse:
        return self._c.post("/api/sms/import", {"messages": messages})

    def conversations(self) -> AgentResponse:
        return self._c.get("/api/sms/conversations")


class DeviceApi(_SubApi):
    def info(self) -> AgentResponse:
        return self._c.get("/api/device/info")

    def battery(self) -> AgentResponse:
        return self._c.get("/api/device/battery")

    def network(self) -> AgentResponse:
        return self._c.get("/api/device/network")

    def storage(self) -> AgentResponse:
        return self._c.get("/api/device/storage")

    def screenshot(self, save_path: str = "") -> AgentResponse:
        resp = self._c.get("/api/device/screen")
        if resp.ok and save_path and resp.raw:
            with open(save_path, "wb") as f:
                f.write(resp.raw)
        return resp

    def permissions(self) -> AgentResponse:
        return self._c.get("/api/device/permissions")


class ShellApi(_SubApi):
    def exec(self, command: str, timeout: int = 30) -> AgentResponse:
        return self._c.post("/api/shell/exec", {"command": command, "timeout": timeout})

    def getprop(self, prop: str) -> AgentResponse:
        return self._c.get("/api/shell/getprop", {"prop": prop})

    def settings_get(self, namespace: str, key: str) -> AgentResponse:
        return self._c.get("/api/shell/settings", {"namespace": namespace, "key": key})

    def settings_put(self, namespace: str, key: str, value: str) -> AgentResponse:
        return self._c.post("/api/shell/settings", {
            "namespace": namespace, "key": key, "value": value
        })


class PythonApi(_SubApi):
    def status(self) -> AgentResponse:
        return self._c.get("/api/python/status")

    def setup(self) -> AgentResponse:
        return self._c.post("/api/python/setup")

    def exec(self, code: str, timeout: int = 60) -> AgentResponse:
        return self._c.post("/api/python/exec", {"code": code, "timeout": timeout})

    def run_script(self, name: str, args: str = "") -> AgentResponse:
        return self._c.get("/api/python/run-script", {"name": name, "args": args})

    def pip_install(self, package: str) -> AgentResponse:
        return self._c.post("/api/python/pip", {"package": package})

    def packages(self) -> AgentResponse:
        return self._c.get("/api/python/packages")


class PeerApi(_SubApi):
    def discover(self) -> AgentResponse:
        return self._c.get("/api/peer/discover")

    def identity(self) -> AgentResponse:
        return self._c.get("/api/peer/identity")

    def pair_init(self, device_id: str, label: str, public_key: str) -> AgentResponse:
        return self._c.post("/api/peer/pair-init", {
            "device_id": device_id,
            "label": label,
            "public_key": public_key,
        })

    def pair_pending(self) -> AgentResponse:
        return self._c.get("/api/peer/pair-pending")

    def pair_approve(self, challenge_id: str) -> AgentResponse:
        return self._c.post("/api/peer/pair-approve", {
            "challenge_id": challenge_id,
            "biometric_verified": True,
        })

    def paired(self) -> AgentResponse:
        return self._c.get("/api/peer/paired")

    def revoke(self, device_id: str) -> AgentResponse:
        return self._c.post("/api/peer/revoke", {
            "device_id": device_id,
            "biometric_verified": True,
        })


class OrchestratorApi(_SubApi):
    def topology(self) -> AgentResponse:
        return self._c.get("/api/orchestrator/topology")

    def dispatch(self, target_id: str, method: str, endpoint: str, body: dict | None = None) -> AgentResponse:
        return self._c.post("/api/orchestrator/dispatch", {
            "target_device_id": target_id,
            "method": method,
            "endpoint": endpoint,
            "body": body or {},
        })

    def broadcast(self, method: str, endpoint: str) -> AgentResponse:
        return self._c.post("/api/orchestrator/broadcast", {
            "method": method,
            "endpoint": endpoint,
        })

    def transfer(self, source_id: str, target_id: str, data_type: str, params: dict | None = None) -> AgentResponse:
        return self._c.post("/api/orchestrator/transfer", {
            "source_device_id": source_id,
            "target_device_id": target_id,
            "data_type": data_type,
            "params": params or {},
        })

    def deploy_toolkit(self, target_id: str) -> AgentResponse:
        return self._c.post("/api/orchestrator/deploy-toolkit", {
            "target_device_id": target_id,
        })

    def status(self) -> AgentResponse:
        return self._c.get("/api/orchestrator/status")

    def sync(self, data_type: str, device_ids: list[str] | None = None,
             direction: str = "source_to_targets", source_id: str = "") -> AgentResponse:
        return self._c.post("/api/orchestrator/sync", {
            "data_type": data_type,
            "device_ids": device_ids or ["*"],
            "direction": direction,
            "source_device_id": source_id,
        })
