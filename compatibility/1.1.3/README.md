# Policy Kit

Reusable workspace policy baseline for repositories under `${HOME}/workspace`.

Current standard: `1.1.x` (see `POLICY_VERSION.json`).

## What This Kit Provides

- Baseline policy scripts (template-aligned files copied into repos):
  - `scripts/check_policy_compliance.sh`
  - `scripts/check_git_sanitation.sh`
  - `scripts/check_policy_template_drift.sh`
- Verification wrapper template:
  - `templates/verify_template.sh`
- Policy manifest templates:
  - `templates/policy_manifest.json.template`
  - `templates/policy_template_hashes.json.template`
- Workspace sync/audit helpers:
  - `scripts/sync_workspace_policy.sh`
  - `scripts/audit_workspace_policy.sh`
  - `scripts/check_policy_kit_selftest.sh`
- Repo maintenance helper template:
  - `scripts/update_policy_template_hashes_template.sh`
- Baseline sanitation ignore rules:
  - `sanitation_baseline.gitignore`
- Baseline sanitation script behavior: git-scoped, binary-aware scans (warn-only) to avoid ignored/untracked tree noise such as local `.venv/`.
- Optional shared architecture-metrics pack:
  - `optional/architecture_metrics/architecture_metrics/`
  - class-aware balancer helpers (`code_default`, `tests_lenient`, `non_code_exempt`)
  - tech-debt score and gate evaluators for soft-gate rollout across repos

## Policy Metadata Schema (1.1.x)

Repositories should carry two policy JSON files:

1. `.workspace_policy_version.json`
2. `configs/policy_manifest.json` (or equivalent supported path)

### Standard metadata fields

- `policy_version`: policy-kit standard version (for example `1.1.0`)
- `policy_id`: stable policy identity (`workspace-github-sanitation-and-security`)
- `policy_source`: stable logical source label (for portability), typically `workspace/policy-kit`
- `policy_source_path`: local path hint for the installed policy kit (default placeholder `${HOME}/workspace/policy-kit`; may be overridden with an absolute path)

### Policy manifest fields (recommended)

- `verification_command`: repository verification entrypoint (typically `./scripts/verify.sh`)
- `ci_entrypoint`: CI job entrypoint command for the repo
- `design_sources`: canonical design sources (RDS/ADR/docs) that govern implementation changes
- `secret_scan`: canonical secret-scan command for local/CI use
- `enforcement`: baseline scripts expected to remain template-aligned
- `extended_enforcement`: repo-specific stricter checks (for example `*_repo.sh`)

Optional repo-specific governance fields may extend this set (for example `contract_validation` in repos with a dedicated contract validator).

This split allows repos to preserve stronger local checks without drifting baseline templates.

### What Not To Standardize (Usually Repo-Local)

- `contract_validation`: keep repo-local unless a repo has a dedicated contract/maintenance validator
- deep content-rule contracts that pin shared template script text (use template-drift hashes instead)

## Baseline vs Repo Extensions

Recommended repository model:

- Baseline scripts (template-identical to this kit):
  - `scripts/check_policy_compliance.sh`
  - `scripts/check_git_sanitation.sh`
  - `scripts/check_policy_template_drift.sh`
- Repo extensions (project-specific):
  - `scripts/check_policy_compliance_repo.sh`
  - `scripts/check_git_sanitation_repo.sh`
  - other repo checks (for example structure enforcement)
- Wrapper:
  - `scripts/verify.sh` calls baseline checks, template-drift checks, repo extensions, and project verification tasks

## Template-Drift Workflow

Purpose: ensure baseline scripts in repos remain byte-identical to policy-kit templates even when repos add extension checks.

### Required repo artifacts

- `.policy/policy_template_hashes.json`
- `scripts/check_policy_template_drift.sh`

### Hash manifest format

- `policy_version`: version of the policy standard the hashes correspond to
- `hash_algorithm`: currently `sha256`
- `templates`: map of repo baseline script paths to expected SHA-256 hashes

### Update process (after policy-kit baseline template changes)

1. Sync baseline scripts into the repo from policy kit.
2. Regenerate the repo hash manifest (for example via repo helper `scripts/update_policy_template_hashes.sh`).
3. Run the repo verification wrapper (`./scripts/verify.sh`).
4. Confirm template drift check passes.

## Sync Helper (`scripts/sync_workspace_policy.sh`)

The sync helper rolls out baseline artifacts while preserving repo-specific extensions.

### Key behavior

- Updates `.workspace_policy_version.json` with `policy_version`, `policy_id`, source metadata, and `synced_at`
- Installs/updates baseline scripts:
  - `check_git_sanitation.sh`
  - `check_policy_compliance.sh`
  - `check_policy_template_drift.sh`
- Generates `.policy/policy_template_hashes.json` with current SHA-256 hashes
- Preserves:
  - existing `scripts/verify.sh`
  - existing `scripts/update_policy_template_hashes.sh`
  - existing `.policy/sanitation_gitignore_base.txt`
  - any `scripts/check_*_repo.sh` files

### Usage examples

Sync all repos under `${HOME}/workspace`:

```bash
${HOME}/workspace/policy-kit/scripts/sync_workspace_policy.sh
```

Dry run for one repo:

```bash
${HOME}/workspace/policy-kit/scripts/sync_workspace_policy.sh \
  --repo ${HOME}/workspace/raizor/rmbg-scores \
  --dry-run
```

Sync one repo with explicit paths:

```bash
${HOME}/workspace/policy-kit/scripts/sync_workspace_policy.sh \
  --repo /abs/path/to/repo \
  --policy-kit-dir /abs/path/to/policy-kit
```

## Audit Helper (`scripts/audit_workspace_policy.sh`)

Audits repos for required baseline artifacts (version file, baseline scripts, template drift script, hash manifest, and hash-update helper).

Use it for workspace-level visibility before/after policy upgrades.

## Policy-Kit Self-Test (`scripts/check_policy_kit_selftest.sh`)

Runs a local smoke test for the policy kit itself:

- validates expected files/templates exist
- parses JSON manifests/templates
- checks `required_files_manifest.json` contains expected baseline repo artifacts
- validates `sync_workspace_policy.sh --help`
- provisions a temporary fixture git repo under `/tmp`
- runs sync helper dry-run + apply against the fixture
- verifies baseline artifacts are created and repo extension scripts are preserved
- runs generated baseline drift/hash helper checks in the fixture

Run it after changing policy-kit templates or sync logic:

```bash
${HOME}/workspace/policy-kit/scripts/check_policy_kit_selftest.sh
```

## Versioning and Rollout Guidance

When changing baseline templates or required artifacts:

1. Update `POLICY_VERSION.json`
2. Update `required_files_manifest.json`
3. Update templates/scripts in this kit
4. Run `scripts/sync_workspace_policy.sh --dry-run` on a representative repo
5. Sync target repos and run each repo’s `./scripts/verify.sh`
6. Commit repo updates and document any repo-specific extension changes

Rollout/sync result reports should include: file list, short summary, diffstat, and a LOC summary (added/deleted lines).

## Notes

- `policy_source` should remain portable (logical label).
- `policy_source_path` defaults to the portable placeholder `${HOME}/workspace/policy-kit`; custom installs may use an absolute override path.
- This kit standardizes baseline policy enforcement; repositories may add stricter checks in `extended_enforcement`.
