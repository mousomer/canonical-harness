#!/usr/bin/env bash
set -euo pipefail

DEFAULT_WORKSPACE_ROOT="${HOME}/workspace"
DEFAULT_POLICY_KIT_DIR="${HOME}/workspace/policy-kit"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-${DEFAULT_WORKSPACE_ROOT}}"
POLICY_KIT_DIR="${POLICY_KIT_DIR:-${DEFAULT_POLICY_KIT_DIR}}"
DRY_RUN=0
TARGET_REPOS=()

usage() {
  cat <<'USAGE'
Usage: sync_workspace_policy.sh [options]

Options:
  --repo <path>          Sync only the specified git repo (repeatable)
  --workspace-root <p>   Workspace root to scan for git repos (default: ${HOME}/workspace)
  --policy-kit-dir <p>   Policy kit directory (default: ${HOME}/workspace/policy-kit)
  --dry-run              Print actions without writing files
  -h, --help             Show help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      TARGET_REPOS+=("$2")
      shift 2
      ;;
    --workspace-root)
      WORKSPACE_ROOT="$2"
      shift 2
      ;;
    --policy-kit-dir)
      POLICY_KIT_DIR="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

POLICY_VERSION_FILE="$POLICY_KIT_DIR/POLICY_VERSION.json"
SANITATION_BASELINE_FILE="$POLICY_KIT_DIR/sanitation_baseline.gitignore"
TPL_VERIFY="$POLICY_KIT_DIR/templates/verify_template.sh"
TPL_POLICY_COMPLIANCE="$POLICY_KIT_DIR/scripts/check_policy_compliance_template.sh"
TPL_SANITATION="$POLICY_KIT_DIR/scripts/check_git_sanitation_template.sh"
TPL_TEMPLATE_DRIFT="$POLICY_KIT_DIR/scripts/check_policy_template_drift_template.sh"
TPL_HASH_UPDATE="$POLICY_KIT_DIR/scripts/update_policy_template_hashes_template.sh"

for req in \
  "$POLICY_VERSION_FILE" \
  "$SANITATION_BASELINE_FILE" \
  "$TPL_VERIFY" \
  "$TPL_POLICY_COMPLIANCE" \
  "$TPL_SANITATION" \
  "$TPL_TEMPLATE_DRIFT" \
  "$TPL_HASH_UPDATE"
do
  if [[ ! -f "$req" ]]; then
    echo "ERROR: missing policy-kit file: $req" >&2
    exit 1
  fi
done

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  if command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    echo "ERROR: python3/python not found" >&2
    exit 1
  fi
fi

POLICY_VERSION="$($PYTHON_BIN - <<PY
import json
from pathlib import Path
obj = json.loads(Path('$POLICY_VERSION_FILE').read_text(encoding='utf-8'))
print(str(obj.get('policy_version', '')).strip())
PY
)"

if [[ -z "$POLICY_VERSION" ]]; then
  echo "ERROR: could not read policy_version from $POLICY_VERSION_FILE" >&2
  exit 1
fi

POLICY_ID="workspace-github-sanitation-and-security"
POLICY_SOURCE="workspace/policy-kit"
if [[ -n "${POLICY_SOURCE_PATH:-}" ]]; then
  POLICY_SOURCE_PATH="${POLICY_SOURCE_PATH}"
elif [[ "$POLICY_KIT_DIR" == "$DEFAULT_POLICY_KIT_DIR" ]]; then
  POLICY_SOURCE_PATH='${HOME}/workspace/policy-kit'
else
  POLICY_SOURCE_PATH="$POLICY_KIT_DIR"
fi
TODAY_UTC="$(date -u +%Y-%m-%d)"

copy_file() {
  local src="$1"
  local dst="$2"
  local mode="$3"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "    [dry-run] install -m $mode $src -> $dst"
  else
    mkdir -p "$(dirname "$dst")"
    install -m "$mode" "$src" "$dst"
  fi
}

write_workspace_policy_version() {
  local repo="$1"
  local dst="$repo/.workspace_policy_version.json"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "    [dry-run] write $dst (policy_version=$POLICY_VERSION, policy_id=$POLICY_ID)"
    return
  fi
  "$PYTHON_BIN" - <<PY
import json
from pathlib import Path
repo = Path('$repo')
out = repo / '.workspace_policy_version.json'
obj = {
  'policy_version': '$POLICY_VERSION',
  'policy_id': '$POLICY_ID',
  'policy_source': '$POLICY_SOURCE',
  'policy_source_path': '$POLICY_SOURCE_PATH',
  'synced_at': '$TODAY_UTC',
  'repo': repo.name,
}
out.write_text(json.dumps(obj, indent=2) + '\n', encoding='utf-8')
PY
}

maybe_scaffold_verify() {
  local repo="$1"
  local dst="$repo/scripts/verify.sh"
  if [[ -f "$dst" ]]; then
    echo "    preserve $dst"
    return
  fi
  copy_file "$TPL_VERIFY" "$dst" 755
}

maybe_scaffold_hash_update_helper() {
  local repo="$1"
  local dst="$repo/scripts/update_policy_template_hashes.sh"
  if [[ -f "$dst" ]]; then
    echo "    preserve $dst"
    return
  fi
  copy_file "$TPL_HASH_UPDATE" "$dst" 755
}

maybe_scaffold_sanitation_baseline() {
  local repo="$1"
  local dst="$repo/.policy/sanitation_gitignore_base.txt"
  if [[ -f "$dst" ]]; then
    echo "    preserve $dst"
    return
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "    [dry-run] scaffold $dst from $SANITATION_BASELINE_FILE (comments/blank lines stripped)"
    return
  fi
  mkdir -p "$(dirname "$dst")"
  awk 'NF && $0 !~ /^#/' "$SANITATION_BASELINE_FILE" > "$dst"
}

write_template_hash_manifest() {
  local repo="$1"
  local dst="$repo/.policy/policy_template_hashes.json"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "    [dry-run] generate $dst from baseline scripts (sha256)"
    return
  fi
  "$PYTHON_BIN" - <<PY
import hashlib
import json
from pathlib import Path
repo = Path('$repo')
tracked = [
  'scripts/check_policy_compliance.sh',
  'scripts/check_git_sanitation.sh',
  'scripts/check_policy_template_drift.sh',
]
hashes = {}
for rel in tracked:
    p = repo / rel
    if not p.is_file():
        raise SystemExit(f'ERROR: missing baseline script for hash manifest: {p}')
    hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
out = repo / '.policy' / 'policy_template_hashes.json'
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({
    'policy_version': '$POLICY_VERSION',
    'hash_algorithm': 'sha256',
    'templates': hashes,
}, indent=2) + '\n', encoding='utf-8')
PY
}

sync_repo() {
  local repo="$1"
  if [[ ! -d "$repo/.git" ]]; then
    echo "  skip (not a git repo): $repo"
    return
  fi

  echo "  Syncing $repo"

  if [[ "$DRY_RUN" -eq 0 ]]; then
    mkdir -p "$repo/scripts" "$repo/.policy"
  fi

  write_workspace_policy_version "$repo"
  maybe_scaffold_verify "$repo"
  maybe_scaffold_hash_update_helper "$repo"
  maybe_scaffold_sanitation_baseline "$repo"

  copy_file "$TPL_SANITATION" "$repo/scripts/check_git_sanitation.sh" 755
  copy_file "$TPL_POLICY_COMPLIANCE" "$repo/scripts/check_policy_compliance.sh" 755
  copy_file "$TPL_TEMPLATE_DRIFT" "$repo/scripts/check_policy_template_drift.sh" 755

  write_template_hash_manifest "$repo"

  if compgen -G "$repo/scripts/check_*_repo.sh" >/dev/null 2>&1; then
    for repo_script in "$repo"/scripts/check_*_repo.sh; do
      [[ -e "$repo_script" ]] || continue
      echo "    preserve repo extension script: $repo_script"
    done
  fi
}

if [[ ${#TARGET_REPOS[@]} -eq 0 ]]; then
  echo "Syncing workspace policy baseline $POLICY_VERSION to git repositories under $WORKSPACE_ROOT ..."
  while IFS= read -r -d '' git_dir; do
    sync_repo "${git_dir%/.git}"
  done < <(find "$WORKSPACE_ROOT" -type d -name .git -prune -print0)
else
  echo "Syncing workspace policy baseline $POLICY_VERSION to selected repositories ..."
  for repo in "${TARGET_REPOS[@]}"; do
    sync_repo "$repo"
  done
fi

echo "Sync complete."
