#!/bin/sh
# Projektprüfung: alle Tests
set -e
cd "$(dirname "$0")/.."
uv run pytest -q -p no:warnings
