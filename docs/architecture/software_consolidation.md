# Canonical harness software ownership

The canonical source repository owns the existing governance runtime, release
controls and portable verification, plus the separately packaged Observatory
library and CLI under `packages/harness-observatory/`. The parent testing
workspace supplies no additional application: its generated governance
scaffolding is not a second canonical implementation.

## Publication boundary

Only explicitly reviewed software belongs here: source, schemas, packaging,
software design/usage documentation and test code. Provider test inputs are
generated synthetically in invocation-owned temporary directories. No existing
JSONL fixture, corpus, raw transcript, normalized/enriched record, warehouse,
run result, report, plot, identity mapping, corpus identity, private state,
machine overlay, receipt, log or cache is imported. No old repository Git
history is merged. Original source locations and external data remain intact.

Publication checks cover the worktree, index and newly reachable history,
including commit/tag messages. All source metadata remains subject to content
checks; a filename extension or an ignored directory is not a safety exception.
Limits and input errors reject rather than silently skip coverage.

## Component and authority boundaries

`tools/workspace_governance/` remains the sole portable governance mechanism.
Observatory retains its library/CLI identity, schemas, provenance semantics,
external non-Git data-root rules and component-specific design authorities.
Its dependency and source-import checks remain independent of the governance
runtime. Observatory is not installed into consumer repositories and does not
become an implicit dependency of the shared pack.

Existing immutable releases and logical publisher identity remain unchanged.
The source consolidation does not alter release 1.4.0, activate telemetry
importers or private analyses, or establish any consumer product acceptance.
Changed shipped components require their own reviewed successor publication.

## Migration and acceptance

Import an explicit software inventory from the current source, including
reviewed uncommitted code, while retaining stronger component instructions.
Omit redundant vendored runtime copies and machine-generated dispatch metadata.
Reconcile software documentation without copying private experiment outcomes.
Add a repository verifier composing the unchanged kit gate, publication checks
and Observatory's complete synthetic verifier.

Acceptance requires source/dependency origin checks, CLI and schema loading,
synthetic extraction/enrichment/warehouse tests, temporary-storage and Git-root
boundary regressions, source-only inventory checks and bounded publication
scans. Verify isolated publication trees rather than relying on an unrelated
installed package or a different dirty working tree. Consumer updates are
separate, explicitly owner-approved migrations; all other repositories retain
read-only preservation in the private registry.
