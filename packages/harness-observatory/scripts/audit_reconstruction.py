"""Select and verify a bounded, stratified private reconstruction audit."""

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from harness_observatory.data_root import resolve_data_root
from harness_observatory.model import digest, validate_bundle


def stratum(bundle):
    run = bundle["run"]
    facts = run["facts"]
    sha = "direct" if facts["start_sha"]["value"] else "unknown"
    provenance = facts["start_sha"]["provenance"]
    method = provenance.get("method") if isinstance(provenance, dict) else None
    if facts["start_sha"]["value"] and method != "provider_metadata":
        sha = "inferred"
    ci = (
        "ci"
        if any(event["event_type"].startswith("ci.") for event in bundle["events"])
        else "non_ci"
    )
    warnings = "warning_heavy" if len(bundle["warnings"]) >= 20 else "warning_clean"
    return (run["agent"], sha, run["governance_state"], ci, warnings)


def choose(paths, limit):
    candidates = []
    for path in sorted(paths):
        bundle = json.loads(path.read_text())
        candidates.append((stratum(bundle), path, bundle))
    chosen, seen = [], set()
    for item in candidates:
        if item[0] not in seen:
            chosen.append(item)
            seen.add(item[0])
            if len(chosen) == limit:
                return chosen
    for item in candidates:
        if item not in chosen:
            chosen.append(item)
            if len(chosen) == limit:
                break
    return chosen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root")
    parser.add_argument("--extractor-version", default="0.3.0")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--output", default="analysis/results/reconstruction-audit-v2.json")
    args = parser.parse_args()
    if not 1 <= args.limit <= 20:
        parser.error("limit must be between 1 and 20")
    root = resolve_data_root(args.data_root)
    paths = (root.path / "enriched/governance/v2" / args.extractor_version).glob("*.json")
    selected = choose(paths, args.limit)
    findings, verdicts = [], Counter()
    for group, path, bundle in selected:
        validate_bundle(bundle)
        run = bundle["run"]
        source_id = run["source_id"]
        raw = root.destination(f"raw/{run['agent']}/{source_id.split(':', 1)[1]}.jsonl")
        checks = {
            "raw_present": raw.is_file(),
            "source_digest_matches": raw.is_file() and digest(raw.read_bytes()) == source_id,
            "run_id_matches_filename": path.stem == run["run_id"].split(":", 1)[-1],
            "sequence_contiguous": [e["seq"] for e in bundle["events"]]
            == list(range(len(bundle["events"]))),
        }
        verdict = "pass" if all(checks.values()) else "fail"
        verdicts[verdict] += 1
        findings.append(
            {"run_id": run["run_id"], "stratum": group, "checks": checks, "verdict": verdict}
        )
    root.write(
        args.output,
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "method": "deterministic first-per-stratum selection, then deterministic fill",
            "inspection_limit": args.limit,
            "selected": len(findings),
            "distinct_strata": len({tuple(f["stratum"]) for f in findings}),
            "verdicts": dict(verdicts),
            "findings": findings,
        },
    )
    print(
        f"Private reconstruction audit complete: {len(findings)} selected, {verdicts['fail']} failed."
    )


if __name__ == "__main__":
    main()
