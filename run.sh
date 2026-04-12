#!/usr/bin/env bash
# Run Job Copilot using the virtual environment Python.
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/.venv/bin/python3.12" "$SCRIPT_DIR/app.py" "$@"
