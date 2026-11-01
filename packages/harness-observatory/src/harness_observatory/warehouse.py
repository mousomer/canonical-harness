"""Rebuildable local DuckDB tables and Parquet projections."""

import json
from pathlib import Path

import duckdb

from .model import dumps, validate_bundle

DDL = {
    "runs": "run_id VARCHAR PRIMARY KEY, agent VARCHAR, governance_snapshot_id VARCHAR, ci_poll_count BIGINT, manifest JSON",
    "events": "run_id VARCHAR, seq BIGINT, timestamp TIMESTAMPTZ, event_type VARCHAR, source VARCHAR, data JSON, provenance JSON",
    "governance_snapshots": "run_id VARCHAR, governance_snapshot_id VARCHAR, repo_sha VARCHAR, snapshot JSON",
    "governance_documents": "run_id VARCHAR, governance_snapshot_id VARCHAR, path VARCHAR, sha256 VARCHAR",
    "git_changes": "run_id VARCHAR, kind VARCHAR, commit_sha VARCHAR, path VARCHAR, added BIGINT, deleted BIGINT, provenance JSON",
    "ci_runs": "run_id VARCHAR, ci_run_id VARCHAR, repository VARCHAR, head_sha VARCHAR, conclusion VARCHAR, poll_count BIGINT, record JSON",
}


def build_warehouse(bundles, output, *, temp_directory=None):
    from .data_root import outside_git

    output = outside_git(output)
    if temp_directory is not None:
        temp_directory = outside_git(temp_directory)
        temp_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.exists():
        raise FileExistsError("Warehouse output must be a new directory")
    # Validate all inputs before creating any output; duplicate reconstructions are idempotent.
    unique = {}
    for bundle in bundles:
        validate_bundle(bundle)
        key = bundle["run"]["run_id"]
        if key in unique and dumps(unique[key]) != dumps(bundle):
            raise ValueError("Conflicting reconstructions for one run_id")
        unique[key] = bundle
    output.mkdir(parents=True, mode=0o700)
    with duckdb.connect(
        str(output / "observatory.duckdb"),
        config={"temp_directory": str(temp_directory or output / "tmp")},
    ) as db:
        for table, ddl in DDL.items():
            db.execute(f"CREATE TABLE {table} ({ddl})")

        def insert(table, values):
            db.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in values)})", values)

        with db:
            for b in unique.values():
                r = b["run"]
                insert(
                    "runs",
                    [
                        r["run_id"],
                        r["agent"],
                        r["governance_snapshot_id"],
                        r["facts"]["ci_poll_count"]["value"],
                        dumps(r),
                    ],
                )
                for e in b["events"]:
                    insert(
                        "events",
                        [
                            e["run_id"],
                            e["seq"],
                            e["timestamp"],
                            e["event_type"],
                            e["source"],
                            dumps(e["data"]),
                            dumps(e["provenance"]),
                        ],
                    )
                for s in b["governance_snapshots"]:
                    insert(
                        "governance_snapshots",
                        [r["run_id"], s["governance_snapshot_id"], s["repo_sha"], dumps(s)],
                    )
                    for doc in s["documents"]:
                        insert(
                            "governance_documents",
                            [r["run_id"], s["governance_snapshot_id"], doc["path"], doc["sha256"]],
                        )
                for c in b["git_changes"]:
                    insert(
                        "git_changes",
                        [c[k] for k in ["run_id", "kind", "commit_sha", "path", "added", "deleted"]]
                        + [dumps(c["provenance"])],
                    )
                for c in b["ci_runs"]:
                    insert(
                        "ci_runs",
                        [
                            c[k]
                            for k in [
                                "run_id",
                                "ci_run_id",
                                "repository",
                                "head_sha",
                                "conclusion",
                                "poll_count",
                            ]
                        ]
                        + [dumps(c)],
                    )
            for table in DDL:
                destination = str(output / f"{table}.parquet").replace("'", "''")
                db.execute(f"COPY {table} TO '{destination}' (FORMAT PARQUET)")
    return output / "observatory.duckdb"


def read_bundle(path):
    return json.loads(Path(path).read_text())
