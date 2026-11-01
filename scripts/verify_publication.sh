#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${HARNESS_PYTHON:-${HOME}/workspace/venvs/venv314/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo 'Publication verification refused: selected existing Python unavailable.' >&2
  exit 2
fi
if [[ "$#" -gt 1 || ( "$#" -eq 1 && "$1" != "--full-history" ) ]]; then
  echo 'Publication verification refused: unsupported arguments.' >&2
  exit 2
fi
exec "$PYTHON" -I -B "$ROOT/tools/publication/verify.py" \
  --repo "$ROOT" --published-base df332ad5eed18926b7a17d0795380b65c7fd4f98 "$@"
