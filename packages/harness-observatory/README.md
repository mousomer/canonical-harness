# Harness Observatory

A local-first evidence system for studying coding-agent harness behaviour. Historical Codex and Claude Code JSONL can be normalized, joined to evidenced Git states and historical governance, correlated with GitHub Actions, and queried with DuckDB or Parquet.

The primary boundary is **software in Git; all real evidence and analysis in an external private data root**. A private GitHub repository does not change that rule. There is no public-export feature, dashboard, service, semantic classifier or automatic governance optimizer.

```text
Private evidence → private normalization → private enrichment → private warehouse → private analysis
Harness Observatory → Governance Evaluation → Governance Optimization
```

A repository/harness manifest describes configuration; a run manifest describes one execution; the event log describes observations. Execution history never belongs in governance manifests.

## Install and verify

Python 3.11+ in the approved existing workspace environment:

```bash
scripts/verify.sh
```

The verifier runs this checkout's declared `src` package and checks application and dependency import origins under the selected Python environment. It replaces ambient import roots with that source directory, so an unrelated or stale installed application cannot supply acceptance. Verification checks the declared dependencies without installing packages or creating an environment.

Verification is offline and synthetic: lint/format checks, adapters and schemas, data-root regressions, CLI diagnostics, Git/governance/DuckDB demonstration, and tracked/index/reachable-history publication scans. Provider inputs are constructed by test code in temporary directories; no serialized transcript fixtures are stored in this repository. No historical sources or Git hook are required.

## Initialize private storage

```bash
harness-observatory init-data-root "$HOME/harness-observatory-data"
export HARNESS_OBSERVATORY_DATA_ROOT="$HOME/harness-observatory-data"
```

Alternatively pass `--data-root PATH` after any processing subcommand. An explicit flag takes precedence over the environment. Without either, processing fails with setup instructions before reading input. No default directory is inferred from the current working directory.

Initialization accepts an empty or new external directory, gives it a local corpus identifier, and creates:

```text
raw/{codex,claude,github}/
normalized/{runs,events}/
enriched/{git,governance,ci}/
warehouse/parquet/
identity/mappings/
analysis/{intermediate,results}/
logs/
tmp/
```

The local marker contains the corpus ID. Its location and your environment provide the private ID-to-path mapping; neither an actual configured path nor corpus ID belongs in Git. Initialization never copies evidence or initializes Git. Existing initialized roots are validated and reused; unmarked nonempty directories, home/filesystem roots, Git worktrees and bare repositories are refused.

## Private workflow

```bash
harness-observatory inspect-source codex "$SOURCE_SESSION"
harness-observatory extract codex "$SOURCE_SESSION" --output normalized/runs/example.json
harness-observatory enrich-git normalized/runs/example.json --repo "$SOURCE_REPOSITORY" \
  --document docs/VERIFICATION.md --output enriched/git/example.json
# Operator-provided --start-sha / --end-sha require --evidence REFERENCE.
harness-observatory enrich-ci enriched/git/example.json --repository OWNER/REPO \
  --actions-json "$ACTIONS_SOURCE" --output enriched/ci/example.json
harness-observatory build-warehouse enriched/ci/example.json
```

`extract`, `enrich-git`, `enrich-ci` and `build-warehouse` generate content/run-based output names when `--output` is omitted. Explicit outputs must remain inside the initialized root. Relative bundle inputs and outputs are resolved against that root, never the current directory. Original transcript and Actions inputs may be read from their original external locations.

Extraction preserves an independently identifiable, content-addressed, read-only raw copy under the root, then writes canonical JSON separately. Original evidence is never modified. Derived outputs are new-only; identical raw copies are reused after checking their contents. Warehouse builds create their own subdirectory with the database and six Parquet tables. DuckDB spills go under the private tmp directory. Operation records under logs/ carry corpus/schema/extractor/enrichment versions; inspection findings also stay there. Console diagnostics contain bounded codes, not source values.

All destinations are checked against **any Git worktree**, including linked worktrees and unrelated repositories. Checks resolve symlinks, parent traversal, missing final components and filesystem identity for case aliases. A symlink inside the corpus cannot route output outside it or into a nested Git repository. Merely naming a directory private/ or generated/ never makes it acceptable. The guard assumes no concurrent malicious filesystem substitutions.

Normal private CLI processing retains supported identifiers without requiring pseudonymization or public-safe sanitization. Existing adapters still omit message/tool body columns from their bounded canonical projection; the raw copy preserves those bytes. INTERNAL_METADATA, PRIVATE_ANALYTICAL, RAW_PRIVATE and SECRET all remain outside Git. These labels inform future access policy, not commit eligibility. Direct pure-library consumers must respect the same corpus policy; the CLI and warehouse writer enforce the storage checks described here.

## Demonstration and later smoke

```bash
python scripts/demo.py
```

The demonstration uses synthetic fixtures, temporary Git commits, historical governance hashing and actual DuckDB/Parquet tables. It answers:

```sql
SELECT agent, governance_snapshot_id, COUNT(*) AS runs,
       AVG(ci_poll_count) AS avg_ci_polls
FROM runs
GROUP BY agent, governance_snapshot_id;
```

After the software landing passes verification, a bounded smoke can use explicitly supplied external source directories:

```bash
python scripts/smoke_local.py --codex-root "$CODEX_SOURCE_ROOT" --claude-root "$CLAUDE_SOURCE_ROOT"
```

It selects at most ten files per provider. Raw copies, normalized/enriched records, warehouse and the report remain exclusively in the data root. It performs no bulk backfill and prints no corpus statistics. Check `git status`, `git ls-files` and `the repository publication gate` afterward; no derived values should enter the software repository.

See [architecture](docs/architecture.md), [data model](docs/data-model.md), [qualitative capabilities](docs/source-capability-matrix.md), [prior art](docs/prior-art.md) and [backlog](docs/BACKLOG.md). Analysis code belongs in Git; real results, output-bearing notebooks, identity mappings and local configuration do not. Semantic labeling, bulk backfill, statistics, live telemetry, OpenTelemetry, governance optimization and public export are later milestones.

The offline routing-analysis script requires an explicit `--cwd` argument for
each selected working directory; repeat it for additional directories. Report
version `routing-analysis-1.1` derives totals from those private inputs and omits
fixed historical comparisons. Keep runtime reports outside Git.
