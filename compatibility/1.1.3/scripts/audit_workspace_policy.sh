#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-${HOME}/workspace}"
POLICY_VERSION_FILE="${POLICY_VERSION_FILE:-${HOME}/workspace/policy-kit/POLICY_VERSION.json}"

echo "Auditing workspace policy compliance..."

for repo in $(find "$WORKSPACE_ROOT" -maxdepth 1 -type d); do
    if [ -d "$repo/.git" ]; then
        echo "Checking $repo"
        if [ ! -f "$repo/.workspace_policy_version.json" ]; then
            echo "  Missing .workspace_policy_version.json"
        fi
        if [ ! -f "$repo/scripts/verify.sh" ]; then
            echo "  Missing verify script"
        fi
        if [ ! -f "$repo/scripts/check_git_sanitation.sh" ]; then
            echo "  Missing sanitation script"
        fi
        if [ ! -f "$repo/scripts/check_policy_compliance.sh" ]; then
            echo "  Missing policy compliance script"
        fi
        if [ ! -f "$repo/scripts/check_policy_template_drift.sh" ]; then
            echo "  Missing policy template drift script"
        fi
        if [ ! -f "$repo/.policy/policy_template_hashes.json" ]; then
            echo "  Missing policy template hash manifest"
        fi
        if [ ! -f "$repo/scripts/update_policy_template_hashes.sh" ]; then
            echo "  Missing policy template hash update helper"
        fi
    fi
done

echo "Audit complete."
