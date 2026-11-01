"""Reproduce historical regex aggregation; never reruns classification or action inference."""

import argparse
import collections
import json
import re
import sqlite3

from harness_observatory.data_root import DataRoot


def historical_output_measurement(value):
    """Return the exact legacy per-record observation, not a token correction."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    pattern = re.compile(r'(?:Original token count|original_token_count)[:" ]+(\d+)', re.I)
    match = pattern.search(text)
    return (
        text,
        int(match[1]) if match else 0,
        "truncated output" in text.lower(),
        match is not None,
    )


def main(args):
    root = DataRoot(args.data_root)
    inv = json.loads(root.destination(args.inventory).read_text())
    run = root.destination(args.run)
    db = sqlite3.connect("file:" + str(run / "measurement.sqlite") + "?mode=ro", uri=True)
    new = {}
    for (value,) in db.execute("select data from outputs"):
        r = json.loads(value)
        ref = r["raw_event_ids"][0]
        n = r["measured"]["original_tokens"]
        if n is not None:
            new[ref] = new.get(ref, 0) + n
    counts = collections.Counter()
    families = collections.defaultdict(collections.Counter)
    seen = set()
    bridge = collections.defaultdict(collections.Counter)
    examples = collections.defaultdict(list)
    for source in inv["sources"]:
        ordinal = collections.Counter()
        with root.destination(source["snapshot"]).open() as stream:
            for line, text in enumerate(stream, 1):
                e = json.loads(text)
                q = e.get("payload", {})
                family = q.get("type")
                if e.get("type") != "response_item" or family not in {
                    "function_call_output",
                    "custom_tool_call_output",
                }:
                    continue
                text, n, truncated, recognized = historical_output_measurement(
                    q.get("output", q.get("content", ""))
                )
                counts["raw_outputs"] += 1
                counts["raw_original_tokens"] += n
                families[family]["raw_outputs"] += 1
                families[family]["raw_original_tokens"] += n
                if truncated and recognized:
                    counts["raw_truncated_associated_original_tokens"] += n
                    families[family]["raw_truncated_associated_original_tokens"] += n
                ordinal[q.get("call_id")] += 1
                key = (q.get("call_id"), ordinal[q.get("call_id")])
                if key in seen:
                    counts["duplicate_original_tokens"] += n
                    if truncated:
                        counts["duplicate_truncated_associated_original_tokens"] += n
                    continue
                seen.add(key)
                ref = source["sha256"] + ":" + str(line)
                corrected = new.get(ref, 0)
                counts["deduplicated_old_original_tokens"] += n
                if truncated:
                    counts["deduplicated_old_truncated_associated_original_tokens"] += n
                label = (
                    "unchanged"
                    if n == corrected
                    else "new_only"
                    if not n
                    else "old_only"
                    if not corrected
                    else "changed_envelope_scope"
                )
                bridge[label]["output_records"] += 1
                bridge[label]["old_tokens"] += n
                bridge[label]["new_tokens"] += corrected
                if n != corrected and len(examples[label]) < 8:
                    examples[label].append(
                        {"raw_event_id": ref, "old": n, "new": corrected, "prefix": text[:400]}
                    )
    result = {
        "inventory_id": inv["inventory_id"],
        "method": "exact historical regex search: first match per raw output; nonstrings json.dumps; truncated output substring",
        "counts": dict(counts),
        "families": {k: dict(v) for k, v in families.items()},
        "bridge": {k: dict(v) for k, v in bridge.items()},
        "examples": dict(examples),
        "new_parser_total": sum(new.values()),
        "status": "reconciliation_not_backend_certification",
    }
    with (run / "truncation_reconciliation.json").open("x") as f:
        json.dump(result, f, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != "examples"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--run", required=True)
    main(parser.parse_args())
