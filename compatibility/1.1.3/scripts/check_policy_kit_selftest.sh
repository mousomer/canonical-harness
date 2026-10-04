#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLICY_KIT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  if command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    echo "FAIL: python3/python not found" >&2
    exit 1
  fi
fi

require_file() {
  local p="$1"
  [[ -f "$p" ]] || { echo "FAIL: missing file: $p" >&2; exit 1; }
}

require_executable() {
  local p="$1"
  [[ -x "$p" ]] || { echo "FAIL: missing executable: $p" >&2; exit 1; }
}

echo "Self-test: validating policy-kit files"
require_file "$POLICY_KIT_DIR/POLICY_VERSION.json"
require_file "$POLICY_KIT_DIR/required_files_manifest.json"
require_file "$POLICY_KIT_DIR/sanitation_baseline.gitignore"
require_executable "$POLICY_KIT_DIR/scripts/sync_workspace_policy.sh"
require_executable "$POLICY_KIT_DIR/scripts/audit_workspace_policy.sh"
require_file "$POLICY_KIT_DIR/scripts/check_git_sanitation_template.sh"
require_file "$POLICY_KIT_DIR/scripts/check_policy_compliance_template.sh"
require_file "$POLICY_KIT_DIR/scripts/check_policy_template_drift_template.sh"
require_file "$POLICY_KIT_DIR/scripts/update_policy_template_hashes_template.sh"
require_executable "$POLICY_KIT_DIR/scripts/check_policy_kit_selftest.sh"
require_file "$POLICY_KIT_DIR/templates/verify_template.sh"
require_file "$POLICY_KIT_DIR/templates/policy_manifest.json.template"
require_file "$POLICY_KIT_DIR/templates/policy_template_hashes.json.template"

"$PYTHON_BIN" - <<PY
import json
from pathlib import Path
base = Path('$POLICY_KIT_DIR')
json_files = [
    base / 'POLICY_VERSION.json',
    base / 'required_files_manifest.json',
    base / 'templates' / 'policy_manifest.json.template',
    base / 'templates' / 'policy_template_hashes.json.template',
]
for p in json_files:
    json.loads(p.read_text(encoding='utf-8'))
required = json.loads((base / 'required_files_manifest.json').read_text(encoding='utf-8'))
required_files = set(required.get('required_files', []))
expected = {
    '.workspace_policy_version.json',
    'scripts/verify.sh',
    'scripts/check_git_sanitation.sh',
    'scripts/check_policy_compliance.sh',
    'scripts/check_policy_template_drift.sh',
    '.policy/policy_template_hashes.json',
}
missing = sorted(expected - required_files)
if missing:
    raise SystemExit(f'FAIL: required_files_manifest missing expected entries: {missing}')
print('Self-test: JSON parsing and required-files manifest checks passed')
PY

echo "Self-test: validating sync helper CLI"
"$POLICY_KIT_DIR/scripts/sync_workspace_policy.sh" --help >/dev/null

TMPDIR_ROOT="$(mktemp -d /tmp/policy-kit-selftest.XXXXXX)"
trap 'rm -rf "$TMPDIR_ROOT"' EXIT
FIXTURE_REPO="$TMPDIR_ROOT/fixture-repo"
mkdir -p "$FIXTURE_REPO"
(
  cd "$FIXTURE_REPO"
  git init -q
  mkdir -p docs scripts
  cat > scripts/check_demo_repo.sh <<'SH'
#!/usr/bin/env bash
exit 0
SH
  chmod +x scripts/check_demo_repo.sh
  cat > scripts/check_demo_repo_repo.sh <<'SH'
#!/usr/bin/env bash
exit 0
SH
  chmod +x scripts/check_demo_repo_repo.sh
)

REPO_EXT_HASH_BEFORE="$(shasum -a 256 "$FIXTURE_REPO/scripts/check_demo_repo_repo.sh" | awk '{print $1}')"

echo "Self-test: sync helper dry-run"
"$POLICY_KIT_DIR/scripts/sync_workspace_policy.sh" \
  --policy-kit-dir "$POLICY_KIT_DIR" \
  --repo "$FIXTURE_REPO" \
  --dry-run >/dev/null

echo "Self-test: sync helper apply"
"$POLICY_KIT_DIR/scripts/sync_workspace_policy.sh" \
  --policy-kit-dir "$POLICY_KIT_DIR" \
  --repo "$FIXTURE_REPO" >/dev/null

for generated in \
  "$FIXTURE_REPO/.workspace_policy_version.json" \
  "$FIXTURE_REPO/scripts/check_git_sanitation.sh" \
  "$FIXTURE_REPO/scripts/check_policy_compliance.sh" \
  "$FIXTURE_REPO/scripts/check_policy_template_drift.sh" \
  "$FIXTURE_REPO/scripts/update_policy_template_hashes.sh" \
  "$FIXTURE_REPO/scripts/verify.sh" \
  "$FIXTURE_REPO/.policy/policy_template_hashes.json"
do
  require_file "$generated"
done

REPO_EXT_HASH_AFTER="$(shasum -a 256 "$FIXTURE_REPO/scripts/check_demo_repo_repo.sh" | awk '{print $1}')"
if [[ "$REPO_EXT_HASH_BEFORE" != "$REPO_EXT_HASH_AFTER" ]]; then
  echo "FAIL: repo extension script was modified by sync helper" >&2
  exit 1
fi

"$PYTHON_BIN" - <<PY
import json
from pathlib import Path
fixture = Path('$FIXTURE_REPO')
wv = json.loads((fixture / '.workspace_policy_version.json').read_text(encoding='utf-8'))
assert wv['policy_id'] == 'workspace-github-sanitation-and-security'
assert wv['policy_source'] == 'workspace/policy-kit'
assert wv['policy_source_path'] == '$' + '{HOME}/workspace/policy-kit'
hash_manifest = json.loads((fixture / '.policy' / 'policy_template_hashes.json').read_text(encoding='utf-8'))
assert hash_manifest['hash_algorithm'] == 'sha256'
assert len(hash_manifest['templates']) == 3
print('Self-test: generated metadata/hash manifest validation passed')
PY

(
  cd "$FIXTURE_REPO"
  ./scripts/check_policy_compliance.sh >/dev/null
  ./scripts/check_policy_template_drift.sh >/dev/null
  ./scripts/update_policy_template_hashes.sh >/dev/null
)

echo "PASS: policy-kit self-test completed"
