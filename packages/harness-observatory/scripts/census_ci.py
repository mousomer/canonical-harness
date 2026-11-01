"""Inventory the full private CI subset and cache bounded GitHub metadata."""

import argparse
import json
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

import duckdb

from harness_observatory.data_root import resolve_data_root
from harness_observatory.model import digest


def github_slug(value):
    if not value:
        return None
    text = value.removesuffix(".git")
    if text.startswith("git@github.com:"):
        text = text.split(":", 1)[1]
    elif "github.com" in text:
        text = urlparse(text if "://" in text else "https://" + text).path.strip("/")
    else:
        return None
    parts = text.strip("/").split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 else None


def fetch(slug, run_id, deadline):
    for attempt in range(3):
        if time.monotonic() >= deadline:
            return None, "deadline"
        try:
            result = subprocess.run(
                ["gh", "api", f"repos/{slug}/actions/runs/{run_id}"],
                capture_output=True,
                text=True,
                timeout=min(30, max(1, deadline - time.monotonic())),
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result and result.returncode == 0:
            try:
                value = json.loads(result.stdout)
                return {
                    k: value.get(k)
                    for k in ("id", "status", "conclusion", "head_sha", "created_at", "updated_at")
                }, "fetched"
            except json.JSONDecodeError:
                pass
        if attempt < 2:
            time.sleep(min(2**attempt, max(0, deadline - time.monotonic())))
    return None, "unavailable"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root")
    parser.add_argument("--warehouse", required=True)
    parser.add_argument("--deadline-seconds", type=int, default=1800)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Inventory all CI evidence and use cache only; make no network requests.",
    )
    parser.add_argument("--output", default="analysis/results/ci-census.json")
    args = parser.parse_args()
    if not 1 <= args.deadline_seconds <= 3600:
        parser.error("deadline must be between 1 and 3600 seconds")
    root = resolve_data_root(args.data_root)
    warehouse = root.destination(args.warehouse)
    with duckdb.connect(str(warehouse), read_only=True) as db:
        types = dict(
            db.execute(
                "SELECT event_type, count(*) FROM events WHERE event_type LIKE 'ci.%' GROUP BY event_type ORDER BY event_type"
            ).fetchall()
        )
        rows = db.execute("""
            SELECT DISTINCT e.run_id, json_extract_string(e.data, '$.ci_run_id'),
                   json_extract_string(r.manifest, '$.facts.repository.value')
            FROM events e JOIN runs r USING (run_id)
            WHERE e.event_type LIKE 'ci.%'
        """).fetchall()
    deadline = time.monotonic() + args.deadline_seconds
    counts = Counter(observations=sum(types.values()), sessions=len({r[0] for r in rows}))
    pairs = sorted(
        {(github_slug(repo), str(ci_id)) for _, ci_id, repo in rows if ci_id},
        key=lambda pair: (pair[0] or "", pair[1]),
    )
    counts["unique_run_ids"] = len(pairs)
    for slug, ci_id in pairs:
        if not slug:
            counts["repository_unavailable"] += 1
            continue
        key = digest(f"{slug}:{ci_id}".encode()).split(":", 1)[1]
        relative = f"enriched/ci/v1/{key}.json"
        if root.destination(relative).exists():
            counts["cached"] += 1
            continue
        if args.offline:
            counts["external_metadata_not_requested"] += 1
            continue
        metadata, state = fetch(slug, ci_id, deadline)
        counts[state] += 1
        if metadata is not None:
            root.write(relative, {"repository": slug, "ci_run_id": ci_id, "metadata": metadata})
        if state == "deadline":
            break
    root.write(
        args.output,
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "definition": "all structured CI observations in the selected warehouse",
            "event_types": types,
            "counts": dict(counts),
            "deadline_seconds": args.deadline_seconds,
            "offline": args.offline,
            "status": "deadline" if counts["deadline"] else "complete",
        },
    )
    print("Private CI census written; metadata fetch remained deadline-bounded and cached.")


if __name__ == "__main__":
    main()
