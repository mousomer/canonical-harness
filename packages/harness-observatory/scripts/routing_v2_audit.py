"""Private post-replay evidence sample and adjudication metrics; no replay or inference."""

import argparse
import collections
import json
import math
import sqlite3

from harness_observatory.data_root import DataRoot


def wilson(correct, total):
    if not total:
        return None
    z = 1.959963984540054
    p = correct / total
    center = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return [center - half, center + half]


def prepare(root, run, inventory):
    db = sqlite3.connect("file:" + str(run / "measurement.sqlite") + "?mode=ro", uri=True)
    selected = {}
    for oid, text in db.execute("select id,data from acceptance_sample order by id"):
        row = json.loads(text)
        selected[oid] = {
            "operation_id": oid,
            "sampling": "hash_priority_predicted_class",
            "predicted": row["stratum"],
            "normalized": row["normalized"],
            "raw_event_ids": row["raw_event_ids"],
        }
    # Supplement coverage deliberately; excluded from probability-sample estimates.
    for label, where in [
        ("backlog", "data like '%docs/BACKLOG.md%'"),
        ("current_state", "data like '%CURRENT_STATE.md%'"),
        ("rds", "data like '%docs/rds/%'"),
        ("agents", "data like '%AGENTS.md%'"),
        ("policy", "data like '%policy_pack.json%'"),
    ]:
        for oid, text in db.execute(
            "select id,data from operation_classifications where " + where + " order by id limit 3"
        ):
            row = json.loads(text)
            selected.setdefault(
                oid,
                {
                    "operation_id": oid,
                    "sampling": "supplement:" + label,
                    "predicted": row["inferred"]["class"],
                    "normalized": row,
                    "raw_event_ids": row["raw_event_ids"],
                },
            )
    special = []
    for label, where in [
        ("truncated", "json_extract(data,'$.measured.truncated')=1"),
        (
            "nontruncated",
            "json_extract(data,'$.measured.truncated')=0 and json_extract(data,'$.inferred.action_id') is not null",
        ),
        (
            "unattributed",
            "json_extract(data,'$.inferred.action_id') is null and json_extract(data,'$.measured.body_bytes')>0",
        ),
        (
            "ordered_multi",
            "json_extract(data,'$.inferred.attribution')='ordered_symbolic_result_array'",
        ),
    ]:
        for oid, text in db.execute(
            "select id,data from outputs where " + where + " order by id limit 4"
        ):
            row = json.loads(text)
            cid = row.get("call_id")
            rawcall = db.execute(
                "select data from raw_invocations where json_extract(data,'$.call_id')=? limit 1",
                (cid,),
            ).fetchone()
            refs = row["raw_event_ids"] + (
                json.loads(rawcall[0])["raw_event_ids"] if rawcall else []
            )
            special.append(
                {
                    "id": oid,
                    "sampling": label,
                    "normalized": row,
                    "raw_event_ids": refs,
                    "manually_judged": None,
                }
            )
    for label, where in [
        ("continuation", "json_extract(data,'$.relation')='continues_observed_handle'"),
    ]:
        for oid, text in db.execute(
            "select id,data from logical_action_edges where " + where + " order by id limit 4"
        ):
            row = json.loads(text)
            target = db.execute(
                "select data from logical_actions where id=?", (row["target"],)
            ).fetchone()
            refs = row["raw_event_ids"] + (json.loads(target[0])["raw_event_ids"] if target else [])
            special.append(
                {
                    "id": oid,
                    "sampling": label,
                    "normalized": row,
                    "raw_event_ids": refs,
                    "manually_judged": None,
                }
            )
    for kind, where in [
        ("guardian", "json_extract(data,'$.measured.source_kind.subagent.other')='guardian'"),
        (
            "spawned",
            "json_extract(data,'$.measured.source_kind.subagent.thread_spawn') is not null",
        ),
        ("cli", "json_extract(data,'$.measured.source_kind')='cli'"),
        ("vscode", "json_extract(data,'$.measured.source_kind')='vscode'"),
    ]:
        for oid, text in db.execute(
            "select id,data from logical_actions where " + where + " order by id limit 2"
        ):
            row = json.loads(text)
            special.append(
                {
                    "id": oid,
                    "sampling": "source:" + kind,
                    "normalized": row,
                    "raw_event_ids": row["raw_event_ids"],
                    "manually_judged": None,
                }
            )
    refs = collections.defaultdict(set)
    for row in list(selected.values()) + special:
        for ref in row["raw_event_ids"]:
            h, n = ref.split(":")
            refs[h].add(int(n))
    raw_records = {}
    sources = {x["sha256"]: x for x in inventory["sources"]}
    for h, numbers in refs.items():
        path = root.destination(sources[h]["snapshot"])
        with path.open() as f:
            for n, line in enumerate(f, 1):
                if n in numbers:
                    raw_records[h + ":" + str(n)] = json.loads(line)
                if n >= max(numbers):
                    break
    for row in list(selected.values()) + special:
        row["raw_events"] = {r: raw_records[r] for r in row["raw_event_ids"]}
        row["source_metadata"] = {
            r.split(":")[0]: sources[r.split(":")[0]]["session_metadata"]
            for r in row["raw_event_ids"]
        }
        row["manually_judged"] = None
    output = {
        "inventory_id": inventory["inventory_id"],
        "operations": list(selected.values()),
        "graph_and_output": special,
        "status": "AWAITING_INDEPENDENT_ADJUDICATION",
    }
    with (run / "acceptance_evidence.json").open("x") as f:
        json.dump(output, f, indent=2)
    print(
        json.dumps(
            {
                "operation_sample": len(selected),
                "special_sample": len(special),
                "raw_records": len(raw_records),
            }
        )
    )


def metrics(evidence, labels, populations):
    rows = []
    for row in evidence["operations"]:
        if row["sampling"] != "hash_priority_predicted_class":
            continue
        judged = labels.get(row["operation_id"])
        if judged:
            rows.append((row["predicted"], judged["gold"]))
    samples = collections.Counter(p for p, g in rows)
    confusion = collections.Counter(rows)
    result = {}
    for cls in sorted(populations):
        tp = confusion[cls, cls]
        predicted = sum(v for (p, g), v in confusion.items() if p == cls)
        actual = sum(v for (p, g), v in confusion.items() if g == cls)
        weighted_tp = tp * populations[cls] / samples[cls] if samples[cls] else 0
        weighted_actual = sum(
            v * populations[p] / samples[p]
            for (p, g), v in confusion.items()
            if g == cls and samples[p]
        )
        result[cls] = {
            "sample_predicted": predicted,
            "sample_actual": actual,
            "true_positives": tp,
            "sample_precision": tp / predicted if predicted else None,
            "sample_recall": tp / actual if actual else None,
            "precision_wilson_95": wilson(tp, predicted),
            "recall_wilson_95": wilson(tp, actual),
            "stratum_weighted_recall": weighted_tp / weighted_actual if weighted_actual else None,
        }
    return {
        "judged_probability_sample": len(rows),
        "confusion": [
            {"predicted": p, "gold": g, "count": n} for (p, g), n in sorted(confusion.items())
        ],
        "classes": result,
        "uncertainty_note": "Wilson intervals describe the small adjudicated sample. Weighted recall is a stratified point estimate, not a confidence guarantee; supplementary cases are excluded.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--inventory", required=True)
    args = parser.parse_args()
    root = DataRoot(args.data_root)
    prepare(
        root, root.destination(args.run), json.loads(root.destination(args.inventory).read_text())
    )
