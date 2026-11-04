#!/usr/bin/env python3
"""Reject private/runtime files in the Git index, a tree, or reachable history."""
import argparse
import json
from pathlib import PurePosixPath
import subprocess

PRIVATE_ROOTS = ("workflow/runs/", "rag/data/", "memory/", "demo/")
PRIVATE_SUFFIXES = {
    ".pyc", ".pyo", ".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db",
    ".mp3", ".wav", ".m4a", ".flac", ".mp4", ".mov", ".docx", ".pdf",
}
PRIVATE_PARTS = {
    "__pycache__", ".aws", ".codex", ".agents", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "htmlcov",
}


def reason(path):
    parts = PurePosixPath(path).parts
    name = parts[-1].lower()
    if path.startswith(PRIVATE_ROOTS) or path == "rag/catalog.json":
        return "private_runtime_or_library"
    if any(part.lower() in PRIVATE_PARTS for part in parts):
        return "local_configuration_or_cache"
    if name in {".ds_store", "credentials.json", ".coverage"} or name.startswith(("._", "client_secret")):
        return "local_metadata_or_credentials"
    if name == ".env" or name.startswith(".env.") and name != ".env.example":
        return "environment_credentials"
    if (PurePosixPath(name).suffix in PRIVATE_SUFFIXES
            or name.endswith((".sqlite-wal", ".sqlite-shm", ".sqlite3-wal", ".sqlite3-shm",
                              ".db-wal", ".db-shm", ".log", ".tmp"))):
        return "private_material_or_generated_file"
    return None


def git(*args):
    return subprocess.check_output(["git", *args])


def paths(tree=None):
    command = ("ls-tree", "-r", "--name-only", "-z", tree) if tree else ("ls-files", "-z")
    return [item.decode("utf-8", "surrogateescape") for item in git(*command).split(b"\0") if item]


def inspect(tree=None, history=False):
    findings = []
    trees = git("rev-list", "--all").decode().splitlines() if history else [tree]
    for revision in trees:
        for path in paths(revision):
            rule = reason(path)
            if rule:
                findings.append({"path": path, "rule": rule, "revision": revision or "index"})
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--tree", help="Inspect a committed tree instead of the index")
    mode.add_argument("--history", action="store_true", help="Inspect all reachable commits")
    args = parser.parse_args()
    try:
        findings = inspect(args.tree, args.history)
    except (subprocess.CalledProcessError, OSError):
        print(json.dumps({"error": "git_inspection_failed"}))
        return 2
    print(json.dumps({"scope": "history" if args.history else args.tree or "index",
                      "findings": findings, "count": len(findings)}, ensure_ascii=True, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
