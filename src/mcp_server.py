"""MCP stdio bridge for the ADB Toolkit Android Agent.

This module intentionally implements the small JSON-RPC surface required by an
MCP stdio server without third-party packages.  That keeps it runnable in
Termux/PRoot, where the official Python MCP SDK's compiled dependencies may not
have Android wheels.

Run with::

    python -m src.mcp_server

Configuration is read from environment variables; see ``README.md``.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


SERVER_NAME = "adb-toolkit-android"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2024-11-05")
DEFAULT_AGENT_URL = "http://127.0.0.1:15555"
DEFAULT_TOKEN_FILE = "~/.config/adb-toolkit/agent-token"
MAX_FILE_RESPONSE = 1024 * 1024


class AgentError(RuntimeError):
    """Raised when the Android Agent rejects or cannot complete a request."""

    def __init__(self, message: str, status: int = 0, data: Any = None):
        super().__init__(message)
        self.status = status
        self.data = data


@dataclass(frozen=True)
class BinaryResponse:
    data: bytes
    content_type: str
    headers: dict[str, str]


class AgentApiClient:
    """Small HTTP client for the embedded Android Agent API."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("ADB_TOOLKIT_AGENT_URL", DEFAULT_AGENT_URL)).rstrip("/")
        self.token = token if token is not None else self._load_token()
        self.timeout = timeout or float(os.getenv("ADB_TOOLKIT_AGENT_TIMEOUT", "30"))

    @staticmethod
    def _load_token() -> str:
        token = os.getenv("ADB_TOOLKIT_AGENT_TOKEN", "").strip()
        if token:
            return token

        token_file = os.getenv("ADB_TOOLKIT_AGENT_TOKEN_FILE", DEFAULT_TOKEN_FILE).strip()
        token_path = Path(token_file).expanduser()
        if token_path.exists():
            try:
                return token_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise AgentError(f"Could not read ADB_TOOLKIT_AGENT_TOKEN_FILE: {exc}") from exc
        return ""

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | bytes | None = None,
        expect_binary: bool = False,
    ) -> Any:
        url = f"{self.base_url}{endpoint}"
        if params:
            encoded = {key: str(value).lower() if isinstance(value, bool) else value for key, value in params.items()}
            url += "?" + urllib.parse.urlencode(encoded)

        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Agent-Token"] = self.token

        payload: bytes | None = None
        if isinstance(body, dict):
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif isinstance(body, bytes):
            payload = body
            headers["Content-Type"] = "application/octet-stream"

        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                content_type = response.headers.get_content_type()
                response_headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            data = self._decode_error(raw)
            message = self._error_message(data, exc.reason)
            if exc.code == 401 and not self.token:
                message += "; configure ADB_TOOLKIT_AGENT_TOKEN or ADB_TOOLKIT_AGENT_TOKEN_FILE"
            raise AgentError(message, status=exc.code, data=data) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AgentError(f"Could not reach Android Agent at {self.base_url}: {exc}") from exc

        if expect_binary or content_type != "application/json":
            return BinaryResponse(raw, content_type, response_headers)

        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentError(f"Agent returned invalid JSON from {endpoint}") from exc

    @staticmethod
    def _decode_error(raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _error_message(data: Any, fallback: Any) -> str:
        if isinstance(data, dict):
            return str(data.get("message") or data.get("error") or fallback)
        return str(data or fallback)

    def get(self, endpoint: str, params: dict[str, Any] | None = None, *, binary: bool = False) -> Any:
        return self.request("GET", endpoint, params=params, expect_binary=binary)

    def post(
        self,
        endpoint: str,
        body: dict[str, Any] | bytes | None = None,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return self.request("POST", endpoint, params=params, body=body)


def _schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def _tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    *,
    read_only: bool,
    destructive: bool = False,
    idempotent: bool = True,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": input_schema,
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": idempotent,
            "openWorldHint": False,
        },
    }


TOOLS = [
    _tool("android_ping", "Check whether the ADB Toolkit Android Agent is running. No token is required.", _schema(), read_only=True),
    _tool(
        "android_device_info",
        "Read Android device information, battery, network, storage, permissions, or filtered system properties.",
        _schema(
            {
                "section": {
                    "type": "string",
                    "enum": ["info", "battery", "network", "storage", "permissions", "props"],
                    "default": "info",
                },
                "filter": {"type": "string", "description": "Optional substring filter for the props section."},
            }
        ),
        read_only=True,
    ),
    _tool(
        "android_list_files",
        "List files visible to the Android Agent.",
        _schema(
            {
                "path": {"type": "string", "default": "/sdcard"},
                "recursive": {"type": "boolean", "default": False},
            }
        ),
        read_only=True,
    ),
    _tool(
        "android_file_stat",
        "Read metadata for an Android file or directory.",
        _schema({"path": {"type": "string"}}, ["path"]),
        read_only=True,
    ),
    _tool(
        "android_search_files",
        "Search file names below an Android directory using a case-insensitive regular expression.",
        _schema(
            {
                "path": {"type": "string", "default": "/sdcard"},
                "pattern": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            ["pattern"],
        ),
        read_only=True,
    ),
    _tool(
        "android_read_file",
        "Read at most 1 MiB from a file. Text is returned as UTF-8; binary data is returned as base64.",
        _schema(
            {
                "path": {"type": "string"},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_FILE_RESPONSE, "default": 131072},
            },
            ["path"],
        ),
        read_only=True,
    ),
    _tool(
        "android_write_file",
        "Write base64-encoded bytes to an Android path. The caller must explicitly confirm the write.",
        _schema(
            {
                "path": {"type": "string"},
                "content_base64": {"type": "string"},
                "confirm": {"type": "boolean", "description": "Must be true after the user approves the write."},
            },
            ["path", "content_base64", "confirm"],
        ),
        read_only=False,
        destructive=False,
    ),
    _tool(
        "android_delete_path",
        "Delete an Android file or directory recursively. Refuses storage roots and requires explicit confirmation.",
        _schema(
            {
                "path": {"type": "string"},
                "confirm": {"type": "boolean", "description": "Must be true after the user approves deletion."},
            },
            ["path", "confirm"],
        ),
        read_only=False,
        destructive=True,
    ),
    _tool(
        "android_list_apps",
        "List packages installed on Android.",
        _schema({"third_party_only": {"type": "boolean", "default": True}}),
        read_only=True,
    ),
    _tool(
        "android_app_info",
        "Read details for an installed Android package.",
        _schema({"package": {"type": "string"}}, ["package"]),
        read_only=True,
    ),
    _tool(
        "android_screenshot",
        "Capture the Android screen and save the PNG to an explicit local path.",
        _schema({"local_path": {"type": "string"}}, ["local_path"]),
        read_only=False,
        destructive=False,
        idempotent=False,
    ),
    _tool(
        "android_getprop",
        "Read one Android system property.",
        _schema({"key": {"type": "string"}}, ["key"]),
        read_only=True,
    ),
    _tool(
        "android_shell",
        "Execute a shell command as the Android Agent app UID. Requires explicit confirmation for every command.",
        _schema(
            {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 300, "default": 30},
                "confirm": {"type": "boolean", "description": "Must be true after the user approves command execution."},
            },
            ["command", "confirm"],
        ),
        read_only=False,
        destructive=True,
        idempotent=False,
    ),
]


PROTECTED_DELETE_PATHS = {
    "/",
    "/sdcard",
    "/storage",
    "/storage/emulated",
    "/storage/emulated/0",
}


class AndroidMcpTools:
    def __init__(self, client: AgentApiClient | None = None) -> None:
        self.client = client or AgentApiClient()
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "android_ping": self.ping,
            "android_device_info": self.device_info,
            "android_list_files": self.list_files,
            "android_file_stat": self.file_stat,
            "android_search_files": self.search_files,
            "android_read_file": self.read_file,
            "android_write_file": self.write_file,
            "android_delete_path": self.delete_path,
            "android_list_apps": self.list_apps,
            "android_app_info": self.app_info,
            "android_screenshot": self.screenshot,
            "android_getprop": self.getprop,
            "android_shell": self.shell,
        }

    def call(self, name: str, arguments: dict[str, Any] | None) -> Any:
        handler = self.handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool: {name}")
        return handler(arguments or {})

    def ping(self, _: dict[str, Any]) -> Any:
        return self.client.get("/api/ping")

    def device_info(self, args: dict[str, Any]) -> Any:
        section = args.get("section", "info")
        if section not in {"info", "battery", "network", "storage", "permissions", "props"}:
            raise ValueError(f"Unsupported section: {section}")
        params = {"filter": args.get("filter", "")} if section == "props" else None
        return self.client.get(f"/api/device/{section}", params)

    def list_files(self, args: dict[str, Any]) -> Any:
        return self.client.get(
            "/api/files/list",
            {"path": args.get("path", "/sdcard"), "recursive": bool(args.get("recursive", False))},
        )

    def file_stat(self, args: dict[str, Any]) -> Any:
        return self.client.get("/api/files/stat", {"path": self._required_string(args, "path")})

    def search_files(self, args: dict[str, Any]) -> Any:
        pattern = self._required_string(args, "pattern")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc
        maximum = min(max(int(args.get("max_results", 100)), 1), 500)
        return self.client.get(
            "/api/files/search",
            {"path": args.get("path", "/sdcard"), "pattern": pattern, "max": maximum},
        )

    def read_file(self, args: dict[str, Any]) -> Any:
        path = self._required_string(args, "path")
        maximum = min(max(int(args.get("max_bytes", 131072)), 1), MAX_FILE_RESPONSE)
        response = self.client.get("/api/files/read", {"path": path}, binary=True)
        if len(response.data) > maximum:
            raise ValueError(f"File is {len(response.data)} bytes; max_bytes is {maximum}")

        content_type = response.content_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
        try:
            text = response.data.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "path": path,
                "size": len(response.data),
                "content_type": content_type,
                "encoding": "base64",
                "content": base64.b64encode(response.data).decode("ascii"),
            }
        return {
            "path": path,
            "size": len(response.data),
            "content_type": content_type,
            "encoding": "utf-8",
            "content": text,
        }

    def write_file(self, args: dict[str, Any]) -> Any:
        self._require_confirmation(args, "write this file")
        path = self._required_string(args, "path")
        try:
            data = base64.b64decode(self._required_string(args, "content_base64"), validate=True)
        except ValueError as exc:
            raise ValueError("content_base64 is not valid base64") from exc
        return self.client.post("/api/files/write", data, params={"path": path})

    def delete_path(self, args: dict[str, Any]) -> Any:
        self._require_confirmation(args, "delete this path")
        path = self._required_string(args, "path").rstrip("/") or "/"
        if path in PROTECTED_DELETE_PATHS:
            raise ValueError(f"Refusing to delete protected root: {path}")
        return self.client.post("/api/files/delete", params={"path": path})

    def list_apps(self, args: dict[str, Any]) -> Any:
        return self.client.get("/api/apps/list", {"third_party": bool(args.get("third_party_only", True))})

    def app_info(self, args: dict[str, Any]) -> Any:
        package = self._required_string(args, "package")
        return self.client.get(f"/api/apps/info/{urllib.parse.quote(package, safe='')}")

    def screenshot(self, args: dict[str, Any]) -> Any:
        local_path = Path(self._required_string(args, "local_path")).expanduser().resolve()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        response = self.client.get("/api/device/screen", binary=True)
        local_path.write_bytes(response.data)
        return {
            "saved": True,
            "path": str(local_path),
            "size": len(response.data),
            "content_type": response.content_type,
        }

    def getprop(self, args: dict[str, Any]) -> Any:
        return self.client.get("/api/shell/getprop", {"key": self._required_string(args, "key")})

    def shell(self, args: dict[str, Any]) -> Any:
        self._require_confirmation(args, "execute this shell command")
        command = self._required_string(args, "command")
        timeout = min(max(int(args.get("timeout", 30)), 1), 300)
        return self.client.post("/api/shell/exec", {"cmd": command, "timeout": timeout})

    @staticmethod
    def _required_string(args: dict[str, Any], key: str) -> str:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        return value

    @staticmethod
    def _require_confirmation(args: dict[str, Any], action: str) -> None:
        if args.get("confirm") is not True:
            raise ValueError(f"Refusing to {action} without confirm=true")


class McpStdioServer:
    def __init__(self, tools: AndroidMcpTools | None = None) -> None:
        self.tools = tools or AndroidMcpTools()

    def serve_forever(self) -> None:
        for raw_line in sys.stdin.buffer:
            if not raw_line.strip():
                continue
            try:
                message = json.loads(raw_line)
                response = self.handle_message(message)
            except Exception as exc:  # keep the server alive on malformed input
                self._log_exception(exc)
                response = self._error(None, -32700, f"Parse error: {exc}")
            if response is not None:
                self._write(response)

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return self._error(None, -32600, "Invalid Request")

        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}

        if request_id is None:
            return None
        if method == "initialize":
            requested = params.get("protocolVersion")
            protocol = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
            return self._result(
                request_id,
                {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": (
                        "Use read-only Android tools freely. Before writes, deletion, screenshots, or shell commands, "
                        "explain the action to the user and obtain confirmation; mutation tools also require confirm=true."
                    ),
                },
            )
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._call_tool(request_id, params)
        return self._error(request_id, -32601, f"Method not found: {method}")

    def _call_tool(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self.tools.call(str(params.get("name", "")), params.get("arguments") or {})
            text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
            return self._result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except (AgentError, ValueError, TypeError, OSError) as exc:
            detail: dict[str, Any] = {"error": str(exc)}
            if isinstance(exc, AgentError) and exc.status:
                detail["status"] = exc.status
            text = json.dumps(detail, ensure_ascii=False, indent=2)
            return self._result(request_id, {"content": [{"type": "text", "text": text}], "isError": True})
        except Exception as exc:
            self._log_exception(exc)
            text = json.dumps({"error": f"Internal MCP error: {exc}"}, ensure_ascii=False)
            return self._result(request_id, {"content": [{"type": "text", "text": text}], "isError": True})

    @staticmethod
    def _result(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _write(message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        sys.stdout.buffer.write(data.encode("utf-8"))
        sys.stdout.buffer.flush()

    @staticmethod
    def _log_exception(exc: Exception) -> None:
        if os.getenv("ADB_TOOLKIT_MCP_DEBUG") == "1":
            traceback.print_exception(exc, file=sys.stderr)
        else:
            print(f"{SERVER_NAME}: {exc}", file=sys.stderr, flush=True)


def main() -> None:
    McpStdioServer().serve_forever()


if __name__ == "__main__":
    main()
