#!/bin/sh
set -eu
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
DEFAULT=python3
if [ -x "$ROOT/.venv/bin/python" ]; then
    DEFAULT="$ROOT/.venv/bin/python"
fi
PYTHON="${REBORN_PYTHON:-$DEFAULT}"
if ! "$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10)' >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
        printf '%s' 'Authorize Python 3.12 installation using existing Homebrew? [y/N] '
        read -r answer
        if [ "$answer" != y ]; then exit 2; fi
        brew install python@3.12
        PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
    else
        printf '%s\n' 'Python 3.10+ is required. An administrator must provision Python 3.12.'
        exit 2
    fi
fi
"$PYTHON" -B "$ROOT/reborn.py" onboard
