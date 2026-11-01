"""One-time deterministic migration generator for canonical schema 0.2."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "harness_observatory" / "schemas"
KINDS = ("run", "event", "provenance", "governance_snapshot")


def obj(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


def write(name, schema):
    (ROOT / name).write_text(
        json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}, indent=2)
        + "\n"
    )


def main():
    legacy = ROOT / "v0.1"
    legacy.mkdir(exist_ok=True)
    for kind in KINDS:
        source = ROOT / f"{kind}.json"
        target = legacy / f"{kind}.json"
        if not target.exists():
            target.write_bytes(source.read_bytes())

    string = {"type": "string"}
    nullable_string = {"type": ["string", "null"]}
    provenance = obj(
        {
            "schema_version": {"const": "0.2"},
            "source": string,
            "method": string,
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence_refs": {"type": "array", "items": string, "minItems": 1},
            "extractor": string,
        }
    )
    fact = obj(
        {
            "value": {"type": ["string", "number", "boolean", "null"]},
            "provenance": {"$ref": "#/$defs/provenance"},
        }
    )
    fact_names = """session_id cwd repository branch start_sha end_sha model input_tokens output_tokens
cache_read_tokens cache_write_tokens cost duration_seconds observed_span_seconds started_at finished_at
lifecycle_status command_count tool_call_count user_turn_count compaction_count ci_observation_count
ci_poll_count""".split()
    run = obj(
        {
            "schema_version": {"const": "0.2"},
            "extractor_version": string,
            "run_id": string,
            "agent": {"enum": ["codex", "claude"]},
            "source_id": string,
            "governance_snapshot_id": nullable_string,
            "governance_state": {
                "enum": [
                    "not_reconstructed",
                    "none_existed",
                    "reconstructed",
                    "not_applicable",
                    "reconstruction_failed",
                ]
            },
            "poll_definition_version": {"enum": ["1", "2"]},
            "facts": obj({name: {"$ref": "#/$defs/fact"} for name in fact_names}),
            "labels": {
                "type": "array",
                "items": obj(
                    {
                        "namespace": string,
                        "label": string,
                        "provenance": {"$ref": "#/$defs/provenance"},
                    }
                ),
            },
        }
    )
    run["$defs"] = {"provenance": provenance, "fact": fact}

    event_types = """run.started run.finished run.compacted user.message agent.message tool.started tool.finished
command.started command.finished file.read file.write test.started test.finished ci.run_started ci.status_query
ci.blocking_watch ci.run_list_query ci.pr_check_query ci.artifact_query ci.run_finished git.commit git.diff
governance.discovered user.intervention agent.stop agent.handoff""".split()
    data = obj(
        {
            "call_id": string,
            "tool_name": string,
            "ci_run_id": string,
            "path": nullable_string,
            "status": string,
            "compaction_index": {"type": "integer", "minimum": 1},
            "observation_kind": string,
            "observed_sha": {"$ref": "#/$defs/fact"},
            "ci_status": {"$ref": "#/$defs/fact"},
            "ci_conclusion": {"$ref": "#/$defs/fact"},
            "ci_created_at": {"$ref": "#/$defs/fact"},
            "ci_started_at": {"$ref": "#/$defs/fact"},
            "ci_completed_at": {"$ref": "#/$defs/fact"},
        },
        [],
    )
    event = obj(
        {
            "schema_version": {"const": "0.2"},
            "run_id": string,
            "seq": {"type": "integer", "minimum": 0},
            "timestamp": {"type": ["string", "null"], "format": "date-time"},
            "event_type": {"enum": event_types},
            "source": {"enum": ["codex", "claude", "git", "github_actions", "governance"]},
            "data": data,
            "provenance": {"$ref": "#/$defs/provenance"},
        }
    )
    event["$defs"] = {"provenance": provenance, "fact": fact}
    event["allOf"] = [
        {
            "if": {"properties": {"event_type": {"enum": ["tool.started"]}}},
            "then": {"properties": {"data": {"required": ["call_id", "tool_name"]}}},
        },
        {
            "if": {
                "properties": {
                    "event_type": {"enum": ["tool.finished", "command.started", "command.finished"]}
                }
            },
            "then": {"properties": {"data": {"required": ["call_id"]}}},
        },
        {
            "if": {"properties": {"event_type": {"enum": ["file.read", "file.write"]}}},
            "then": {"properties": {"data": {"required": ["call_id", "path", "status"]}}},
        },
        {
            "if": {
                "properties": {
                    "event_type": {
                        "enum": ["ci.status_query", "ci.blocking_watch", "ci.artifact_query"]
                    }
                }
            },
            "then": {
                "properties": {"data": {"required": ["call_id", "ci_run_id", "observation_kind"]}}
            },
        },
        {
            "if": {
                "properties": {"event_type": {"enum": ["ci.run_list_query", "ci.pr_check_query"]}}
            },
            "then": {"properties": {"data": {"required": ["call_id", "observation_kind"]}}},
        },
        {
            "if": {"properties": {"event_type": {"const": "run.compacted"}}},
            "then": {"properties": {"data": {"required": ["compaction_index"]}}},
        },
    ]
    governance = obj(
        {
            "schema_version": {"const": "0.2"},
            "governance_snapshot_id": string,
            "repo_sha": string,
            "governance_discovery_version": string,
            "scope_path": string,
            "state": {
                "enum": ["none_existed", "reconstructed", "not_applicable", "reconstruction_failed"]
            },
            "documents": {
                "type": "array",
                "items": obj(
                    {
                        "path": string,
                        "sha256": string,
                        "kind": {"enum": ["agents", "claude", "declared"]},
                        "applicable": {"type": "boolean"},
                    }
                ),
            },
            "missing_optional": {"type": "array", "items": string},
            "coverage": {"const": "historical_scoped_candidates"},
            "provenance": {"$ref": "#/$defs/provenance"},
        }
    )
    governance["$defs"] = {"provenance": provenance}
    write("provenance.json", provenance)
    write("run.json", run)
    write("event.json", event)
    write("governance_snapshot.json", governance)


if __name__ == "__main__":
    main()
