#!/usr/bin/env python3
"""
On-device Python daemon — runs inside the agent's embedded Python runtime.

Provides pyaccelerate integration for high-speed data processing directly
on the Android device, eliminating USB/ADB bottleneck for heavy operations.

Usage (from PythonApi):
    python3 daemon.py [--port 15560] [--once]
"""
import json
import os
import sys
import socket
import hashlib
import time
import threading
from pathlib import Path

DAEMON_PORT = 15560
VERSION = "1.0.0"


def get_device_info():
    """Collect basic info from the Android environment."""
    return {
        "python_version": sys.version,
        "platform": sys.platform,
        "daemon_version": VERSION,
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "home": os.environ.get("HOME", ""),
    }


def hash_file(path, algo="sha256", chunk_size=1 << 20):
    """Hash a file with the given algorithm."""
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def diff_file_lists(src_files, dst_files):
    """
    Compare two file lists (dicts of {relpath: {size, mtime, hash}})
    and return files to copy/update/delete.
    """
    to_copy = []
    to_update = []
    to_delete = []

    for path, info in src_files.items():
        if path not in dst_files:
            to_copy.append(path)
        elif info.get("hash") != dst_files[path].get("hash"):
            to_update.append(path)

    for path in dst_files:
        if path not in src_files:
            to_delete.append(path)

    return {"copy": to_copy, "update": to_update, "delete": to_delete}


def scan_directory(base_path, extensions=None):
    """Recursively scan a directory and return file metadata."""
    files = {}
    base = Path(base_path)
    if not base.exists():
        return files

    for p in base.rglob("*"):
        if p.is_file():
            if extensions and p.suffix.lower() not in extensions:
                continue
            rel = str(p.relative_to(base))
            stat = p.stat()
            files[rel] = {
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "path": str(p),
            }
    return files


def try_import_pyaccelerate():
    """Attempt to import pyaccelerate and return status."""
    try:
        import pyaccelerate
        return {
            "available": True,
            "version": getattr(pyaccelerate, "__version__", "unknown"),
        }
    except ImportError:
        return {"available": False, "version": None}


def handle_command(cmd):
    """Process a daemon command and return a response."""
    action = cmd.get("action", "")

    if action == "ping":
        return {"status": "pong", **get_device_info()}

    elif action == "info":
        return {
            **get_device_info(),
            "pyaccelerate": try_import_pyaccelerate(),
        }

    elif action == "hash":
        path = cmd.get("path", "")
        algo = cmd.get("algo", "sha256")
        if not path or not os.path.exists(path):
            return {"error": f"File not found: {path}"}
        return {"hash": hash_file(path, algo), "algo": algo, "path": path}

    elif action == "scan":
        path = cmd.get("path", "")
        exts = cmd.get("extensions")
        if not path:
            return {"error": "Missing path"}
        files = scan_directory(path, exts)
        return {"files": files, "count": len(files)}

    elif action == "diff":
        src = cmd.get("src_files", {})
        dst = cmd.get("dst_files", {})
        return diff_file_lists(src, dst)

    elif action == "exec":
        code = cmd.get("code", "")
        if not code:
            return {"error": "Missing code"}
        import io
        old_stdout = sys.stdout
        sys.stdout = buf = io.StringIO()
        try:
            exec(code)  # noqa: S102
            return {"stdout": buf.getvalue(), "error": None}
        except Exception as e:
            return {"stdout": buf.getvalue(), "error": str(e)}
        finally:
            sys.stdout = old_stdout

    else:
        return {"error": f"Unknown action: {action}"}


def run_server(port=DAEMON_PORT):
    """Simple JSON-over-TCP server for daemon commands."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(4)
    print(f"Daemon listening on 127.0.0.1:{port}")

    while True:
        try:
            conn, addr = server.accept()
            threading.Thread(
                target=handle_client, args=(conn,), daemon=True
            ).start()
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Accept error: {e}")

    server.close()


def handle_client(conn):
    """Handle a single client connection."""
    try:
        data = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break

        if data:
            cmd = json.loads(data.decode("utf-8"))
            response = handle_command(cmd)
            conn.sendall(json.dumps(response).encode("utf-8") + b"\n")
    except Exception as e:
        try:
            conn.sendall(json.dumps({"error": str(e)}).encode("utf-8") + b"\n")
        except Exception:
            pass
    finally:
        conn.close()


def run_once(cmd_json):
    """Execute a single command from stdin/arg and print result."""
    cmd = json.loads(cmd_json)
    result = handle_command(cmd)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ADB Toolkit on-device daemon")
    parser.add_argument("--port", type=int, default=DAEMON_PORT)
    parser.add_argument("--once", type=str, help="Run single command (JSON)")
    args = parser.parse_args()

    if args.once:
        run_once(args.once)
    else:
        run_server(args.port)
