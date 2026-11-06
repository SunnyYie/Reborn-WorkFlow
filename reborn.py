#!/usr/bin/env python3
"""Portable project entry point. No installation happens without consent."""

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main():
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.is_file() and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        return subprocess.call([str(python), "-B", str(ROOT / "reborn.py"), *sys.argv[1:]])
    from portable.runtime import main as run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
