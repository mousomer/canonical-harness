#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${HARNESS_PYTHON:-${GOVERNANCE_PYTHON:-${PYTHON_BIN:-${HOME}/workspace/venvs/venv314/bin/python}}}"
if [[ ! -x "$PYTHON" ]]; then
  echo 'Repository verification refused: select an approved existing Python.' >&2
  exit 2
fi
export HARNESS_PYTHON="$PYTHON" GOVERNANCE_PYTHON="$PYTHON" PYTHON_BIN="$PYTHON"
export PATH="$(dirname -- "$PYTHON"):/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export PYTEST_ADDOPTS='-p no:cacheprovider'
cd "$ROOT"

# Verify the unchanged explicit kit components without requiring private state.
"$PYTHON" -I -B - "$ROOT" <<'VERIFY_KIT'
import pathlib
import sys
import types

root = pathlib.Path(sys.argv[1]).resolve()
namespace = types.ModuleType("tools")
namespace.__path__ = [str(root / "tools")]
sys.modules["tools"] = namespace
sys.path.insert(0, str(root))
from tools.policy_kit_distribution.verification import main

raise SystemExit(main(root, ["--candidate", str(root)]))
VERIFY_KIT

"$PYTHON" -I -B -m pytest -q repository_tests -p no:cacheprovider

"$PYTHON" -B -m ruff check tools/publication repository_tests
"$PYTHON" -B -m ruff format --check tools/publication repository_tests
"$ROOT/packages/harness-observatory/scripts/verify.sh"
echo 'Canonical harness repository verification passed.'
