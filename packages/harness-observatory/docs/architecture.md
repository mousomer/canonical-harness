# Architecture v0.1

Design authority: this document and data-model.md. Scope: local, reproducible evidence foundations for the modern AI harness programme.

L0 private immutable sources → provider adapters → private L1 canonical JSON events and run manifest → private L2 mechanical facts → private DuckDB and Parquet. L3 semantic labels are reserved for later. No server, live service, semantic classifier, governance rewriting or bulk importer.

One input session file is one reconstructed run. IDs derive from provider and source bytes; append-only changes create a new reconstruction. Evidence references use source SHA-256 plus line number, never raw source paths. All reconstructed facts carry source, method, confidence, references and extractor version. Null means unknown; empty collections mean no observations in this reconstruction, not proof of absence. Missing end state stays missing. Recorded command invocation is distinct from successful execution.

Git enrichment requires an explicit historical starting commit, either recorded in Codex metadata or supplied with evidence by the operator. Ending commit is optional and must be separately evidenced. No timestamp-to-HEAD guessing. Commit ranges describe changes between endpoints; they do not establish agent authorship. Dirty/uncommitted work is unavailable. Governance is hashed from Git objects at the starting commit, with all nested AGENTS.md and an explicit additional document list. Optional missing documents are recorded. This is a reproducible candidate document set, not a complete instruction-precedence resolver. External bundles must be vendored at that revision or separately captured in a future adapter.

Actions enrichment consumes saved REST run metadata or read-only `gh api` results. Joins require exact commit SHA and an operator-selected repository. Poll recognition covers a leading explicit gh command, not quoted examples or arbitrary shell orchestration. It covers explicit `gh run view ID` and REST `actions/runs/ID` queries; broad lists and opaque nested orchestration remain unsupported. A query proves an attempted observation, not agent detection of completion. REST updated_at is retained separately from exact completion time.

Normal CLI ingestion retains supported identifiers inside the private corpus and does not promise public-safe output. The existing bounded canonical projection omits free-text bodies; content-addressed raw copies preserve evidence independently. Legacy pure-library callers may still request secret redaction, but that does not grant publication eligibility. The initialized external storage boundary is primary, with repository scans as defense in depth.

The canonical schema is authoritative. Parquet/DuckDB are rebuildable projections; future OpenTelemetry export adapts from the canonical model and does not dictate it. Repository/harness manifests declare configuration; run manifests describe executions; event logs describe observations. Execution history never belongs in governance manifests. No shared manifest schema change is needed for this landing.

Verification: scripts/verify.sh exercises lint, formatting, schemas, adapters, security boundary, historical Git joins and a synthetic end-to-end SQL demonstration.

The offline source verifier binds its import path only to this checkout's declared `src` directory. It checks that the application resolves from that source and that verifier/dependency packages resolve from the selected Python environment before running the complete existing gate. Ambient import roots and stale installed application copies cannot replace the checked source. Dependency installation remains a separate setup step; verification constructs synthetic inputs only in temporary directories.

Publication safety scans Git-tracked worktree and index content plus trees reachable from all refs and HEAD. Unreachable objects and reflogs are not publication inputs. Diagnostics report categories and object/path locations without matched content. CLI validation and subprocess errors emit bounded codes. No hooks or private sources are required for verification.

The output guard discovers Git ancestry directly from physical parent directories, gitfiles and Git discovery independent of caller Git environment overrides. Installed packages do not need to locate their source checkout. This is a local storage boundary, not a race-proof filesystem sandbox.

## Private data-root landing

The primary storage contract is software-only Git and an initialized external private corpus. The CLI resolves --data-root before HARNESS_OBSERVATORY_DATA_ROOT; absence fails with setup instructions. Initialization creates a local corpus identifier and raw, normalized, enriched, warehouse, identity, analysis, logs and tmp directories, without reading or copying evidence. Roots and every output destination are checked against any Git worktree, including gitfiles, linked worktrees, canonical aliases and missing components. Bare Git repositories are also refused. Symlinked descendants may not escape the corpus. No override is provided.

Every data-processing command validates the root before reading evidence. Outputs default to deterministic names beneath that root; explicit outputs are root-relative or absolute within the root. Raw inputs are read-only and may remain in their original external locations. A corpus manifest preserves the local root identifier and schema/extractor/enrichment versions beside derived outputs. Runtime reports and temporary processing files belong in logs/ and tmp/ under the root; no automatic file logger writes into the checkout. DuckDB spill files use the private tmp directory.

INTERNAL_METADATA, PRIVATE_ANALYTICAL, RAW_PRIVATE and SECRET describe future access/export needs. All remain outside Git. Existing extractors still project a bounded set of fields and omit unsupported text; this landing does not add transcript columns or public export. Normal CLI normalization/enrichment preserves the supported identifiers without an anonymization requirement. Original source bytes can be preserved independently under raw/ for reproduction. Publication scans are defense in depth, not approval to publish analysis.

Acceptance: synthetic repositories exercise source/linked/other Git roots, symlinks, traversal, case aliases where supported, explicit/environment resolution, safe initialization, scoped output and warehouse temp placement. Only after the software landing and clean verification may a bounded local smoke command preserve a small sample and write its report wholly under the root. No bulk ingestion or source-derived documentation changes are part of this landing.

## Reconstruction programme

The next reconstruction programme is split into independently verified landings. Landing A caches immutable schema validators and filesystem-state-keyed Git boundary probes, retains bounded provider record types in warnings, and normalizes recorded repository identifiers without changing schema 0.1. Later schema and governance changes must land coherently before census processing. Every census is a single deadline-bounded, restartable process with private checkpoints; agent-driven polling is prohibited.

Governance discovery version 2 reads only the selected historical Git tree. It inventories `AGENTS.md`, `CLAUDE.md`, and explicitly routed documents, then marks candidates applicable only when their directory lies on the root-to-cwd ancestry (explicit routes are applicable by declaration). Snapshot identity hashes the discovery version, scope, state, document content/paths/applicability, and optional absences. A changed discovery algorithm therefore cannot reuse an older identity.

The run-level governance state is `none_existed` when the historical tree has no governance candidates, `not_applicable` when candidates exist but none governs the recorded scope, `reconstructed` when applicable documents were recovered, and `reconstruction_failed` when the starting commit or repository-relative cwd cannot be established. A failed reconstruction has no fabricated snapshot. Current checkout files never substitute for historical tree content.

The census runner enumerates both provider roots once, processes the population internally, checkpoints after a bounded number of sessions, and enforces a monotonic 30-minute deadline. Versioned content-addressed normalized and governance outputs are validated and skipped on resume; conflicting identities fail closed. Warehouse output uses a new census/version directory and is never overwritten. Checkpoints contain phase/version/HEAD/times/status/counts/outputs/next phase under the private root. They deliberately do not enter Git or terminal context.

Landing D is a private boundary parser, versioned as extractor 0.3.0 and poll definition 2. It pairs tool results only with a small recognized Git/CI command set, scans a bounded prefix, emits unambiguous structured facts with provenance, and never places the surrounding body in an analytical event. Its outputs use new version directories; Existing private census outputs remain immutable.

Review utilities select at most 20 reconstructions across deterministic evidence strata, inventory every structured CI observation in a selected warehouse, and place reports and any cached metadata under the initialized private data root. CI metadata requests are deduplicated, individually timeout-bounded, retry-bounded, cached, and governed by an overall deadline.
Offline mode performs the complete evidence inventory without transmitting repository or run identifiers and consumes only previously cached metadata.

## Private routing-analysis extension

The separately invoked routing_analysis.py is an offline, opt-in private analytical projection, not a canonical schema change. It freezes source byte extents, preserves content-addressed snapshots, and writes SQLite plus CSV only under an initialized external DataRoot. Literal single-site wrappers are mapped to their existing invocation ID; dynamic and multi-site wrappers remain UNKNOWN with source-site evidence. Recorded invocation does not prove execution. No production repository writes or routing experiments are permitted.

### Routing measurement repair v2

The repair consumes only an immutable, hash-verified inventory derived from an existing checkpoint. It never discovers live inputs. A separate action graph distinguishes source containers, candidate nested invocations, measured results and continuation observations; only evidenced ownership can allocate output to a leaf action. JavaScript and shell syntax are parsed without executing historical code. Candidate Git states, injected instruction observations and uncertain transitions remain distinct. Acceptance requires a stratified raw audit, disclosed coverage/error intervals, at least 99% leaf-output byte attribution and at least 95% precision/recall for relevant operation classes; passing software tests alone is insufficient. The run stops after acceptance or rejection, with no route design or production mutation.
