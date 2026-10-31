# Workspace policy-kit 1.4.0

This directory is the local owner of reusable workspace governance. The editable
general mechanism, shared skills and general policy live in
`tools/workspace_governance/`. Project facts, product/design authority and stricter
data or environment constraints remain owned by each consumer. Editing the kit
does not transfer those authorities or automatically activate deferred policy.

`.governance/distribution.json` pins the reviewed active release under
`.releases/1.4.0/`. That immutable release contains its complete manifest and every
declared source byte. It reconstructs the general pack and its legacy baseline;
changing editable sources requires another reviewed release and guarded adoption.
To reconstruct a candidate release, use the general exporter's explicit destination:
`python tools/workspace_governance/release.py --pack tools/workspace_governance
--policy-kit . --output /private/path/release --version 1.4.0
--verification-root /private/path/verification-component
--distribution-root /private/path/distribution-component`. Stage the complete test
and public control file sets into those component roots first. The exporter
retains its legacy API default; provide the reviewed successor version explicitly. Review and pin its result before adoption;
exporting alone does not activate it. The exporter reads the archived 1.1.3 baseline
when present, preserving compatibility bytes across a distribution upgrade.
The distribution version 1.4.0 and pack version are separate identities.

`compatibility/1.1.3/` preserves all 19 captured original source files byte for
byte, with modes and hashes recorded in `manifest.json`. Existing template paths
and optional architecture metrics remain available as legacy 1.1.3 interfaces.
The compatibility baseline does not claim to enforce the full active pack.

The script names remain stable, with explicit guarded actions:

```sh
./scripts/sync_workspace_policy.sh plan
./scripts/sync_workspace_policy.sh apply --plan /private/path/plan.json --receipt /private/path/receipt.json
./scripts/sync_workspace_policy.sh undo --receipt /private/path/receipt.json
./scripts/sync_workspace_policy.sh verify
./scripts/audit_workspace_policy.sh
./scripts/check_policy_kit_selftest.sh
```

Sync defaults to planning; `--dry-run` is a planning alias. Blind recursive
discovery/copy and legacy `--repo`, `--workspace-root` or `--policy-kit-dir` writes
are retired. Repository selection and adoption mode come from the explicit
private fleet registry. `verify --checkout <registry-id>` bounds verification.
An apply requires a saved plan matching the configured registry path/hash and
active release, plus a fresh receipt destination; undo refuses to overwrite
intervening edits. None of these scripts commits or publishes changes.

`_machine/distribution.local.json` is mode 600 machine configuration. It points
to the private registry, receipts, plans and verification evidence outside
consumer repositories and selects the existing shared governance interpreter.
The active wrappers execute the pinned release's fleet code after byte checks.
Do not put this machine file or private registry into a public Git repository.

All discovered locations must have an explicit direct, inherited, external or
blocked disposition. External/read-only declarations protect tool-managed
stores, dependencies, backups, fixtures and worktrees from installer writes.
They still require a release pin, owner policy and observed identity. Stronger
local instructions and protected input constraints continue to apply.

Audit reads the configured verification evidence and checks registry identities,
release bytes, hooks and consumer governance without starting product runners.
Use explicit `verify --checkout <registry-id>` to collect approved product evidence.
Private `blocked_execution` entries refuse a selected or all-repository verify
before any runner starts. IQA-sort currently requires a separate owner-reviewed
execution decision because its canonical runner may download model weights.
A missing
product verifier, governance-only coverage, stale evidence, blocked/orphan
location or unresolved scan gap produces a nonzero audit. A successful installer
or metadata check alone is insufficient product verification. The bounded pack
secret scanner does not prove ignored files, history, binary/oversized files or
unreadable locations secret-free; those limits remain explicit evidence duties.

Selftest checks the immutable release, editable general pack, complete 1.1.3
archive, active metadata and discovery links. It does not execute consumer
verification or claim whole-computer coverage. The archived 1.1.3 selftest remains
historical source; its old blind-copy fixture is not the active distribution gate.

The 1.3 release has separately hashed runtime, verification and distribution
control components. Consumers receive only the runtime payload. Public-safe
source, tests and publication tooling have independent Git history; `_machine/`,
private evidence, telemetry and keys remain excluded. Operational fleet state is
permanent private state, with an independently verified backup and restoration
receipt. The original session copy and original release remain retained. Copies
on one device provide no device-loss protection.

The Make runner ordering contract is defined in `MAKE_VERIFICATION_DESIGN.md`.
It guards the explicit verification recipe graph with order-only edges. Opaque
ordinary-build prerequisites and optional dependency includes require explicit
reviewed runner declarations; unsupported verification graphs are rejected.
Source repair and release/adoption duties are tracked in `OPEN_WORK.md`.

The full kit gate is distinct from the quick integrity selftest:

```sh
./scripts/verify_policy_kit.sh
./scripts/verify_policy_kit.sh --candidate /absolute/path/to/policy-kit
./scripts/sync_workspace_policy.sh baseline --checkout <registry-id>
./scripts/sync_workspace_policy.sh verify --checkout <registry-id>
```

The full gate defaults to active immutable code and separately pinned tests.
Editable code needs explicit candidate selection. It checks the declared Python,
pytest, packaging, pinned Ruff, Git and Bash dependencies without installing packages or
creating an environment. Selected origins, schemas, fixtures, modes and hashes
are validated. Ambient Python startup/import paths and pytest plugins cannot
substitute developer checkout code. Ordinary context commands stay bounded.
The gate runs the selected runtime's Ruff lint/C901 and format checks plus the
selected test suite, retaining per-phase outcomes and bounded structural
diagnostics. A passing test suite cannot mask a failed style phase. Dependency
origins must belong to the selected interpreter's installed library directories.
Top-level output hashes and test counts describe pytest; per-phase records retain
each tool's full output measurements and the overall status covers every phase.

Default audit reads current collector-reported evidence and bounded structural
observations. It does not silently refresh a baseline or start products. Baseline
refresh explicitly runs the declared governance runner with the same approved
execution policy and process-group timeout as collection. The v2 evidence
envelope binds completed runs, declared selections, registry/release identities,
actual argv, outcomes and stable inputs. Mixed-result batches retain both passing
and failing records. The trust model is collector-reported execution; a same-user
actor who can rewrite the collector and evidence remains outside authentication
guarantees. No local readable signing key changes that boundary.

Global skill discovery targets active immutable skill directories. Each consumer
adapter declares name, link, canonical body hash and fleet release identity, and
ordinary `gov check` validates that relationship. Installer ownership covers
reviewed machinery and dispatch sections; mutable human authorities, backlog and
current-state text are not frozen historical postimages. Declared local relative
CLAUDE-to-AGENTS aliases are preserved and checked without following writes.

`gov telemetry status --json` reports configuration and the last observed signed
append/readback readiness, including store identity and observation time. It does
not infer current writability. `gov telemetry selftest --json` performs an explicit
private synthetic write probe; disabled telemetry writes nothing. Probe results
never establish product evidence. Recording failures preserve the observed
command's status. P1b/P1c/P6 and global automatic logging remain gated.

Machine tool paths remain in the private execution declaration. Public fleet
metadata retains the portable verifier declaration and binds the complete
private execution policy with its version, private-registry source and SHA-256.
A policy change invalidates that binding while execution consumes the original
private declaration. Releases 1.2.0, 1.3.0, 1.3.1 and 1.3.2 remain retained and immutable.

A declared retired temporary external store can carry a release-bound observed-absence lifecycle. Audit reports `governed_absent` only after a fresh absence observation and rejects unexpected reappearance. This records preservation and absence; it never certifies adoption, product behavior or missing scan coverage.
