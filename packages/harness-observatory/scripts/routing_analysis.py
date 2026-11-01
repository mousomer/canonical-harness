"""Private, offline rollout measurement. Never executes recorded commands."""

import argparse
import collections
import csv
import hashlib
import io
import json
import re
import shlex
import sqlite3
import subprocess
from pathlib import Path

from harness_observatory.data_root import DataRoot

VERSION = "routing-analysis-1.1"
CLASSES = "WHOLE_READ BOUNDED_READ SEARCH GIT_INSPECT EDIT GENERATED_REFRESH VERIFY RUNTIME_OR_BUILD GOV_QUERY METADATA OTHER UNKNOWN".split()


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(value).hexdigest()


def js_tokens(source):
    """Lex comments/strings before locating calls; no eval or execution."""
    pattern = r"""\s+|//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|[^\s]"""
    return [
        m.group()
        for m in re.finditer(pattern, source)
        if not m.group().isspace() and not m.group().startswith(("//", "/*"))
    ]


def js_value(tokens, index):
    token = tokens[index]
    if token == "{":
        obj = {}
        index += 1
        while tokens[index] != "}":
            key = tokens[index].strip("\"'")
            if tokens[index + 1] != ":":
                raise ValueError("dynamic_object")
            obj[key], index = js_value(tokens, index + 2)
            if tokens[index] == ",":
                index += 1
            elif tokens[index] != "}":
                raise ValueError("dynamic_value")
        return obj, index + 1
    if token.startswith('"'):
        return json.loads(token), index + 1
    if token.startswith(("'", "`")):
        if "${" in token:
            raise ValueError("template_expression")
        # Support only documented basic JS escapes; unknown escapes stay unknown.
        body = token[1:-1]
        if re.search(r"\\(?![\\'\"`nrt])", body):
            raise ValueError("unsupported_escape")
        escapes = {"n": "\n", "r": "\r", "t": "\t"}
        return re.sub(r"\\(.)", lambda m: escapes.get(m[1], m[1]), body), index + 1
    if token in {"true", "false", "null"}:
        return {"true": True, "false": False, "null": None}[token], index + 1
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return float(token) if "." in token else int(token), index + 1
    raise ValueError("nonliteral")


def nested_calls(source):
    tokens = js_tokens(source)
    sites = []
    control = any(t in {"if", "for", "while", "function", "switch"} for t in tokens)
    for i in range(len(tokens) - 3):
        if tokens[i] == "tools" and tokens[i + 1] == "." and tokens[i + 3] == "(":
            value = None
            try:
                value, end = js_value(tokens, i + 4)
                if tokens[end] != ")":
                    value = None
            except (ValueError, IndexError, json.JSONDecodeError):
                pass
            sites.append(
                {
                    "tool_name": tokens[i + 2],
                    "arguments": value,
                    "execution_relation": "conditional_or_loop" if control else "source_call_site",
                }
            )
    return sites


def classify(command, tool="exec_command"):
    """Conservative shell segments. Paths are mentions unless a segment owns them."""
    if tool.endswith("apply_patch"):
        return [
            {
                "operation_class": "EDIT",
                "command": command,
                "paths_referenced": re.findall(
                    r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", command, re.M
                ),
            }
        ]
    if tool not in {"exec_command", "shell_command", "shell"}:
        return [{"operation_class": "OTHER", "command": command, "paths_referenced": []}]
    if "<<" in command or "$ (" in command or "$(" in command:
        return [{"operation_class": "UNKNOWN", "command": command, "paths_referenced": []}]
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        lex.commenters = "#"
        segments = [[]]
        for token in lex:
            if token and all(c in ";&|\n" for c in token):
                segments.append([])
            else:
                segments[-1].append(token)
    except ValueError:
        return [{"operation_class": "UNKNOWN", "command": command, "paths_referenced": []}]
    result = []
    for args in filter(None, segments):
        while args and re.match(r"^\w+=", args[0]):
            args = args[1:]
        if not args:
            continue
        exe = Path(args[0]).name
        paths = [
            a
            for a in args[1:]
            if not a.startswith("-")
            and ("/" in a or re.search(r"\.(?:md|json|py|gd|cpp|h|toml|yaml|yml)$", a))
        ]
        kind = "UNKNOWN"
        if exe == "cat" and not any(a.startswith(">") for a in args):
            kind = "WHOLE_READ"
        elif exe in {"head", "tail"} or (exe == "sed" and "-n" in args):
            kind = "BOUNDED_READ"
        elif exe in {"rg", "grep", "find"}:
            kind = "SEARCH"
        elif exe == "git":
            kind = (
                "GIT_INSPECT"
                if any(
                    a
                    in {
                        "diff",
                        "show",
                        "log",
                        "status",
                        "rev-parse",
                        "ls-tree",
                        "cat-file",
                        "ls-files",
                        "branch",
                    }
                    for a in args[1:]
                )
                else "OTHER"
            )
        elif exe == "gov":
            kind = "GOV_QUERY"
        elif (
            exe in {"pytest", "unittest"}
            or any(
                re.search(r"(?:verify|validate|check_)[\w.-]*\.py$|(?:verify|test).*\.sh$", a)
                for a in args
            )
            or ("-m" in args and "pytest" in args)
        ):
            kind = "VERIFY"
        elif any(re.search(r"(?:generate|refresh)[\w.-]*\.(?:py|sh)$", a) for a in args):
            kind = "GENERATED_REFRESH"
        elif exe in {
            "cmake",
            "make",
            "ninja",
            "godot",
            "dotnet",
            "cargo",
            "python",
            "python3",
            "node",
            "npm",
        }:
            kind = "RUNTIME_OR_BUILD"
        elif exe in {"pwd", "ls", "stat", "wc", "du", "which", "command", "cd", "uname"}:
            kind = "METADATA"
        elif exe in {"echo", "printf", "true", "sort", "uniq", "sleep"}:
            kind = "OTHER"
        result.append(
            {"operation_class": kind, "command": shlex.join(args), "paths_referenced": paths}
        )
    return result or [{"operation_class": "UNKNOWN", "command": command, "paths_referenced": []}]


def output_metrics(value):
    """Exact recorded text, excluding image/audio blobs. Never regex the body for counts."""
    texts = []
    if isinstance(value, str):
        texts = [value]
    elif isinstance(value, list):
        texts = [x["text"] for x in value if isinstance(x, dict) and isinstance(x.get("text"), str)]
    else:
        return {
            "retained_bytes": None,
            "retained_chars": None,
            "original_tokens": None,
            "truncated": None,
            "retained_tokens": None,
            "text_parts": [],
        }
    originals = []
    truncated = False
    for text in texts:
        # Structured shell result printed by exec; only parse the entire block.
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            obj = None
        values = obj if isinstance(obj, list) else [obj]
        for item in values:
            if isinstance(item, dict) and isinstance(item.get("output"), str):
                if isinstance(item.get("original_token_count"), int):
                    originals.append(item["original_token_count"])
                truncated |= bool(
                    re.match(r"(?:Warning: truncated output|Output exceeded)", item["output"])
                )
                truncated |= bool(re.search(r"^.{0,80}tokens truncated", item["output"], re.M))
        # Shell envelope only, never arbitrary matches in printed source/logs.
        header = text.split("Output:", 1)[0] if "Output:" in text[:1000] else ""
        match = re.search(r"(?mi)^Original token count: (\d+)\s*$", header)
        warning = re.match(r"Warning: truncated output \(original token count: (\d+)\)", text)
        if match:
            originals.append(int(match[1]))
        elif warning:
            originals.append(int(warning[1]))
        truncated |= bool(warning) or bool(re.match(r"Output exceeded", text))
        truncated |= bool(re.search(r"^.{0,80}tokens truncated", text, re.M))
    return {
        "retained_bytes": sum(len(t.encode()) for t in texts),
        "retained_chars": sum(map(len, texts)),
        "retained_tokens": None,
        "original_tokens": sum(originals) if originals else None,
        "truncated": truncated,
        "text_parts": texts,
    }


def historical(repo, sha):
    def git(*args):
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
        return p.stdout if p.returncode == 0 else None

    if not sha or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return {"status": "missing_sha", "documents": None}
    tree = git("ls-tree", "-r", sha)
    if tree is None:
        return {"status": "unavailable_sha", "documents": None}
    docs = {}
    for line in tree.decode().splitlines():
        metadata, path = line.split("\t", 1)
        if (
            Path(path).name in {"AGENTS.md", "CLAUDE.md"}
            or path
            in {
                "config/governance/project.json",
                "config/project/policy_pack.json",
                ".governance/workspace.json",
                "gov",
            }
            or "workspace-governance" in path
        ):
            docs[path] = metadata.split()[2]
    return {
        "status": "git_candidate_not_effective_state",
        "documents": docs,
        "gov_available": "gov" in docs,
    }


def surface_names(operation, rds):
    names = set()
    for path in operation["paths_referenced"]:
        if path.endswith("docs/BACKLOG.md"):
            names.add("docs/BACKLOG.md")
        if Path(path).name == "CURRENT_STATE.md":
            names.add("CURRENT_STATE.md")
        if Path(path).name == "AGENTS.md":
            names.add("AGENTS.md")
        if Path(path).name == "policy_pack.json":
            names.add("policy_pack.json")
        if "docs/rds" in path:
            names.add("docs/rds/")
        for current in rds:
            if path.endswith(current):
                names.add(current)
    if operation["operation_class"] == "GOV_QUERY":
        names.add("./gov")
    return sorted(names)


def write_csv(path, rows, fields):
    with path.open("x") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--source-root", action="append", required=True)
    parser.add_argument("--cwd", action="append", required=True)
    parser.add_argument("--output", default="analysis/results/routing_analysis_v1")
    args = parser.parse_args()
    root = DataRoot(args.data_root)
    dest = root.destination(args.output)

    inventory = []
    # Freeze byte extents before parsing, including active append-only transcripts.
    for source in sorted(p for r in args.source_root for p in Path(r).rglob("rollout-*.jsonl")):
        with source.open("rb") as f:
            meta = json.loads(f.readline())["payload"]
        cwd = meta.get("cwd", "")
        if cwd not in args.cwd:
            continue
        inventory.append(
            {"path": str(source), "frozen_extent": source.stat().st_size, "metadata": meta}
        )
    if not inventory:
        parser.exit(2, "routing-analysis: no_matching_inputs\n")

    dest.mkdir(mode=0o700, parents=True, exist_ok=False)
    db = sqlite3.connect(dest / "routing.sqlite")
    for table in [
        "logical_calls",
        "raw_calls",
        "outputs",
        "operations",
        "session_summary",
        "usage_records",
        "world_states",
    ]:
        db.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, data TEXT NOT NULL)")

    def put(table, key, row):
        db.execute(f"INSERT INTO {table} VALUES (?,?)", (key, json.dumps(row, ensure_ascii=False)))

    counts = collections.Counter()
    formats = collections.Counter()
    seen = {}
    usage_seen = {}
    eras = {}
    era_rows = []
    samples = {"calls": [], "truncations": [], "wrappers": [], "eras": []}
    rds = sorted(
        str(p.relative_to(args.repo)) for p in Path(args.repo).glob("docs/rds/*") if p.is_file()
    )
    surface = collections.defaultdict(
        lambda: {
            "sessions": set(),
            "calls": set(),
            "counts": collections.Counter(),
            "retained": 0,
            "original": 0,
            "output_groups": set(),
            "truncated_groups": 0,
        }
    )
    for item in inventory:
        with Path(item["path"]).open("rb") as f:
            raw = f.read(item["frozen_extent"])
        raw = raw[: raw.rfind(b"\n") + 1]
        source_hash = digest(raw)
        saved = root.preserve_raw("codex", raw)
        item.update({"bytes": len(raw), "sha256": source_hash, "snapshot": str(saved)})
        meta = item["metadata"]
        session = meta.get("id") or source_hash
        sha = (meta.get("git") or {}).get("commit_hash")
        if sha not in eras:
            eras[sha] = historical(args.repo, sha)
        era = eras[sha]
        era_id = digest({"git_candidate": era, "cli_version": meta.get("cli_version")})
        erow = {
            "session_id": session,
            "repo_sha": sha,
            "era_id": era_id,
            "state": era["status"],
            "gov_available": era.get("gov_available"),
            "cli_version": meta.get("cli_version"),
            "components_json": json.dumps(era["documents"], sort_keys=True),
            "effective_instructions": "unknown; inspect captured world_states separately",
        }
        era_rows.append(erow)
        if len(samples["eras"]) < 5 and era["status"].startswith("git_"):
            samples["eras"].append(erow)
        events = []
        outputs = collections.defaultdict(list)
        model = None
        tasks = []
        state_refs = []
        session_counts = collections.Counter()
        session_usage = collections.Counter()
        usage_n = 0
        for line, raw_line in enumerate(io.BytesIO(raw), 1):
            event = json.loads(raw_line)
            q = event.get("payload", {})
            ref = source_hash + ":" + str(line)
            typ = event["type"]
            counts[typ] += 1
            if typ == "turn_context":
                model = q.get("model", model)
            if typ == "world_state":
                put(
                    "world_states",
                    ref,
                    {
                        "session_id": session,
                        "raw_event_id": ref,
                        "full": q.get("full"),
                        "payload_hash": digest(q),
                        "agents_md_hash": digest(q.get("state", {}).get("agents_md"))
                        if q.get("state", {}).get("agents_md")
                        else None,
                    },
                )
                state_refs.append(ref)
            if typ == "token_usage_record":
                key = q.get("response_id") or ref
                usage = q.get("usage", {})
                fingerprint = digest(usage)
                if key in usage_seen:
                    counts[
                        "usage_duplicate" if usage_seen[key] == fingerprint else "usage_conflict"
                    ] += 1
                else:
                    usage_seen[key] = fingerprint
                    put(
                        "usage_records",
                        key,
                        {
                            "session_id": session,
                            "raw_event_id": ref,
                            "response_id": q.get("response_id"),
                            "usage": usage,
                        },
                    )
                    usage_n += 1
                    for k, v in usage.items():
                        if isinstance(v, int):
                            session_usage[k] += v
            if typ != "response_item":
                continue
            qt = q.get("type")
            counts["response:" + str(qt)] += 1
            if qt == "message" and q.get("role") == "user":
                tasks.append(
                    {
                        "raw_event_id": ref,
                        "text": "\n".join(
                            x.get("text", "") for x in q.get("content", []) if isinstance(x, dict)
                        ),
                    }
                )
            if qt in {"function_call_output", "custom_tool_call_output"}:
                outputs[q.get("call_id")].append((ref, q.get("output")))
            if qt in {"function_call", "custom_tool_call"}:
                events.append((ref, event, model))
                counts["tool:" + str(q.get("name"))] += 1
        for ref, event, model in events:
            q = event["payload"]
            cid = q.get("call_id") or ref
            call_signature = digest(
                {"name": q.get("name"), "arguments": q.get("arguments"), "input": q.get("input")}
            )
            put(
                "raw_calls",
                ref,
                {
                    "session_id": session,
                    "call_id": cid,
                    "signature": call_signature,
                    "raw_event_id": ref,
                },
            )
            if cid in seen:
                counts["mirrored_calls" if seen[cid] == call_signature else "call_id_conflict"] += 1
                continue
            seen[cid] = call_signature
            tool = q.get("name", "UNKNOWN")
            command = q.get("input", q.get("arguments", ""))
            params = None
            if "arguments" in q:
                try:
                    params = json.loads(q["arguments"])
                except (ValueError, TypeError):
                    pass
            output_refs = []
            output_group = []
            for output_ref, value in outputs.get(q.get("call_id"), []):
                metrics = output_metrics(value)
                output_refs.append(output_ref)
                metrics.update(
                    {"logical_call_id": cid, "raw_event_id": output_ref, "session_id": session}
                )
                put("outputs", output_ref, metrics)
                output_group.append(metrics)
                if metrics["truncated"] and len(samples["truncations"]) < 8:
                    samples["truncations"].append(
                        {k: v for k, v in metrics.items() if k != "text_parts"}
                    )
            if not output_refs:
                counts["unpaired_calls"] += 1
            sites = nested_calls(command) if tool == "exec" else []
            normalized_tool = tool
            relation = "direct"
            action_args = params
            if tool == "exec":
                counts["wrapper_sites:" + str(min(len(sites), 3))] += 1
                if (
                    len(sites) == 1
                    and sites[0]["arguments"] is not None
                    and sites[0]["execution_relation"] == "source_call_site"
                ):
                    normalized_tool = sites[0]["tool_name"]
                    action_args = sites[0]["arguments"]
                    relation = "single_literal_site_same_wrapper_id"
                else:
                    relation = "unresolved_wrapper"
                    counts["unresolved_wrappers"] += 1
            if isinstance(action_args, dict):
                action = action_args.get("cmd", action_args.get("command", json.dumps(action_args)))
                cwd = action_args.get("workdir", action_args.get("cwd", meta.get("cwd")))
            elif isinstance(action_args, str):
                action = action_args
                cwd = meta.get("cwd")
            else:
                action, cwd = command, meta.get("cwd")
            ops = (
                classify(action, normalized_tool)
                if relation != "unresolved_wrapper"
                else [{"operation_class": "UNKNOWN", "command": action, "paths_referenced": []}]
            )
            retained = sum(x["retained_bytes"] or 0 for x in output_group) if output_group else None
            original_values = [
                x["original_tokens"] for x in output_group if x["original_tokens"] is not None
            ]
            original = sum(original_values) if original_values else None
            op_classes = sorted(set(o["operation_class"] for o in ops))
            row = {
                "session_id": session,
                "logical_call_id": cid,
                "raw_event_ids": [ref, *output_refs],
                "timestamp": event.get("timestamp"),
                "agent": meta.get("source"),
                "model": model,
                "tool_name": normalized_tool,
                "recorded_tool_name": tool,
                "command_or_action": action,
                "cwd": cwd,
                "paths_referenced": sorted(set(p for o in ops for p in o["paths_referenced"])),
                "operation_class": op_classes[0] if len(op_classes) == 1 else "UNKNOWN",
                "operation_classes": op_classes,
                "output_original_bytes": None,
                "output_original_tokens": original,
                "output_retained_bytes": retained,
                "output_retained_tokens": None,
                "output_truncated": any(o["truncated"] for o in output_group)
                if output_group
                else None,
                "usage_input_tokens": None,
                "usage_cached_input_tokens": None,
                "usage_uncached_input_tokens": None,
                "usage_output_tokens": None,
                "usage_reasoning_tokens": None,
                "trace_format": q["type"],
                "parser_version": VERSION,
                "classification_confidence": "mechanical_partial"
                if relation != "unresolved_wrapper"
                else "unknown",
                "relation": relation,
                "nested_source_sites": sites,
                "era_id": era_id,
            }
            put("logical_calls", cid, row)
            formats[q["type"]] += 1
            session_counts.update(op_classes)
            if len(samples["calls"]) < 40 or (
                relation.startswith("single_") and len(samples["wrappers"]) < 8
            ):
                sample = {
                    k: row[k]
                    for k in [
                        "logical_call_id",
                        "raw_event_ids",
                        "tool_name",
                        "relation",
                        "operation_classes",
                        "command_or_action",
                    ]
                }
                if len(samples["calls"]) < 40:
                    samples["calls"].append(sample)
                if relation.startswith("single_") and len(samples["wrappers"]) < 8:
                    samples["wrappers"].append(sample)
            touched = set()
            for ordinal, op in enumerate(ops):
                op.update(
                    {
                        "logical_call_id": cid,
                        "session_id": session,
                        "raw_event_id": ref,
                        "surfaces": surface_names(op, rds),
                    }
                )
                put("operations", cid + ":" + str(ordinal), op)
                for name in op["surfaces"]:
                    s = surface[name]
                    s["sessions"].add(session)
                    s["calls"].add(cid)
                    s["counts"][op["operation_class"]] += 1
                    touched.add(name)
            for name in touched:
                s = surface[name]
                s["retained"] += retained or 0
                s["original"] += original or 0
                s["output_groups"].update(output_refs)
                s["truncated_groups"] += sum(bool(o["truncated"]) for o in output_group)
        put(
            "session_summary",
            source_hash,
            {
                "session_id": session,
                "source_hash": source_hash,
                "cwd": meta.get("cwd"),
                "source": meta.get("source"),
                "timestamp": meta.get("timestamp"),
                "era_id": era_id,
                "base_repo_sha": sha,
                "end_repo_sha": None,
                "final_diff_summary": None,
                "task_messages": tasks,
                "operation_class_call_counts": dict(session_counts),
                "usage": dict(session_usage) if usage_n else None,
                "usage_record_count": usage_n,
                "world_state_refs": state_refs,
                "provenance": "metadata SHA is start candidate; no dirty state or end inference",
            },
        )
        db.commit()
    surface_rows = []
    for name in sorted(
        set(surface)
        | set(rds)
        | {
            "docs/BACKLOG.md",
            "CURRENT_STATE.md",
            "AGENTS.md",
            "policy_pack.json",
            "docs/rds/",
            "./gov",
        }
    ):
        s = surface[name]
        surface_rows.append(
            {
                "surface": name,
                "sessions_touched": len(s["sessions"]),
                "logical_calls": len(s["calls"]),
                **{c: s["counts"][c] for c in CLASSES},
                "associated_group_retained_bytes": s["retained"],
                "associated_group_known_original_tokens": s["original"],
                "output_groups": len(s["output_groups"]),
                "truncated_groups": s["truncated_groups"],
            }
        )
    write_csv(dest / "surface_operations.csv", surface_rows, list(surface_rows[0]))
    write_csv(dest / "governance_eras.csv", era_rows, list(era_rows[0]))
    summary = []
    for label, predicate in [
        ("all", "1"),
        ("truncated", "json_extract(data,'$.truncated')=1"),
        ("not_explicitly_truncated", "json_extract(data,'$.truncated')=0"),
    ]:
        r = db.execute(
            f"SELECT count(*), sum(json_extract(data,'$.retained_bytes')),sum(json_extract(data,'$.original_tokens')),count(json_extract(data,'$.original_tokens')) FROM outputs WHERE {predicate}"
        ).fetchone()
        summary.append(
            dict(
                zip(
                    [
                        "scope",
                        "output_records",
                        "retained_text_bytes",
                        "known_original_tokens",
                        "original_count_coverage",
                    ],
                    [label, *r],
                )
            )
        )
    write_csv(dest / "retained_output_summary.csv", summary, list(summary[0]))
    report = {
        "parser_version": VERSION,
        "session_count": len(inventory),
        "bytes": sum(i["bytes"] for i in inventory),
        "gib": sum(i["bytes"] for i in inventory) / 2**30,
        "counts": dict(counts),
        "logical_calls": len(seen),
        "formats": dict(formats),
        "sources": inventory,
        "current_rds_files_inventory_only": rds,
        "era_states": dict(collections.Counter(r["state"] for r in era_rows)),
        "source_kinds": dict(
            collections.Counter(
                json.dumps(i["metadata"].get("source"), sort_keys=True) for i in inventory
            )
        ),
        "date_range": [
            min(i["metadata"].get("timestamp", "") for i in inventory),
            max(i["metadata"].get("timestamp", "") for i in inventory),
        ],
        "status": "machine_pass_manual_validation_pending",
    }
    (dest / "corpus_inventory.json").write_text(json.dumps(report, indent=2) + "\n")
    (dest / "validation_samples.json").write_text(json.dumps(samples, indent=2) + "\n")
    db.execute("CREATE INDEX operation_call ON operations(json_extract(data,'$.logical_call_id'))")
    db.execute("CREATE INDEX call_session ON logical_calls(json_extract(data,'$.session_id'))")
    db.commit()
    db.close()
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"sources", "source_kinds"}}, indent=2
        )
    )


if __name__ == "__main__":
    main()
