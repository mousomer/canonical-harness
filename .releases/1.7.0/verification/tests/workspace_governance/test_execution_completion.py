"""Completion contract must survive export, consumer resolution and tool yields."""

from __future__ import annotations

import json

import pytest
from support import PACK, build_checkout
from tools.workspace_governance.execution_policy import load_policy, review_execution
from tools.workspace_governance.resolver.core import GovernanceResolver
from tools.workspace_governance.validators.core import validate_pack

SHA = "a" * 40


def test_resolves_shared_policy_with_provenance(tmp_path):
    root = build_checkout(tmp_path / "consumer with spaces")
    result = GovernanceResolver.for_root(root).resolve()
    entry = result["entries"]["execution.completion"]
    assert entry["owner"] == "shared_pack"
    assert entry["source"].endswith("policies/execution.json")
    assert entry["value"]["requirements"]["autonomous_polling"] == "forbidden"
    assert entry["value"]["requirements"]["tool_watch_modes"] == "forbidden"
    assert entry["value"]["id"] == "synchronous-completion-v2"
    assert len(entry["value"]["sha256"]) == 64


@pytest.mark.parametrize(
    "defect",
    ["missing_binding", "wrong_version", "missing_file", "weakened", "symlink"],
)
def test_policy_defects_fail_even_before_lock_repair(tmp_path, defect):
    root = build_checkout(tmp_path / "consumer")
    pack = root / "tools/workspace_governance"
    path = pack / "MANIFEST.json"
    manifest = json.loads(path.read_text())
    policy = pack / "policies/execution.json"
    if defect == "missing_binding":
        del manifest["execution_policy"]
    elif defect == "wrong_version":
        manifest["execution_policy"]["id"] = "unknown-v2"
    elif defect == "missing_file":
        policy.unlink()
    elif defect == "weakened":
        data = json.loads(policy.read_text())
        data["requirements"]["autonomous_polling"] = "allowed"
        policy.write_text(json.dumps(data))
    else:
        outside = tmp_path / "foreign.json"
        outside.write_bytes(policy.read_bytes())
        policy.unlink()
        policy.symlink_to(outside)
    path.write_text(json.dumps(manifest))
    with pytest.raises((ValueError, OSError)):
        load_policy(pack, manifest)
    workspace = json.loads((root / ".governance/workspace.json").read_text())
    assert validate_pack(root, workspace)


def test_both_skills_route_the_exported_authority():
    manifest = json.loads((PACK / "MANIFEST.json").read_text())
    exported = {entry["path"] for entry in manifest["artifact_roles"]}
    assert {
        "execution_policy.py",
        "policies/execution.md",
        "policies/execution.json",
    } <= exported
    for name in ("governance-context", "governance-maintenance"):
        text = (PACK / "skills" / name / "SKILL.md").read_text()
        assert "execution.completion" in text
        assert "../../policies/execution.md" in text


def remote():
    return [
        {"event": "start", "id": "run-1", "mode": "remote", "sha": SHA},
        {"event": "notification", "id": "run-1"},
    ]


def test_yields_and_reconnect_keep_one_remote_execution():
    events = remote()
    events += [
        {"event": kind, "id": "run-1", "sha": SHA}
        for kind in (
            "yield",
            "wait",
            "yield",
            "wait",
            "disconnect",
            "reconnect",
            "notification",
        )
    ]
    events += [{"event": "finish", "id": "run-1", "sha": SHA, "exit_code": 0}]
    assert review_execution(events)["status"] == "success"


@pytest.mark.parametrize("event", ["log_tail", "watch", "redispatch", "unexpected"])
def test_redundant_polling_and_duplicate_watchers_rejected(event):
    assert (
        review_execution(remote() + [{"event": event, "id": "run-1"}])["status"]
        == "violation"
    )


def test_wrong_sha_cannot_complete_a_run():
    events = remote() + [
        {"event": "finish", "id": "run-1", "sha": "b" * 40, "exit_code": 0}
    ]
    result = review_execution(events)
    assert result["status"] == "violation"
    assert result["pending"] == ["run-1"]


@pytest.mark.parametrize("terminal", ["timeout", "cancel", "unsupported"])
def test_non_success_terminal_states(terminal):
    assert (
        review_execution(remote() + [{"event": terminal, "id": "run-1"}])["status"]
        == "failure"
    )


def test_parallel_failure_and_unknown_are_preserved():
    starts = [{"event": "start", "id": name, "mode": "local"} for name in ("a", "b")]
    assert review_execution([])["status"] == "unknown"
    assert review_execution(starts)["status"] == "unknown"
    first = starts + [{"event": "finish", "id": "a", "exit_code": 0}]
    assert review_execution(first)["status"] == "unknown"
    assert (
        review_execution(first + [{"event": "finish", "id": "b", "exit_code": 1}])[
            "status"
        ]
        == "failure"
    )
    assert (
        review_execution(first + [{"event": "finish", "id": "b", "exit_code": 0}])[
            "status"
        ]
        == "success"
    )


def test_duplicate_start_and_missing_exit_status_are_rejected():
    assert review_execution(remote() + remote())["status"] == "violation"
    assert (
        review_execution(remote() + [{"event": "finish", "id": "run-1"}])["status"]
        == "violation"
    )


def test_blocking_watch_is_no_longer_a_completion_mechanism():
    start = remote()[:1]
    result = review_execution(start + [{"event": "watch", "id": "run-1"}])
    assert result["status"] == "violation"
    assert result["violations"] == ["agent_polling_or_redispatch"]


def test_remote_completion_requires_observation_or_notification():
    start = remote()[:1]
    finish = [{"event": "finish", "id": "run-1", "sha": SHA, "exit_code": 0}]
    assert review_execution(start + finish)["status"] == "violation"
    assert (
        review_execution(start + [{"event": "notification", "id": "run-1"}] + finish)[
            "status"
        ]
        == "success"
    )


def test_real_runner_drains_both_streams_and_preserves_failure(tmp_path):
    import sys

    from tools.workspace_governance.fleet_execution import run_digest

    result = run_digest(
        [
            sys.executable,
            "-c",
            "import sys; print('o'*200000); print('e'*200000,file=sys.stderr); sys.exit(7)",
        ],
        tmp_path,
        {},
        10,
    )
    assert result["exit_code"] == 7
    assert result["timed_out"] is False
    assert result["stdout_bytes"] == result["stderr_bytes"] == 200001


@pytest.mark.parametrize("trace", [None, {}, [None], [{}], [{}] * 10001])
def test_malformed_or_unbounded_traces_cannot_certify_success(trace):
    assert review_execution(trace)["status"] == "violation"
