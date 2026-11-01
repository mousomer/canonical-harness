"""Mechanically compare versioned census warehouses; write only to the private root."""

import argparse
from datetime import datetime, timezone

import duckdb

from harness_observatory.data_root import resolve_data_root


def metrics(path):
    with duckdb.connect(str(path), read_only=True) as db:
        rows = db.execute(
            """
            SELECT agent,
                   count(*) AS runs,
                   count(*) FILTER (WHERE json_extract_string(manifest, '$.facts.start_sha.value') IS NOT NULL) AS start_sha,
                   count(*) FILTER (WHERE json_extract_string(manifest, '$.facts.end_sha.value') IS NOT NULL) AS end_sha,
                   sum(ci_poll_count) AS polls,
                   count(*) FILTER (WHERE governance_snapshot_id IS NOT NULL) AS governance_snapshots
            FROM runs GROUP BY agent ORDER BY agent
            """
        ).fetchall()
        ci = dict(
            db.execute(
                "SELECT event_type, count(*) FROM events WHERE event_type LIKE 'ci.%' GROUP BY event_type"
            ).fetchall()
        )
    return {
        "by_provider": [
            {
                "provider": row[0],
                "runs": row[1],
                "start_sha": row[2],
                "end_sha": row[3],
                "polls": row[4],
                "governance_snapshots": row[5],
            }
            for row in rows
        ],
        "ci_event_counts": ci,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root")
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--output", default="analysis/intermediate/census-1-to-2.json")
    args = parser.parse_args()
    root = resolve_data_root(args.data_root)
    before = metrics(root.destination(args.before))
    after = metrics(root.destination(args.after))
    report = {
        "schema_version": "Extractor-version differences are reconstruction changes, not corpus changes.",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "before": before,
        "after": after,
    }
    root.write(args.output, report)
    print("Census delta report written under the private data root.")


if __name__ == "__main__":
    main()
