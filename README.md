# Workspace policy-kit 1.2.0

This directory is the local owner of reusable workspace governance. The editable
general mechanism, shared skills and general policy live in
`tools/workspace_governance/`. Project facts, product/design authority and stricter
data or environment constraints remain owned by each consumer. Editing the kit
does not transfer those authorities or automatically activate deferred policy.

`.governance/distribution.json` pins the reviewed active release under
`.releases/1.2.0/`. That immutable release contains its complete manifest and every
declared source byte. It reconstructs the general pack and its legacy baseline;
changing editable sources requires another reviewed release and guarded adoption.
To reconstruct a candidate release, use the general exporter's explicit destination:
`python tools/workspace_governance/release.py --pack tools/workspace_governance
--policy-kit . --output /private/path/release`. Its distribution version defaults to 1.2.0; use an explicit next `--version`
for a later release. Review and pin its result before adoption;
exporting alone does not activate it. The exporter reads the archived 1.1.3 baseline
when present, preserving compatibility bytes across a distribution upgrade.
The distribution version 1.2.0 and pack version are separate identities.

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
