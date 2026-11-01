#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Verify this checkout; dependencies still come from the selected environment.
export PYTHONPATH="$PWD/src:$PWD/scripts"
PYTHON="${GOVERNANCE_PYTHON:-${PYTHON_BIN:-python}}"
"$PYTHON" - <<'VERIFY_SOURCE'
import importlib.util
import sysconfig
import tomllib
from pathlib import Path

repository = Path.cwd().resolve()
with (repository / "pyproject.toml").open("rb") as stream:
    project = tomllib.load(stream)
if project["tool"]["setuptools"]["packages"]["find"]["where"] != ["src"]:
    raise SystemExit("SOURCE_DISCOVERY_INVALID")
source = (repository / "src").resolve()
if not source.is_relative_to(repository):
    raise SystemExit("SOURCE_DISCOVERY_INVALID")
library_roots = {Path(sysconfig.get_path(name)).resolve() for name in ("purelib", "platlib")}
for name in ("harness_observatory", "ruff", "pytest", "duckdb", "jsonschema", "esprima", "bashlex"):
    specification = importlib.util.find_spec(name)
    if specification is None or specification.origin is None:
        raise SystemExit("SOURCE_IMPORT_UNAVAILABLE:" + name)
    origin = Path(specification.origin).resolve()
    roots = {source} if name == "harness_observatory" else library_roots
    if not any(origin.is_relative_to(root) for root in roots):
        raise SystemExit("SOURCE_IMPORT_ORIGIN_INVALID:" + name)
print("Source and verifier package origins passed.")
VERIFY_SOURCE
"$PYTHON" -m ruff check .
"$PYTHON" -m ruff format --check .
"$PYTHON" -m pytest -q
"$PYTHON" -m harness_observatory.cli --help >/dev/null
"$PYTHON" scripts/demo.py
scripts/verify_public_safe.sh
