"""Small canonical model; serialization and validation are shared by every adapter."""

import hashlib
import json
from datetime import datetime, timezone
from functools import lru_cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker

VERSION = "0.2"
EXTRACTOR_VERSION = "0.3.0"
POLL_DEFINITION_VERSION = "2"
EVENT_TYPES = """run.started run.finished user.message agent.message tool.started tool.finished
command.started command.finished file.read file.write test.started test.finished ci.run_started
ci.status_query ci.blocking_watch ci.run_list_query ci.pr_check_query ci.artifact_query
ci.run_finished git.commit git.diff governance.discovered user.intervention agent.stop
agent.handoff""".split()


def dumps(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def digest(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def timestamp(value):
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timezone required")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def provenance(source, method, refs, confidence=1.0):
    return dict(
        schema_version=VERSION,
        source=source,
        method=method,
        confidence=confidence,
        evidence_refs=refs,
        extractor=f"harness-observatory/{EXTRACTOR_VERSION}",
    )


def fact(value, source, method, refs, confidence=1.0):
    return {"value": value, "provenance": provenance(source, method, refs, confidence)}


FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("date-time", raises=(ValueError, TypeError, AttributeError))
def valid_datetime(value):
    if not isinstance(value, str):
        return True
    return "T" in value and timestamp(value) is not None


@lru_cache(maxsize=None)
def validator(kind, version=VERSION):
    suffix = f"v{version}/{kind}.json" if version == "0.1" else f"{kind}.json"
    schema = json.loads(files("harness_observatory").joinpath("schemas", suffix).read_text())
    return Draft202012Validator(schema, format_checker=FORMAT_CHECKER)


def validate(kind, value):
    validator(kind, value.get("schema_version", VERSION)).validate(value)


def validate_bundle(bundle):
    version = bundle["run"]["schema_version"]
    validate("run", bundle["run"])
    for seq, e in enumerate(bundle["events"]):
        validate("event", e)
        if e["run_id"] != bundle["run"]["run_id"] or e["seq"] != seq:
            raise ValueError("Event lineage or sequence mismatch")
        if e["schema_version"] != version:
            raise ValueError("Mixed schema versions in bundle")
    for s in bundle["governance_snapshots"]:
        validate("governance_snapshot", s)
