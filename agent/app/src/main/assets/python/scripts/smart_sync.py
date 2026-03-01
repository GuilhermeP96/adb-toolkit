#!/usr/bin/env python3
"""
Smart sync — runs on-device to compute diffs and orchestrate transfers.

Given a manifest of files from the source device, computes which files
need to be transferred, updated, or deleted on the target.

Usage:
    python3 smart_sync.py --manifest /path/to/manifest.json --target /sdcard/Backup/
"""
import json
import hashlib
import os
import sys
from pathlib import Path


def hash_file(path, chunk_size=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def scan_local(base_path):
    """Scan local files and return metadata dict."""
    files = {}
    base = Path(base_path)
    if not base.exists():
        return files
    for p in base.rglob("*"):
        if p.is_file():
            rel = str(p.relative_to(base))
            stat = p.stat()
            files[rel] = {
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "hash": hash_file(str(p)),
            }
    return files


def compute_diff(source_manifest, local_files):
    """Compute transfer plan."""
    to_transfer = []
    to_update = []
    unchanged = []
    to_delete = []

    for rel, info in source_manifest.items():
        if rel not in local_files:
            to_transfer.append({"path": rel, "size": info.get("size", 0)})
        elif info.get("hash") != local_files[rel].get("hash"):
            to_update.append({"path": rel, "size": info.get("size", 0)})
        else:
            unchanged.append(rel)

    for rel in local_files:
        if rel not in source_manifest:
            to_delete.append(rel)

    total_bytes = sum(f["size"] for f in to_transfer + to_update)

    return {
        "transfer": to_transfer,
        "update": to_update,
        "unchanged": unchanged,
        "delete": to_delete,
        "stats": {
            "to_transfer": len(to_transfer),
            "to_update": len(to_update),
            "unchanged": len(unchanged),
            "to_delete": len(to_delete),
            "total_bytes": total_bytes,
        }
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Smart sync diff tool")
    parser.add_argument("--manifest", required=True, help="Source manifest JSON file")
    parser.add_argument("--target", required=True, help="Local target directory")
    parser.add_argument("--output", help="Output file (default: stdout)")
    args = parser.parse_args()

    with open(args.manifest) as f:
        source_manifest = json.load(f)

    local_files = scan_local(args.target)
    plan = compute_diff(source_manifest, local_files)

    result = json.dumps(plan, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w") as f:
            f.write(result)
        print(f"Plan written to {args.output}")
    else:
        print(result)


if __name__ == "__main__":
    main()
