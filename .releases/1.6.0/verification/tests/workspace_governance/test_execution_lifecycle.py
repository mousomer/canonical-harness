"""Synthetic normalized lifecycle cases; never native runtime evidence."""

from __future__ import annotations

import pytest

from tools.workspace_governance.execution_lifecycle import ExecutionLifecycle
from tools.workspace_governance.execution_policy import review_execution


def trace(*events):
    return [{"event": "start", "id": "original", "mode": "local"}] + [
        {"id": "original", **event} for event in events
    ]


def terminated(code=0, cause="exit", **updates):
    return dict(
        event="terminated",
        exit_code=code,
        cause=cause,
        termination_confirmed=True,
        **updates,
    )


@pytest.mark.parametrize(
    "kind",
    [
        "timeout",
        "cancel",
        "cancel_requested",
        "deadline",
        "deadline_elapsed",
        "unsupported",
    ],
)
def test_requests_never_establish_completion(kind):
    result = review_execution(trace(dict(event=kind)))
    assert result["status"] == "unknown"
    assert result["pending"] == ["original"]
    assert result["outcomes"] == {}
    assert result["executions"]["original"]["exit_code"] is None


@pytest.mark.parametrize(
    "cause,state",
    [("exit", "succeeded"), ("cancellation", "cancelled"), ("deadline", "deadline")],
)
def test_confirmed_outcomes_preserve_cause_separately(cause, state):
    result = review_execution(trace(terminated(cause=cause)))
    execution = result["executions"]["original"]
    assert execution["lifecycle"] == state
    assert execution["exit_code"] == 0
    assert execution["cause"] == cause
    assert result["status"] == ("success" if cause == "exit" else "failure")


def test_completion_during_disconnect_survives_reconcile_and_replay():
    events = trace(dict(event="disconnect"), terminated())
    result = review_execution(events)
    assert result["status"] == "success"
    assert result["executions"]["original"]["connection"] == "disconnected"
    events += [
        dict(event=e, id="original")
        for e in ("reconnect", "reconcile", "retrieve", "retrieve")
    ]
    events += [dict(id="original", **terminated())]
    replay = review_execution(events)
    assert replay["status"] == "success"
    assert replay["outcomes"] == result["outcomes"]
    assert replay["executions"]["original"]["reconciliation_required"] is False


@pytest.mark.parametrize("kind", ["wait", "watch", "retrieve"])
def test_reconnected_clients_must_reconcile_before_resuming(kind):
    result = review_execution(
        trace(dict(event="disconnect"), dict(event="reconnect"), dict(event=kind))
    )
    assert result["status"] == "violation"
    assert "reconciliation_required" in result["violations"]


def test_unconfirmed_terminal_does_not_consume_original_identity():
    event = terminated()
    event["termination_confirmed"] = False
    result = review_execution(trace(dict(event="cancel"), event))
    assert result["status"] == "violation"
    assert result["pending"] == ["original"]
    assert result["outcomes"] == {}


@pytest.mark.parametrize(
    "defect",
    [
        "missing_confirmation",
        "missing_cause",
        "bad_cause",
        "bool_exit",
        "unhashable_cause",
    ],
)
def test_incomplete_or_malformed_completion_never_certifies_success(defect):
    event = terminated()
    if defect == "missing_confirmation":
        del event["termination_confirmed"]
    elif defect == "missing_cause":
        del event["cause"]
    elif defect == "bad_cause":
        event["cause"] = "assertion_failure"
    elif defect == "unhashable_cause":
        event["cause"] = []
    else:
        event["exit_code"] = True
    result = review_execution(trace(event))
    assert result["status"] == "violation"
    assert result["pending"] == ["original"]


def test_conflicting_replay_and_wrong_identity_are_rejected():
    assert (
        "conflicting_terminal_result"
        in review_execution(trace(terminated(), terminated(code=7)))["violations"]
    )
    events = trace(terminated()) + [dict(id="replacement", event="retrieve")]
    assert "unknown_execution" in review_execution(events)["violations"]


def test_request_does_not_override_confirmed_natural_exit():
    result = review_execution(trace(dict(event="cancel_requested"), terminated()))
    assert result["status"] == "success"
    assert result["executions"]["original"]["cause"] == "exit"


def test_direct_api_and_trace_reviewer_use_identical_transitions():
    operation = ExecutionLifecycle(mode="local")
    events = [dict(event="deadline_elapsed"), terminated(code=-15, cause="deadline")]
    for event in events:
        assert operation.apply(event) is None
    result = review_execution(trace(*events))
    assert result["executions"]["original"] == operation.as_dict()
    assert result["outcomes"]["original"] == operation.outcome


def test_malformed_event_name_does_not_raise_or_complete():
    result = review_execution(trace(dict(event=[])))
    assert result["status"] == "violation"
    assert result["pending"] == ["original"]
