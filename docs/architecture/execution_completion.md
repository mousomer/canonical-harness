# Shared execution completion contract

The shared governance pack owns completion-based execution for local tests,
scripts and remote CI. MANIFEST.json binds policies/execution.json with the
synchronous-completion-v1 identity. Runtime 0.8.0 requires the binding and its
complete requirements; missing, weakened and foreign/symlinked policy bytes
fail validation even if a caller regenerates the ordinary pack lock.

The resolver exposes execution.completion with shared-pack ownership, canonical
source, policy version and content hash. Both governance skills reference the
same human-readable authority. Consumer schemas already reject undeclared local
policy overrides; stricter local requirements retain their existing authority.
Older releases remain immutable and do not claim this capability.

The existing bounded execution runner remains the process owner. Internal
process checks, pipe draining and blocking CLI watches are permitted. A yielded
tool resumes the same invocation. No new background watcher or telemetry service
is installed. Explicit normalized observations can be reviewed by the pure
execution-policy checker; unknown or incomplete traces cannot certify success.
Tests cover policy corruption, resolver provenance, yielded handles, duplicate
watchers, status/log polling, remote identity changes, failure propagation and
unknown completion. Synthetic traces are evidence of checker behavior, not proof
of unobserved agent behavior.

Release through the guarded immutable distribution path, then saved fleet plans.
Keep protected stores and historical evidence unchanged. Missing/stale product
verification remains an audit gap; installing policy alone does not certify it.
