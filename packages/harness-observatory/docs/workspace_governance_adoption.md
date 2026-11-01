# Observatory governance integration

This package is a component of Canonical Harness, not an independently installed
fleet consumer. The repository root owns the single governance runtime and
immutable release controls. The package retains its own software architecture,
data-model authority, source/dependency checks and external non-Git storage rules.

Run the repository aggregate verifier for complete acceptance, or the component
verifier for its synthetic application checks and the shared publication gate.
No vendored governance runtime, machine overlay or local fleet record is imported
into this package. Source consolidation authorizes no collection, backfill,
private analysis, telemetry activation or consumer product acceptance.
