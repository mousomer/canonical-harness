"""Adapters for observed Codex rollout and Claude Code JSONL record families."""

import json
import re
import shlex
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from .model import (
    EXTRACTOR_VERSION,
    POLL_DEFINITION_VERSION,
    VERSION,
    digest,
    fact,
    provenance,
    timestamp,
    validate_bundle,
)
from .redaction import sanitize


class RecordError(ValueError):
    pass


def extract(raw, provider, *, sanitize_output=True):
    if provider not in {"codex", "claude"}:
        raise ValueError("Unsupported provider")
    source_id = digest(raw)
    run_id = digest((provider + source_id).encode())
    events, warnings, observed = [], [], []
    facts, calls = {}, {}
    token_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    token_seen = set()
    compactions = 0
    ci_states = {}
    v2_poll_count = 0
    observed_shas = []

    def warn(line, code, record_type=None):
        warning = {"line": line, "code": code, "source_id": source_id}
        if isinstance(record_type, str):
            warning["source_record_type"] = record_type[:100]
        warnings.append(warning)

    def put(name, value, line):
        if value is not None and not isinstance(value, str):
            raise RecordError()
        if (
            name == "session_id"
            and name in facts
            and value is not None
            and facts[name]["value"] != value
        ):
            raise RecordError()
        if value is not None and name not in facts:
            facts[name] = fact(value, provider, "direct", [f"{source_id}#L{line}"])

    def emit(kind, data, line, ts):
        events.append(
            dict(
                schema_version=VERSION,
                run_id=run_id,
                seq=len(events),
                timestamp=ts,
                event_type=kind,
                source=provider,
                data=data,
                provenance=provenance(provider, "source_record", [f"{source_id}#L{line}"]),
            )
        )

    def tool(name, call_id, args, line, ts):
        nonlocal v2_poll_count
        if not isinstance(name, str) or not isinstance(call_id, str) or not isinstance(args, dict):
            raise RecordError()
        calls[call_id] = {"name": name, "args": args}
        emit("tool.started", {"call_id": call_id, "tool_name": name}, line, ts)
        command = (
            args.get("cmd")
            if name == "exec_command"
            else args.get("command")
            if name == "Bash"
            else None
        )
        if isinstance(command, str):
            emit("command.started", {"call_id": call_id}, line, ts)
            # Narrow recognition: an attempted explicit query, never inferred completion detection.
            try:
                words = shlex.split(command)
            except ValueError:
                words = []
                warn(line, "unsupported_shell_syntax")
            ci_id = None
            ci_event = None
            if len(words) >= 4 and words[:3] == ["gh", "run", "view"] and words[3].isdigit():
                ci_id = words[3]
                ci_event = "ci.status_query"
            elif len(words) >= 4 and words[:3] == ["gh", "run", "watch"] and words[3].isdigit():
                ci_id = words[3]
                ci_event = "ci.blocking_watch"
            elif len(words) >= 3 and words[:3] == ["gh", "run", "list"]:
                ci_event = "ci.run_list_query"
            elif len(words) >= 3 and words[:3] == ["gh", "pr", "checks"]:
                ci_event = "ci.pr_check_query"
            elif len(words) >= 4 and words[:3] == ["gh", "run", "download"] and words[3].isdigit():
                ci_id = words[3]
                ci_event = "ci.artifact_query"
            elif len(words) >= 3 and words[:2] == ["gh", "api"]:
                match = re.fullmatch(r"/?repos/[^/]+/[^/]+/actions/runs/(\d+)(?:\?.*)?", words[2])
                if match:
                    ci_id = match[1]
                    ci_event = "ci.status_query"
            if ci_event:
                data = {"call_id": call_id, "observation_kind": ci_event.removeprefix("ci.")}
                if ci_id:
                    data["ci_run_id"] = ci_id
                is_poll = (
                    ci_event == "ci.status_query"
                    and ci_id
                    and ci_states.get(ci_id)
                    in {"queued", "in_progress", "pending", "requested", "waiting"}
                )
                if is_poll:
                    data["status"] = "poll_v2"
                    v2_poll_count += 1
                emit(ci_event, data, line, ts)
        if name in {"Read", "Write", "Edit"}:
            if args.get("file_path") is not None and not isinstance(args["file_path"], str):
                raise RecordError()
            emit(
                "file.read" if name == "Read" else "file.write",
                {"call_id": call_id, "path": args.get("file_path"), "status": "attempted"},
                line,
                ts,
            )

    def result(call_id, line, ts, body=None):
        if not isinstance(call_id, str):
            raise RecordError()
        data = {"call_id": call_id, "status": "response_observed"}
        if call_id not in calls:
            warn(line, "orphan_tool_result")
        else:
            structured = parse_tool_result(calls[call_id], body, provider, source_id, line)
            data.update(structured)
            if structured.get("observed_sha"):
                observed_shas.append(structured["observed_sha"])
            ci_id = structured.get("ci_run_id")
            status = structured.get("ci_status")
            if ci_id and status:
                ci_states[ci_id] = status["value"]
        emit("tool.finished", data, line, ts)
        # A yielded exec response is not proof that the process finished.

    for line, raw_line in enumerate(raw.splitlines(), 1):
        if not raw_line.strip():
            warn(line, "blank_record")
            continue
        checkpoint = (len(events), len(observed), facts.copy(), calls.copy())
        try:
            row = json.loads(raw_line)
            if not isinstance(row, dict):
                raise RecordError()
            ts = timestamp(row.get("timestamp"))
            if ts:
                observed.append(ts)
            else:
                warn(line, "missing_timestamp")
            typ = row.get("type")
            if provider == "codex":
                p = row.get("payload")
                if not isinstance(p, dict):
                    raise RecordError()
                if typ == "session_meta":
                    put("session_id", p.get("id", p.get("session_id")), line)
                    put("cwd", p.get("cwd"), line)
                    git = p.get("git") or {}
                    if not isinstance(git, dict):
                        raise RecordError()
                    for name, key in [
                        ("start_sha", "commit_hash"),
                        ("branch", "branch"),
                        ("repository", "repository_url"),
                    ]:
                        value = git.get(key)
                        if name == "repository" and isinstance(value, str):
                            value = normalize_repository(value)
                        put(name, value, line)
                    emit("run.started", {}, line, ts)
                elif typ == "turn_context":
                    put("model", p.get("model"), line)
                elif typ == "compacted":
                    compactions += 1
                    emit("run.compacted", {"compaction_index": compactions}, line, ts)
                elif typ == "token_usage_record":
                    usage = p.get("usage") or p.get("turn_token_usage")
                    if isinstance(usage, dict):
                        accumulate_usage(usage, token_totals, token_seen, f"{source_id}#L{line}")
                elif typ == "response_item":
                    sub = p.get("type")
                    if sub == "message" and p.get("role") in {"user", "assistant"}:
                        if not isinstance(p.get("content"), list):
                            raise RecordError()
                        emit(
                            "user.message" if p["role"] == "user" else "agent.message", {}, line, ts
                        )
                    elif sub in {"function_call", "custom_tool_call"}:
                        args = (
                            json.loads(p.get("arguments", "{}")) if sub == "function_call" else {}
                        )
                        tool(p.get("name"), p.get("call_id"), args, line, ts)
                    elif sub in {"function_call_output", "custom_tool_call_output"}:
                        result(p.get("call_id"), line, ts, p.get("output"))
                    else:
                        warn(line, "unsupported_response_item")
                elif typ == "event_msg" and p.get("type") == "turn_aborted":
                    emit("agent.stop", {}, line, ts)
                else:
                    warn(line, "unsupported_or_mirror_record", typ)
            else:
                for name, key in [
                    ("session_id", "sessionId"),
                    ("cwd", "cwd"),
                    ("branch", "gitBranch"),
                ]:
                    put(name, row.get(key), line)
                if typ not in {"user", "assistant"}:
                    warn(line, "unsupported_record", typ)
                    continue
                if row.get("isSidechain") or row.get("isMeta"):
                    warn(line, "sidechain_or_meta_excluded")
                    continue
                message = row.get("message")
                if not isinstance(message, dict):
                    raise RecordError()
                put("model", message.get("model"), line)
                usage = message.get("usage")
                if isinstance(usage, dict):
                    accumulate_usage(usage, token_totals, token_seen, f"{source_id}#L{line}")
                content = message.get("content")
                if isinstance(content, str):
                    content = [{"type": "text"}]
                if not isinstance(content, list):
                    raise RecordError()
                emitted_message = False
                for block in content:
                    if not isinstance(block, dict):
                        raise RecordError()
                    if block.get("type") == "text":
                        if not emitted_message:
                            emit("user.message" if typ == "user" else "agent.message", {}, line, ts)
                            emitted_message = True
                    elif block.get("type") == "tool_use":
                        tool(block.get("name"), block.get("id"), block.get("input"), line, ts)
                    elif block.get("type") == "tool_result":
                        result(block.get("tool_use_id"), line, ts, block.get("content"))
                    else:
                        warn(line, "unsupported_content_block")
        except (ValueError, TypeError, AttributeError, KeyError, UnicodeDecodeError):
            count, span, previous_facts, previous_calls = checkpoint
            del events[count:]
            del observed[span:]
            facts, calls = previous_facts, previous_calls
            warn(line, "malformed_record")

    for name in [
        "session_id",
        "cwd",
        "repository",
        "branch",
        "start_sha",
        "end_sha",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost",
        "duration_seconds",
        "observed_span_seconds",
        "started_at",
        "finished_at",
        "lifecycle_status",
    ]:
        facts.setdefault(name, fact(None, provider, "unavailable", [source_id]))
    if observed:
        start = min(observed, key=datetime.fromisoformat)
        end = max(observed, key=datetime.fromisoformat)
        facts["observed_span_seconds"] = fact(
            (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(),
            provider,
            "timestamp_span",
            [source_id],
        )
        facts["started_at"] = fact(start, provider, "first_observed_timestamp", [source_id], 0.8)
    facts["lifecycle_status"] = fact("finish_unobserved", provider, "source_eof", [source_id], 0.5)
    if observed_shas:
        if facts["start_sha"]["value"] is None:
            facts["start_sha"] = observed_shas[0]
        facts["end_sha"] = observed_shas[-1]
    for name, value in token_totals.items():
        facts[name] = fact(
            value if token_seen else None,
            provider,
            "deduplicated_usage_sum",
            sorted(token_seen) or [source_id],
            0.9 if token_seen else 1.0,
        )
    for name, kind in [
        ("command_count", "command.started"),
        ("tool_call_count", "tool.started"),
        ("user_turn_count", "user.message"),
        ("compaction_count", "run.compacted"),
    ]:
        facts[name] = fact(
            sum(e["event_type"] == kind for e in events),
            provider,
            "observed_event_count",
            [source_id],
        )
    ci_types = {
        "ci.status_query",
        "ci.blocking_watch",
        "ci.run_list_query",
        "ci.pr_check_query",
        "ci.artifact_query",
    }
    facts["ci_observation_count"] = fact(
        sum(e["event_type"] in ci_types for e in events),
        provider,
        "observed_event_count",
        [source_id],
    )
    facts["ci_poll_count"] = fact(
        v2_poll_count,
        provider,
        "poll_definition_v2",
        [source_id],
    )
    bundle = dict(
        run=dict(
            schema_version=VERSION,
            extractor_version=EXTRACTOR_VERSION,
            run_id=run_id,
            agent=provider,
            source_id=source_id,
            governance_snapshot_id=None,
            governance_state="not_reconstructed",
            poll_definition_version=POLL_DEFINITION_VERSION,
            facts=facts,
            labels=[],
        ),
        events=events,
        warnings=warnings,
        governance_snapshots=[],
        git_changes=[],
        ci_runs=[],
    )
    if sanitize_output:
        bundle = sanitize(bundle)
    validate_bundle(bundle)
    return bundle


def normalize_repository(value):
    """Canonicalize common Git identifiers without querying a repository."""
    value = value.strip()
    scp = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", value)
    if scp and "://" not in value:
        host, path = scp.groups()
        value = f"https://{host}/{path}"
    parts = urlsplit(value)
    if parts.scheme and parts.hostname:
        host = parts.hostname.lower()
        port = f":{parts.port}" if parts.port else ""
        path = parts.path.rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return urlunsplit((parts.scheme.lower(), host + port, path, "", ""))
    return value.removesuffix(".git").rstrip("/")


def parse_tool_result(call, body, provider, source_id, line):
    """Extract a fixed token vocabulary; never return surrounding tool output."""
    if isinstance(body, list):
        body = "\n".join(
            block.get("text", "") if isinstance(block, dict) else "" for block in body[:20]
        )
    if not isinstance(body, str):
        return {}
    body = body[:65536]
    name, args = call["name"], call["args"]
    command = (
        args.get("cmd")
        if name == "exec_command"
        else args.get("command")
        if name == "Bash"
        else None
    )
    if not isinstance(command, str):
        return {}
    try:
        words = shlex.split(command)
    except ValueError:
        return {}
    reference = f"{source_id}#L{line}"
    structured = {}
    if words[:3] in (["git", "rev-parse", "HEAD"], ["git", "rev-parse", "--verify"]):
        matches = re.findall(r"(?im)(?<![0-9a-f])([0-9a-f]{40})(?![0-9a-f])", body)
        if len(set(matches)) == 1:
            structured["observed_sha"] = fact(
                matches[0], provider, "git_command_output", [reference], 0.95
            )
    ci_command = words[:3] in (
        ["gh", "run", "view"],
        ["gh", "run", "watch"],
        ["gh", "run", "download"],
    ) or words[:2] == ["gh", "api"]
    if not ci_command:
        return structured
    run_ids = re.findall(r'(?i)"(?:id|databaseId)"\s*:\s*(\d+)', body)
    if len(set(run_ids)) == 1:
        structured["ci_run_id"] = run_ids[0]
    elif len(words) >= 4 and words[3].isdigit():
        structured["ci_run_id"] = words[3]
    for output, key in [("ci_status", "status"), ("ci_conclusion", "conclusion")]:
        values = re.findall(rf'(?i)"{key}"\s*:\s*"([a-z_ -]+)"', body)
        if len(set(values)) == 1:
            structured[output] = fact(
                values[0].lower().replace(" ", "_"),
                provider,
                "ci_command_output",
                [reference],
                0.95,
            )
    for output, key in [
        ("ci_created_at", "createdAt"),
        ("ci_started_at", "startedAt"),
        ("ci_completed_at", "completedAt"),
    ]:
        values = re.findall(rf'"{key}"\s*:\s*"([^"\r\n]{{10,40}})"', body)
        if len(set(values)) == 1:
            try:
                value = timestamp(values[0])
            except ValueError:
                continue
            structured[output] = fact(value, provider, "ci_command_output", [reference], 0.95)
    return structured


def accumulate_usage(usage, totals, seen, reference):
    """Sum one observed usage object once; missing meanings stay absent."""
    if reference in seen:
        return
    mapping = {
        "input_tokens": "input_tokens",
        "prompt_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "completion_tokens": "output_tokens",
        "cache_read_input_tokens": "cache_read_tokens",
        "cached_tokens": "cache_read_tokens",
        "cache_creation_input_tokens": "cache_write_tokens",
    }
    found = False
    for source, target in mapping.items():
        value = usage.get(source)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            totals[target] += value
            found = True
    if found:
        seen.add(reference)


def inspect_source(raw, provider):
    bundle = extract(raw, provider)
    return {
        "provider": provider,
        "source_id": bundle["run"]["source_id"],
        "event_count": len(bundle["events"]),
        "warnings": bundle["warnings"],
        "available_facts": sorted(
            k for k, v in bundle["run"]["facts"].items() if v["value"] is not None
        ),
    }
