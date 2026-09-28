from __future__ import annotations

import base64
import json
import unittest

from src.mcp_server import (
    AgentError,
    AndroidMcpTools,
    BinaryResponse,
    McpStdioServer,
    PROTECTED_DELETE_PATHS,
    TOOLS,
)


class FakeClient:
    def __init__(self) -> None:
        self.calls = []

    def get(self, endpoint, params=None, *, binary=False):
        self.calls.append(("GET", endpoint, params, binary))
        if binary:
            return BinaryResponse(b"hello", "text/plain", {})
        return {"endpoint": endpoint, "params": params}

    def post(self, endpoint, body=None, *, params=None):
        self.calls.append(("POST", endpoint, params, body))
        return {"endpoint": endpoint, "params": params, "body": body}


class AndroidMcpToolsTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.tools = AndroidMcpTools(self.client)

    def test_tool_names_are_unique(self):
        names = [tool["name"] for tool in TOOLS]
        self.assertEqual(len(names), len(set(names)))

    def test_shell_uses_agent_contract(self):
        result = self.tools.call(
            "android_shell",
            {"command": "id", "timeout": 12, "confirm": True},
        )
        self.assertEqual(result["body"], {"cmd": "id", "timeout": 12})

    def test_shell_requires_confirmation(self):
        with self.assertRaisesRegex(ValueError, "confirm=true"):
            self.tools.call("android_shell", {"command": "id", "confirm": False})

    def test_write_decodes_base64_and_uses_query_path(self):
        encoded = base64.b64encode(b"abc").decode("ascii")
        result = self.tools.call(
            "android_write_file",
            {"path": "/sdcard/test.txt", "content_base64": encoded, "confirm": True},
        )
        self.assertEqual(result["params"], {"path": "/sdcard/test.txt"})
        self.assertEqual(result["body"], b"abc")

    def test_delete_rejects_storage_roots(self):
        for path in PROTECTED_DELETE_PATHS:
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "protected root"):
                self.tools.call("android_delete_path", {"path": path, "confirm": True})

    def test_read_file_returns_utf8(self):
        result = self.tools.call("android_read_file", {"path": "/sdcard/a.txt"})
        self.assertEqual(result["encoding"], "utf-8")
        self.assertEqual(result["content"], "hello")


class McpProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = McpStdioServer(AndroidMcpTools(FakeClient()))

    def test_initialize(self):
        response = self.server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(response["result"]["serverInfo"]["name"], "adb-toolkit-android")

    def test_tools_list(self):
        response = self.server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertGreaterEqual(len(response["result"]["tools"]), 10)

    def test_tool_errors_are_mcp_results(self):
        response = self.server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "android_shell", "arguments": {"command": "id", "confirm": False}},
            }
        )
        self.assertTrue(response["result"]["isError"])
        error = json.loads(response["result"]["content"][0]["text"])
        self.assertIn("confirm=true", error["error"])

    def test_notifications_do_not_receive_responses(self):
        self.assertIsNone(self.server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}))


if __name__ == "__main__":
    unittest.main()
