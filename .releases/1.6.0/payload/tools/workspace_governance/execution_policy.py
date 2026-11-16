"""Shared completion contract and explicit, bounded execution-trace review.

This does not intercept agent tools or enable transcript collection. Callers
provide normalized observations; absent completion remains unknown.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .execution_lifecycle import MODEL_ID, ExecutionLifecycle

POLICY_ID = "synchronous-completion-v1"
POLICY_PATH = "policies/execution.json"
REQUIRED = {
    "local_completion": "await_exit",
    "remote_completion": "blocking_watch_or_notification",
    "yield_handling": "resume_same_invocation",
    "agent_status_polling": "forbidden",
    "parallel_completion": "await_all_preserve_failures",
    "unknown_outcome": "never_success",
    "recovery": "reconnect_same_identity_no_automatic_redispatch",
}


def load_policy(pack: Path, manifest: dict) -> dict:
    binding = manifest.get("execution_policy")
    if binding != {"id": POLICY_ID, "path": POLICY_PATH}:
        raise ValueError("missing or incompatible shared execution policy binding")
    path = pack / POLICY_PATH
    if path.is_symlink() or not path.resolve().is_relative_to(pack.resolve()):
        raise ValueError("execution policy must be owned by the selected pack")
    raw = path.read_bytes()
    policy = json.loads(raw)
    if policy != {"schema_version": 1, "id": POLICY_ID, "requirements": REQUIRED}:
        raise ValueError("shared execution policy is incomplete or weakened")
    return {**policy, "sha256": hashlib.sha256(raw).hexdigest()}


def _start(event: dict, operations: dict) -> str | None:
    identity = event["id"]
    if identity in operations:
        return "duplicate_execution"
    if not isinstance(event.get("mode"), str) or event["mode"] not in {
        "local",
        "remote",
    }:
        return "unsupported_completion_mechanism"
    sha = event.get("sha")
    if event["mode"] == "remote" and (
        not isinstance(sha, str)
        or len(sha) != 40
        or any(c not in "0123456789abcdef" for c in sha)
    ):
        return "missing_remote_sha"
    operations[identity] = ExecutionLifecycle(event["mode"], sha)
    return None


def _apply(event: dict, operations: dict) -> str | None:
    if not isinstance(event, dict):
        return "invalid_event"
    identity = event.get("id")
    if not isinstance(identity, str) or not identity:
        return "missing_identity"
    if not isinstance(event.get("event"), str):
        return "invalid_event"
    if event["event"] == "start":
        return _start(event, operations)
    if identity not in operations:
        return "unknown_execution"
    return operations[identity].apply(event)


def review_execution(events: list[dict]) -> dict:
    """Review bounded normalized observations using the shared lifecycle model.

    Legacy finish denotes confirmed termination. Timeout/cancel/deadline requests
    never infer termination; explicit terminated requires true confirmation.
    """
    operations: dict[str, ExecutionLifecycle] = {}
    violations: list[str] = []
    if not isinstance(events, list) or len(events) > 10000:
        return {
            "model": MODEL_ID,
            "status": "violation",
            "violations": ["invalid_trace"],
            "pending": [],
            "outcomes": {},
            "executions": {},
        }
    for event in events:
        violation = _apply(event, operations)
        if violation:
            violations.append(violation)
    outcomes = {key: op.outcome for key, op in operations.items() if op.terminal}
    pending = sorted(key for key, op in operations.items() if not op.terminal)
    status = (
        "success"
        if outcomes and all(v == "success" for v in outcomes.values())
        else "failure"
    )
    if pending or not outcomes:
        status = "unknown"
    if violations:
        status = "violation"
    return {
        "model": MODEL_ID,
        "status": status,
        "violations": violations,
        "pending": pending,
        "outcomes": outcomes,
        "executions": {key: op.as_dict() for key, op in operations.items()},
    }
