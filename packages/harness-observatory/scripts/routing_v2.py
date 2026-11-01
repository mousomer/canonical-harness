"""Inventory-frozen offline action graph. Stops before all route design."""

import argparse
import collections
import datetime
import hashlib
import io
import json
import re
import sqlite3
import subprocess
from pathlib import Path

from harness_observatory.data_root import DataRoot
from routing_v2_core import (
    classify,
    decode_output,
    hash_value,
    is_unknown,
    parse_wrapper,
    source_output_owner,
)

VERSION = "routing-analysis-2.0"
TABLES = "logical_actions logical_action_edges outputs usage operation_classifications governance_eras state_transitions task_episodes acceptance_sample raw_invocations session_summary".split()


def run(args):
    root = DataRoot(args.data_root)
    inventory_path = root.destination(args.inventory)
    inventory = json.loads(inventory_path.read_text())
    identity = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(inventory["sources"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    if identity != inventory["inventory_id"]:
        raise ValueError("inventory_identity_mismatch")
    dest = root.destination(args.output)
    dest.mkdir(exist_ok=False, parents=True, mode=0o700)
    db = sqlite3.connect(dest / "measurement.sqlite")
    for table in TABLES:
        db.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, data TEXT NOT NULL)")

    def put(table, key, value):
        value = {"inventory_id": identity, **value}
        db.execute(
            f"INSERT INTO {table} VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False))
        )

    def edge(source, target, relation, refs, **extra):
        key = hash_value([source, target, relation, refs])
        put(
            "logical_action_edges",
            key,
            {
                "source": source,
                "target": target,
                "relation": relation,
                "raw_event_ids": refs,
                "measured": {"relation_identifiers": extra},
                "inferred": {},
            },
        )

    baseline_path = Path(inventory["baseline_checkpoint"])
    base = sqlite3.connect("file:" + str(baseline_path / "routing.sqlite") + "?mode=ro", uri=True)
    baseline = {r[0]: json.loads(r[1]) for r in base.execute("select id,data from logical_calls")}
    seen, output_seen, usage_seen = {}, {}, {}
    totals = collections.Counter()
    reasons = collections.defaultdict(collections.Counter)
    naive = collections.defaultdict(collections.Counter)
    era_cache = {}
    sample_candidates = collections.defaultdict(list)
    policy_examples = []
    start_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def era_for(sha, cli, agents_hash):
        cache_key = (sha, cli, agents_hash)
        if cache_key in era_cache:
            return era_cache[cache_key]
        docs = None
        if sha and re.fullmatch(r"[0-9a-f]{40}", sha):
            result = subprocess.run(
                ["git", "-C", args.repo, "ls-tree", "-r", sha], capture_output=True
            )
            if result.returncode == 0:
                docs = {}
                for line in result.stdout.decode().splitlines():
                    info, path = line.split("\t", 1)
                    if (
                        Path(path).name in {"AGENTS.md", "CLAUDE.md"}
                        or path
                        in {
                            "gov",
                            "config/governance/project.json",
                            "config/project/policy_pack.json",
                            ".governance/workspace.json",
                        }
                        or "workspace-governance" in path
                    ):
                        docs[path] = info.split()[2]
        candidate = {
            "repo_sha": sha,
            "cli_version": cli,
            "git_documents": docs,
            "injected_agents_hash": agents_hash,
        }
        eid = hash_value(candidate)
        put(
            "governance_eras",
            eid,
            {
                "era_id": eid,
                "measured": {"metadata_cli_version": cli, "injected_agents_hash": agents_hash},
                "inferred": {
                    "candidate_repo_sha": sha,
                    "git_blob_ids": docs,
                    "gov_present_in_candidate": ("gov" in docs) if docs is not None else None,
                },
                "effective_manifest_state": "unknown_unless_separately_observed",
                "raw_event_ids": [],
            },
        )
        era_cache[cache_key] = (
            eid,
            docs is not None,
            ("gov" in docs) if docs is not None else None,
        )
        return era_cache[cache_key]

    for number, source in enumerate(inventory["sources"], 1):
        path = root.destination(source["snapshot"])
        raw = path.read_bytes()
        if len(raw) != source["bytes"] or hash_value(raw) != source["sha256"]:
            raise ValueError("snapshot_integrity_failure")
        meta = source["session_metadata"]
        sid = meta.get("id", source["sha256"])
        owner = meta.get("source")
        cli = meta.get("cli_version")
        sha = (meta.get("git") or {}).get("commit_hash")
        agents_hash = None
        state_refs = [source["sha256"] + ":1"]
        model = None
        episode = None
        cell_map = {}
        process_map = {}
        calls = {}
        actions = {}
        output_ordinal = collections.Counter()
        session_counts = collections.Counter()
        initial_era = era_for(sha, cli, agents_hash)
        current_era = initial_era
        session_usage_ids = []
        for line, encoded in enumerate(io.BytesIO(raw), 1):
            ref = source["sha256"] + ":" + str(line)
            try:
                event = json.loads(encoded)
            except ValueError:
                totals["malformed_records"] += 1
                put(
                    "state_transitions",
                    ref,
                    {"session_id": sid, "kind": "malformed_record", "raw_event_ids": [ref]},
                )
                continue
            typ = event.get("type")
            q = event.get("payload", {})
            timestamp = event.get("timestamp")
            totals["event:" + str(typ)] += 1
            if typ == "turn_context":
                model = q.get("model", model)
                # Cwd observations are preserved without assigning a new repository SHA.
                if q.get("cwd") and q["cwd"] != meta.get("cwd"):
                    sha = None
                    state_refs = [ref]
                    put(
                        "state_transitions",
                        ref,
                        {
                            "session_id": sid,
                            "kind": "cwd_observation",
                            "measured": {"cwd": q["cwd"]},
                            "inferred": {"repo_sha": None},
                            "raw_event_ids": [ref],
                        },
                    )
                    current_era = era_for(sha, cli, agents_hash)
            if typ == "world_state":
                state = q.get("state", {})
                if q.get("full"):
                    agents_hash = None
                if "agents_md" in state:
                    agents_hash = hash_value(state["agents_md"]) if state["agents_md"] else None
                state_refs = [*state_refs, ref][-8:]
                current_era = era_for(sha, cli, agents_hash)
                put(
                    "state_transitions",
                    ref,
                    {
                        "session_id": sid,
                        "kind": "injected_state_observation",
                        "measured": {
                            "full": q.get("full"),
                            "components": state,
                            "component_hashes": {k: hash_value(v) for k, v in state.items()},
                        },
                        "inferred": {
                            "era_id": current_era[0],
                            "delta_semantics": "observed_component_update_not_full_precedence_reconstruction",
                        },
                        "raw_event_ids": [ref],
                    },
                )
            if typ == "token_usage_record":
                totals["usage_raw_records"] += 1
                response = q.get("response_id")
                key = response or ref
                u = q.get("usage", {})
                compatible = (
                    isinstance(u.get("input_tokens"), int)
                    and isinstance(u.get("cached_input_tokens"), int)
                    and 0 <= u["cached_input_tokens"] <= u["input_tokens"]
                )
                duplicate = key in usage_seen
                if duplicate and usage_seen[key] != hash_value(u):
                    totals["usage_conflicts"] += 1
                usage_seen.setdefault(key, hash_value(u))
                put(
                    "usage",
                    ref,
                    {
                        "session_id": sid,
                        "response_id": response,
                        "era_id": current_era[0],
                        "cli_version": cli,
                        "source_kind": owner,
                        "measured": {"payload": q, "usage": u},
                        "inferred": {
                            "uncached_input_tokens": u["input_tokens"] - u["cached_input_tokens"]
                            if compatible
                            else None,
                            "duplicate_of_response": key if duplicate else None,
                            "authoritative_record": bool(response),
                        },
                        "raw_event_ids": [ref],
                    },
                )
                if not duplicate:
                    session_usage_ids.append(key)
                    for family, value in u.items():
                        if isinstance(value, int):
                            totals["usage:" + family] += value
            if typ == "event_msg" and q.get("type") == "token_count":
                totals["legacy_token_count_records"] += 1
            if typ != "response_item":
                continue
            family = q.get("type")
            if family == "message" and q.get("role") == "user":
                episode = ref
                put(
                    "task_episodes",
                    ref,
                    {
                        "session_id": sid,
                        "measured": {"user_message": q.get("content"), "timestamp": timestamp},
                        "inferred": {
                            "kind": "user_turn_candidate_not_coding_task_boundary",
                            "final_repo_sha": None,
                            "final_diff_summary": None,
                        },
                        "raw_event_ids": [ref],
                    },
                )
            if family in {"function_call", "custom_tool_call"}:
                totals["raw_invocations"] += 1
                cid = q.get("call_id") or ref
                signature = hash_value({k: q.get(k) for k in ["name", "arguments", "input"]})
                put(
                    "raw_invocations",
                    ref,
                    {
                        "session_id": sid,
                        "call_id": cid,
                        "timestamp": timestamp,
                        "signature": signature,
                        "raw_event_ids": [ref],
                        "provider_item_id": q.get("id"),
                        "provider_metadata": q.get("internal_chat_message_metadata_passthrough"),
                    },
                )
                if cid in seen:
                    totals[
                        "mirrored_invocations" if seen[cid] == signature else "invocation_conflicts"
                    ] += 1
                    edge(
                        ref,
                        cid,
                        "duplicate_representation"
                        if seen[cid] == signature
                        else "conflicting_identifier",
                        [ref],
                    )
                    calls[cid] = {"duplicate": True}
                    continue
                seen[cid] = signature
                tool = q.get("name", "unknown")
                text = q.get("input", q.get("arguments", ""))
                totals["recorded_tool:" + tool] += 1
                wrapper = tool == "exec"
                if wrapper:
                    parsed = (
                        parse_wrapper(text)
                        if len(text) < 2_000_000
                        else {
                            "sites": [],
                            "emissions": [],
                            "reason": "unsupported_historical_trace_format",
                            "error": "source_size_bound",
                        }
                    )
                    sites = parsed["sites"]
                    reason = parsed["reason"]
                    if sites and all(s["tool_name"] in {"wait", "write_stdin"} for s in sites):
                        reason = "async_continuation_wait"
                else:
                    try:
                        params = json.loads(q["arguments"]) if "arguments" in q else q.get("input")
                    except ValueError:
                        params = {"unknown": True}
                    sites = [
                        {"ordinal": 0, "tool_name": tool, "arguments": params, "conditional": False}
                    ]
                    parsed = {"sites": sites, "emissions": []}
                    reason = "direct_invocation"
                era_id, resolved_git, gov = current_era
                call = {
                    "session_id": sid,
                    "call_id": cid,
                    "ref": ref,
                    "wrapper": wrapper,
                    "reason": reason,
                    "sites": [],
                    "emissions": parsed["emissions"],
                    "era_id": era_id,
                    "cli_version": cli,
                    "gov": gov,
                    "duplicate": False,
                }
                calls[cid] = call
                if wrapper:
                    put(
                        "logical_actions",
                        cid,
                        {
                            "session_id": sid,
                            "kind": "wrapper_container",
                            "measured": {
                                "source": text,
                                "timestamp": timestamp,
                                "recorded_tool_name": tool,
                            },
                            "inferred": {
                                "reason": reason,
                                "parser_problems": parsed.get("problems", []),
                                "era_id": era_id,
                            },
                            "raw_event_ids": [ref],
                            "state_refs": state_refs,
                        },
                    )
                for site in sites:
                    aid = cid + ":site:" + str(site["ordinal"]) if wrapper else cid
                    params = site["arguments"]
                    st = site["tool_name"]
                    command = (
                        params.get("cmd", params.get("command"))
                        if isinstance(params, dict)
                        else params
                        if isinstance(params, str)
                        else None
                    )
                    argument_known = not is_unknown(params)
                    if st in {"exec_command", "shell_command", "shell"} and not isinstance(
                        command, str
                    ):
                        argument_known = False
                    if st.endswith("apply_patch") and not isinstance(params, str):
                        argument_known = False
                    kind = (
                        "continuation_observation"
                        if st in {"wait", "write_stdin"}
                        else "tool_action"
                    )
                    site = {
                        **site,
                        "id": aid,
                        "command": command,
                        "argument_known": argument_known,
                        "kind": kind,
                    }
                    call["sites"].append(site)
                    actions[aid] = site
                    put(
                        "logical_actions",
                        aid,
                        {
                            "session_id": sid,
                            "kind": kind,
                            "measured": {
                                "tool_name": st,
                                "arguments": params,
                                "source_range": site.get("source_range"),
                                "timestamp": timestamp,
                                "model": model,
                                "source_kind": owner,
                                "cli_version": cli,
                            },
                            "inferred": {
                                "source_invocation_resolved": argument_known
                                and not site["conditional"],
                                "execution_success": None,
                                "conditional": site["conditional"],
                                "era_id": era_id,
                                "repo_sha_candidate": sha,
                                "git_candidate_resolved": resolved_git,
                                "gov_present_in_candidate": gov,
                                "task_episode_candidate": episode,
                            },
                            "raw_event_ids": [ref],
                            "state_refs": state_refs,
                        },
                    )
                    if wrapper:
                        edge(
                            cid, aid, "source_describes_invocation", [ref], ordinal=site["ordinal"]
                        )
                    if isinstance(params, dict) and st in {"wait", "write_stdin"}:
                        handle = str(params.get("cell_id" if st == "wait" else "session_id"))
                        mapping = cell_map if st == "wait" else process_map
                        target = mapping.get(handle)
                        if target:
                            site["continuation_of"] = target
                            edge(aid, target, "continues_observed_handle", [ref], handle=handle)
                    ops = (
                        classify(command, st)
                        if isinstance(command, str) and argument_known
                        else [
                            {
                                "class": "OTHER_OR_UNKNOWN",
                                "command": None,
                                "paths": [],
                                "basis": "unknown_arguments",
                            }
                        ]
                    )
                    for ordinal, op in enumerate(ops):
                        oid = aid + ":op:" + str(ordinal)
                        surfaces = []
                        for pathref in op["paths"]:
                            for name in [
                                "docs/BACKLOG.md",
                                "CURRENT_STATE.md",
                                "AGENTS.md",
                                "policy_pack.json",
                            ]:
                                if pathref == name or pathref.endswith("/" + name):
                                    surfaces.append(name)
                            if pathref == "docs/rds" or "docs/rds/" in pathref:
                                surfaces.extend(
                                    [
                                        "docs/rds/",
                                        pathref[pathref.index("docs/rds/") :]
                                        if "docs/rds/" in pathref
                                        else "docs/rds/",
                                    ]
                                )
                        if op["class"] == "GOV_QUERY":
                            surfaces.append("./gov")
                        row = {
                            "action_id": aid,
                            "session_id": sid,
                            "era_id": era_id,
                            "measured": {"command_fragment": op["command"]},
                            "inferred": {
                                **op,
                                "surfaces": sorted(set(surfaces)),
                                "path_families": sorted(set(p.split("/")[0] for p in op["paths"])),
                            },
                            "manually_judged": None,
                            "raw_event_ids": [ref],
                        }
                        put("operation_classifications", oid, row)
                        session_counts[op["class"]] += 1
                        # Deterministic hash-priority sample, not easiest/shortest examples.
                        sample_candidates[op["class"]].append((hash_value(oid), oid))
                        if op["class"] == "WHOLE_READ" and "policy_pack.json" in surfaces:
                            policy_examples.append(
                                {"action_id": aid, "raw_event_id": ref, "command": command}
                            )
                        if op.get("state_change"):
                            totals["state_change_attempts"] += 1
                            put(
                                "state_transitions",
                                oid,
                                {
                                    "session_id": sid,
                                    "action_id": aid,
                                    "kind": op["state_change"],
                                    "measured": {"command": op["command"]},
                                    "inferred": {
                                        "previous_repo_sha": sha,
                                        "new_repo_sha": None,
                                        "reason": "attempt_invalidates_assumption_not_proof_of_success",
                                    },
                                    "raw_event_ids": [ref],
                                },
                            )
                            sha = None
                            state_refs = [ref]
                            current_era = era_for(sha, cli, agents_hash)
                was_unresolved = baseline.get(cid, {}).get("relation") == "unresolved_wrapper"
                if wrapper:
                    bucket = (reason, era_id, cli, str(gov), was_unresolved)
                    reasons[bucket]["wrappers"] += 1
                    reasons[bucket]["resolved_sites"] += sum(
                        s["argument_known"] and not s["conditional"] for s in call["sites"]
                    )
                    call["reason_bucket"] = bucket
            elif family in {"function_call_output", "custom_tool_call_output"}:
                totals["raw_output_records"] += 1
                value = q.get("output")
                rawtexts = (
                    [value]
                    if isinstance(value, str)
                    else [x.get("text", "") for x in value if isinstance(x, dict)]
                    if isinstance(value, list)
                    else []
                )
                for text in rawtexts:
                    for match in re.finditer(r"Original token count:\s*(\d+)", text):
                        naive[family]["case_sensitive_original_tokens"] += int(match[1])
                        naive[family]["case_sensitive_matches"] += 1
                    for match in re.finditer(r"original token count[: ]+\s*(\d+)", text, re.I):
                        naive[family]["case_insensitive_original_tokens"] += int(match[1])
                        naive[family]["case_insensitive_matches"] += 1
                cid = q.get("call_id")
                output_ordinal[cid] += 1
                fingerprint = hash_value(value)
                dedupkey = (cid, output_ordinal[cid])
                if dedupkey in output_seen:
                    totals[
                        "mirrored_outputs"
                        if output_seen[dedupkey] == fingerprint
                        else "output_identifier_conflicts"
                    ] += 1
                    continue
                output_seen[dedupkey] = fingerprint
                call = calls.get(cid)
                if not call or call.get("duplicate"):
                    totals["orphan_outputs"] += 1
                    call = {"sites": [], "wrapper": False, "era_id": current_era[0]}
                decoded = decode_output(value)
                sites = call["sites"]
                for ordinal, item in enumerate(decoded):
                    oid = ref + ":part:" + str(ordinal)
                    target, attribution = source_output_owner(call, item, decoded)
                    if target in actions:
                        target = actions[target].get("continuation_of", target)
                    if (
                        target
                        and target in actions
                        and actions[target]["kind"] == "continuation_observation"
                    ):
                        attribution = "unlinked_continuation"
                        target = None
                    substantive = bool(item["body"])
                    if item.get("cell_id"):
                        # A running wrapper with one unconditional site has a known
                        # continuation target even though its current output is framing.
                        single = (
                            sites[0]["id"]
                            if len(sites) == 1 and not sites[0]["conditional"]
                            else None
                        )
                        cell_map[item["cell_id"]] = target or single or cid
                    if item.get("process_session_id"):
                        process_map[item["process_session_id"]] = target or cid
                    leaf_target = (
                        target
                        if target in actions and actions[target]["kind"] == "tool_action"
                        else None
                    )
                    put(
                        "outputs",
                        oid,
                        {
                            "session_id": sid,
                            "call_id": cid,
                            "measured": item,
                            "inferred": {
                                "action_id": leaf_target,
                                "container_id": cid,
                                "attribution": attribution,
                                "era_id": call["era_id"],
                                "body_interpretation": "envelope_decoded_not_model_visible_tokens",
                            },
                            "raw_event_ids": [ref],
                        },
                    )
                    edge(
                        oid,
                        leaf_target or cid or "unknown",
                        "output_of" if leaf_target else "unallocated_container_output",
                        [ref],
                    )
                    totals["retained_raw_text_bytes"] += item["raw_bytes"]
                    totals["decoded_body_bytes"] += item["body_bytes"]
                    if leaf_target:
                        totals["leaf_attributed_raw_bytes"] += item["raw_bytes"]
                        totals["leaf_attributed_body_bytes"] += item["body_bytes"]
                    elif not substantive:
                        totals["framing_only_raw_bytes"] += item["raw_bytes"]
                    if item["original_tokens"] is not None:
                        totals["known_original_tokens"] += item["original_tokens"]
                        totals["original_count_records"] += 1
                        if item["truncated"]:
                            totals["truncated_associated_original_tokens"] += item[
                                "original_tokens"
                            ]
                    if item["truncated"]:
                        totals["truncated_output_parts"] += 1
                        totals["truncated_body_bytes"] += item["body_bytes"]
                    if call.get("reason_bucket"):
                        bucket = call["reason_bucket"]
                        reasons[bucket]["raw_text_bytes"] += item["raw_bytes"]
                        reasons[bucket]["unattributed_body_bytes"] += (
                            0 if leaf_target else item["body_bytes"]
                        )
                    # Only an isolated SHA body from a sole rev-parse command is a state observation.
                    if leaf_target and leaf_target in actions:
                        command = actions[leaf_target].get("command")
                        if (
                            isinstance(command, str)
                            and re.fullmatch(r"(?:git\s+rev-parse\s+HEAD)\s*", command)
                            and re.fullmatch(r"[0-9a-f]{40}\s*", item["body"])
                        ):
                            sha = item["body"].strip()
                            totals["isolated_sha_observations"] += 1
                            current_era = era_for(sha, cli, agents_hash)
                            state_refs = [ref]
                            put(
                                "state_transitions",
                                oid,
                                {
                                    "session_id": sid,
                                    "kind": "exact_repo_sha_observation",
                                    "measured": {"repo_sha": sha},
                                    "inferred": {"era_id": current_era[0]},
                                    "raw_event_ids": [ref],
                                },
                            )
        put(
            "session_summary",
            source["sha256"],
            {
                "session_id": sid,
                "source_kind": owner,
                "cli_version": cli,
                "measured": {"start_metadata": meta, "source_hash": source["sha256"]},
                "inferred": {
                    "start_era_id": initial_era[0],
                    "final_candidate_sha": sha,
                    "final_tree_verified": False,
                    "usage_response_ids": session_usage_ids,
                    "operation_counts": dict(session_counts),
                },
                "raw_event_ids": [source["sha256"] + ":1"],
            },
        )
        db.commit()
        if number % 40 == 0:
            print(
                json.dumps({"files_processed": number, "distinct_invocations": len(seen)}),
                flush=True,
            )
    for kind, candidates in sample_candidates.items():
        for rank, (_, oid) in enumerate(sorted(candidates)[:12]):
            row = json.loads(
                db.execute(
                    "select data from operation_classifications where id=?", (oid,)
                ).fetchone()[0]
            )
            put(
                "acceptance_sample",
                oid,
                {
                    "stratum": kind,
                    "sampling": "lowest_SHA256_operation_id_12_per_predicted_class",
                    "rank": rank,
                    "operation_id": oid,
                    "normalized": row,
                    "manually_judged": None,
                    "raw_event_ids": row["raw_event_ids"],
                },
            )
    db.execute("CREATE INDEX output_action ON outputs(json_extract(data,'$.inferred.action_id'))")
    db.execute(
        "CREATE INDEX operation_action ON operation_classifications(json_extract(data,'$.action_id'))"
    )
    db.commit()
    metadata = {
        "inventory_id": identity,
        "inventory_path": str(inventory_path),
        "version": VERSION,
        "analysis_timestamp": start_time,
        "status": "PENDING_RAW_AUDIT_NOT_ACCEPTED",
        "files": len(inventory["sources"]),
        "source_bytes": sum(x["bytes"] for x in inventory["sources"]),
        "totals": dict(totals),
        "distinct_invocations": len(seen),
        "table_counts": {t: db.execute(f"select count(*) from {t}").fetchone()[0] for t in TABLES},
        "naive_original_aggregation": {k: dict(v) for k, v in naive.items()},
        "policy_whole_read_candidates": policy_examples,
        "unresolved_by_reason_era": [
            {
                "reason": k[0],
                "era_id": k[1],
                "cli_version": k[2],
                "gov_candidate": k[3],
                "formerly_unresolved": k[4],
                **dict(v),
            }
            for k, v in reasons.items()
        ],
        "thresholds": {
            "leaf_body_byte_attribution_min": 0.99,
            "operation_precision_min": 0.95,
            "operation_recall_min": 0.95,
            "unknown_governance_max_for_acceptance": 0.05,
            "unresolved_high_volume_classes": "must_be_explained_and_bias_bounded",
        },
    }
    with (dest / "run_summary.json").open("x") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")
    # Each requested artifact has a named queryable JSONL export; SQLite remains canonical.
    for table in TABLES:
        with (dest / (table + ".jsonl")).open("x") as f:
            for key, value in db.execute(f"SELECT id,data FROM {table} ORDER BY id"):
                f.write(json.dumps({"id": key, **json.loads(value)}, ensure_ascii=False) + "\n")
    db.close()
    print(
        json.dumps(
            {
                "inventory_id": identity,
                "totals": dict(totals),
                "table_counts": metadata["table_counts"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--repo", required=True)
    run(parser.parse_args())
