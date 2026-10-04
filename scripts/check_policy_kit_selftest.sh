#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec '/Users/omer/workspace/venvs/venv314/bin/python' "$ROOT/_machine/distribution_cli.py" selfcheck "$@"
