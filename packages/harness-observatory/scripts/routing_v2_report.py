"""Post-audit measurements and a fail-closed acceptance record; no corpus replay."""

import argparse
import collections
import json
import sqlite3
import csv

from harness_observatory.data_root import DataRoot
from routing_v2_audit import metrics


def main(args):
    root = DataRoot(args.data_root)
    run = root.destination(args.run)
    inventory = json.loads(root.destination(args.inventory).read_text())
    summary = json.loads((run / "run_summary.json").read_text())
    db = sqlite3.connect("file:" + str(run / "measurement.sqlite") + "?mode=ro", uri=True)
    old = sqlite3.connect(
        "file:"
        + str(root.destination(inventory["baseline_checkpoint"]) / "routing.sqlite")
        + "?mode=ro",
        uri=True,
    )
    old_unresolved = {
        k
        for k, v in old.execute("select id,data from logical_calls")
        if json.loads(v)["relation"] == "unresolved_wrapper"
    }
    eras = {k: json.loads(v) for k, v in db.execute("select id,data from governance_eras")}
    wrappers = {
        k: json.loads(v)["inferred"]["reason"]
        for k, v in db.execute(
            "select id,data from logical_actions where json_extract(data,'$.kind')='wrapper_container'"
        )
    }

    def era_group(eid):
        e = eras[eid]
        d = e["inferred"]["git_blob_ids"]
        return (
            "unknown_repo_state"
            if d is None
            else "dispatcher_candidate"
            if "gov" in d
            else "pre_authority_candidate"
            if "AGENTS.md" not in d
            else "pre_dispatcher_candidate"
        )

    strata = collections.defaultdict(collections.Counter)
    coarse = collections.defaultdict(collections.Counter)
    cli_rows = collections.defaultdict(collections.Counter)
    action_rows = {}
    wrapper_sites = collections.Counter()
    wrapper_bad = collections.Counter()
    action_counts = collections.Counter()
    for aid, kind, eid, cli, resolved, stateknown, sha in db.execute(
        "select id,json_extract(data,'$.kind'),json_extract(data,'$.inferred.era_id'),json_extract(data,'$.measured.cli_version'),json_extract(data,'$.inferred.source_invocation_resolved'),json_extract(data,'$.inferred.git_candidate_resolved'),json_extract(data,'$.inferred.repo_sha_candidate') from logical_actions"
    ):
        if kind == "wrapper_container":
            continue
        cid = aid.split(":site:")[0]
        trace = "wrapper" if cid in wrappers else "direct"
        group = era_group(eid)
        action_rows[aid] = (eid, cli, trace, kind)
        action_counts["candidates"] += 1
        action_counts["unresolved"] += not bool(resolved)
        action_counts["git_candidate_known"] += bool(stateknown)
        if cid in wrappers:
            wrapper_sites[cid] += 1
            wrapper_bad[cid] += not bool(resolved)
        for bucket in [strata[(eid, cli, trace)], coarse[(group, trace)], cli_rows[(cli, trace)]]:
            bucket["action_candidates"] += 1
            bucket["unresolved_candidates"] += not bool(resolved)
    missing = collections.Counter()
    output_raw_by_cid = collections.Counter()
    for (text,) in db.execute("select data from outputs"):
        row = json.loads(text)
        i = row["inferred"]
        m = row["measured"]
        eid = i["era_id"]
        cid = row.get("call_id")
        cli = eras[eid]["measured"]["metadata_cli_version"]
        trace = "wrapper" if cid in wrappers else "direct"
        group = era_group(eid)
        raw = m["raw_bytes"]
        body = m["body_bytes"]
        attributed = i["action_id"] is not None
        output_raw_by_cid[cid] += raw
        for bucket in [strata[(eid, cli, trace)], coarse[(group, trace)], cli_rows[(cli, trace)]]:
            bucket["retained_raw_bytes"] += raw
            bucket["unattributed_raw_bytes"] += 0 if attributed else raw
            bucket["body_bytes"] += body
            bucket["unattributed_body_bytes"] += 0 if attributed else body
        if not attributed:
            missing[i["attribution"]] += raw

    def rows_from(mapping, fields):
        return [
            {
                **dict(zip(fields, k)),
                **dict(v),
                "unresolved_candidate_rate": v["unresolved_candidates"] / v["action_candidates"]
                if v["action_candidates"]
                else None,
                "unattributed_raw_rate": v["unattributed_raw_bytes"] / v["retained_raw_bytes"]
                if v["retained_raw_bytes"]
                else None,
            }
            for k, v in sorted(mapping.items(), key=lambda kv: str(kv[0]))
        ]

    statekind = collections.Counter(
        json.loads(r[0])["kind"] for r in db.execute("select data from state_transitions")
    )
    populations = collections.Counter()
    surface = collections.defaultdict(
        lambda: {
            "classes": collections.Counter(),
            "actions": set(),
            "envelopes": set(),
            "sessions": set(),
        }
    )
    govgroups = collections.defaultdict(
        lambda: {"operations": 0, "actions": set(), "sessions": set()}
    )
    for oid, text in db.execute("select id,data from operation_classifications"):
        r = json.loads(text)
        kind = r["inferred"]["class"]
        populations[kind] += 1
        for name in r["inferred"]["surfaces"]:
            x = surface[name]
            x["classes"][kind] += 1
            x["actions"].add(r["action_id"])
            x["envelopes"].add(r["action_id"].split(":site:")[0])
            x["sessions"].add(r["session_id"])
        if kind == "GOV_QUERY":
            g = govgroups[era_group(r["era_id"])]
            g["operations"] += 1
            g["actions"].add(r["action_id"])
            g["sessions"].add(r["session_id"])
    previous = list(
        csv.DictReader(
            (root.destination(inventory["baseline_checkpoint"]) / "surface_operations.csv").open()
        )
    )
    comparisons = []
    for prior in previous:
        name = prior["surface"]
        v = surface[name]
        comparisons.append(
            {
                "surface": name,
                "v1_envelopes": int(prior["logical_calls"]),
                "v2_envelopes": len(v["envelopes"]),
                "v2_leaf_candidates": len(v["actions"]),
                "v2_sessions": len(v["sessions"]),
                "v1_classes": {
                    k: int(prior[k])
                    for k in [
                        "WHOLE_READ",
                        "BOUNDED_READ",
                        "SEARCH",
                        "GIT_INSPECT",
                        "EDIT",
                        "GENERATED_REFRESH",
                        "VERIFY",
                        "RUNTIME_OR_BUILD",
                        "GOV_QUERY",
                        "METADATA",
                        "OTHER",
                        "UNKNOWN",
                    ]
                },
                "v2_classes": dict(v["classes"]),
            }
        )
    usage_sessions = set()
    usage_responses = set()
    usage_eras = collections.Counter()
    usage_cli = collections.Counter()
    usage_sources = collections.Counter()
    for (text,) in db.execute("select data from usage"):
        r = json.loads(text)
        usage_sessions.add(r["session_id"])
        usage_responses.add(r["response_id"])
        usage_eras[r["era_id"]] += 1
        usage_cli[r["cli_version"]] += 1
        usage_sources[json.dumps(r["source_kind"], sort_keys=True)] += 1
    evidence = json.loads((run / "acceptance_evidence.json").read_text())
    judgments = json.loads((run / "acceptance_judgements.json").read_text())
    estimates = metrics(evidence, judgments["operation_judgements"], populations)
    wrapper_stats = {
        "wrappers": len(wrappers),
        "with_all_candidates_resolved": sum(
            wrapper_sites[k] > 0 and wrapper_bad[k] == 0 for k in wrappers
        ),
        "with_unresolved_candidates": sum(wrapper_bad[k] > 0 for k in wrappers),
        "without_candidates": sum(wrapper_sites[k] == 0 for k in wrappers),
        "formerly_unresolved": len(old_unresolved),
        "formerly_unresolved_now_all_candidates_resolved": sum(
            wrapper_sites[k] > 0 and wrapper_bad[k] == 0 for k in old_unresolved
        ),
        "formerly_unresolved_with_bad_candidates": sum(wrapper_bad[k] > 0 for k in old_unresolved),
        "formerly_unresolved_without_candidates": sum(
            wrapper_sites[k] == 0 for k in old_unresolved
        ),
    }
    # Same raw-result IDs provide an exact bridge to the diagnostic parser.
    new_counts = collections.Counter()
    for ref, n in db.execute(
        "select json_extract(data,'$.raw_event_ids[0]'),json_extract(data,'$.measured.original_tokens') from outputs"
    ):
        if n is not None:
            new_counts[ref] += n
    bridge = collections.defaultdict(collections.Counter)
    bridge_examples = collections.defaultdict(list)
    for ref, text in old.execute("select id,data from outputs"):
        r = json.loads(text)
        a = r.get("original_tokens") or 0
        b = new_counts[ref]
        label = "unchanged" if a == b else "removed" if not b else "added" if not a else "changed"
        bridge[label]["records"] += 1
        bridge[label]["v1_tokens"] += a
        bridge[label]["v2_tokens"] += b
        if a != b and len(bridge_examples[label]) < 5:
            bridge_examples[label].append(
                {"raw_event_id": ref, "v1": a, "v2": b, "prefix": " ".join(r["text_parts"])[:220]}
            )
    totals = summary["totals"]
    coverage = totals["leaf_attributed_raw_bytes"] / totals["retained_raw_text_bytes"]
    report = {
        "inventory_id": inventory["inventory_id"],
        "decision": "NOT ACCEPTED",
        "decision_basis": [
            "leaf retained-text coverage below 99%",
            "adjudicated operation precision/recall below 95%",
            "nested truncation and legacy envelope failures",
            "effective governance/state reconstruction incomplete",
        ],
        "thresholds_unchanged": True,
        "action_counts": dict(action_counts),
        "wrapper_reconstruction": wrapper_stats,
        "output_leaf_raw_coverage": coverage,
        "missing_output_bytes_by_reason": dict(missing),
        "coarse_era_coverage": rows_from(coarse, ["governance_candidate_group", "trace"]),
        "exact_era_coverage": rows_from(strata, ["era_id", "cli_version", "trace"]),
        "cli_coverage": rows_from(cli_rows, ["cli_version", "trace"]),
        "state_transitions": dict(statekind),
        "gov_queries_by_candidate_era": {
            k: {
                "operations": v["operations"],
                "leaf_candidates": len(v["actions"]),
                "sessions": len(v["sessions"]),
            }
            for k, v in govgroups.items()
        },
        "usage_coverage": {
            "sessions": len(usage_sessions),
            "responses": len(usage_responses),
            "eras": len(usage_eras),
            "records_by_era": dict(usage_eras),
            "records_by_cli": dict(usage_cli),
            "records_by_source": dict(usage_sources),
            "legacy_token_count_records_not_normalized": totals["legacy_token_count_records"],
        },
        "classifier": estimates,
        "surface_comparison": comparisons,
        "diagnostic_token_bridge": {k: dict(v) for k, v in bridge.items()},
        "diagnostic_bridge_examples": dict(bridge_examples),
        "judged_operations": len(judgments["operation_judgements"]),
        "judged_graph_examples": len(judgments["graph_judgements"]),
    }
    with (run / "acceptance_report.json").open("x") as f:
        json.dump(report, f, indent=2)
    for name, key in [
        ("era_coverage", "exact_era_coverage"),
        ("cli_coverage", "cli_coverage"),
        ("surface_comparison", "surface_comparison"),
    ]:
        with (run / (name + ".json")).open("x") as f:
            json.dump(report[key], f, indent=2)
    print(
        json.dumps(
            {
                k: report[k]
                for k in [
                    "decision",
                    "wrapper_reconstruction",
                    "action_counts",
                    "output_leaf_raw_coverage",
                    "coarse_era_coverage",
                    "state_transitions",
                    "gov_queries_by_candidate_era",
                ]
            },
            indent=2,
        )
    )
    print("classifier", json.dumps(estimates, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--inventory", required=True)
    main(parser.parse_args())
