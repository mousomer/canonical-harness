"""Release-blocking behaviour for supervised-completion-v3.

Primary acceptance: covered operations cannot generate uncontrolled agent
polling. These tests drive action sequences through the pack's real hook
launcher (`hooks/no_polling.sh`) and the operation supervisor, with a simulated
clock and service, and assert the downstream calls and their timing rather than
the presence of a message.
"""

from __future__ import annotations

import fcntl
import itertools
import json
import os
import subprocess
import sys

import pytest
from support import PACK, build_checkout
from test_no_polling import claude_payload, codex_payload, run_hook
from tools.workspace_governance import operation_supervisor as ops
from tools.workspace_governance.execution_policy import load_policy
from tools.workspace_governance.operation_supervisor import (
    ADAPTER_FAULT,
    BUDGET_EXHAUSTED,
    DEADLINE_EXPIRED,
    MECHANISM_ERROR,
    PENDING,
    POLLING,
    SUCCEEDED,
    Observation,
    Refused,
    Supervisor,
)

FAKE_PID = 987654
TEN_MINUTES = 600.0


class SimClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.sleeps.append(seconds)
            self.t += seconds


class SimJob:
    """A long job observed only through the supervisor's status requests."""

    def __init__(self, clock: SimClock, done_at: float, outcome: str = SUCCEEDED):
        self.clock, self.done_at, self.outcome = clock, done_at, outcome
        self.calls: list[float] = []
        self.starts = 0
        self.cancels = 0

    def start(self, record: dict) -> dict:
        self.starts += 1
        return {"pid": FAKE_PID}

    def status(self, record: dict) -> Observation:
        self.calls.append(self.clock.now())
        if self.clock.now() >= self.done_at:
            return Observation(
                self.outcome, "exit 0" if self.outcome == SUCCEEDED else ""
            )
        return Observation(PENDING, "running")


class SimContinuation:
    """A tool that must be continued on its handle; may or may not block."""

    def __init__(self, clock: SimClock, done_at: float, honours_block: bool = True):
        self.clock, self.done_at, self.honours_block = clock, done_at, honours_block
        self.calls: list[tuple[float, float]] = []

    def continue_(self, record: dict, block: float) -> Observation:
        self.calls.append((self.clock.now(), block))
        if self.honours_block:
            self.clock.t += min(block, max(self.done_at - self.clock.now(), 0.0))
        if self.clock.now() >= self.done_at:
            return Observation(SUCCEEDED, "handle completed")
        return Observation(PENDING, "still running")


@pytest.fixture
def consumer(tmp_path):
    return build_checkout(tmp_path / "consumer")


@pytest.fixture
def state(tmp_path):
    return tmp_path / "operation-state"


def make_supervisor(root, state, clock, services, profiles=None):
    return Supervisor(
        root, clock=clock, services=services, profiles=profiles, state_base=state
    )


def limits(name):
    """Budget plus polling parameters of a profile, flattened for assertions."""
    profile = ops.load_profiles()[name]
    completion = profile["completion"]
    flat = dict(profile["budget"])
    flat.update(completion.get(POLLING) or {})
    for mechanism in completion["preference"]:
        flat.update(completion[mechanism])
    return flat


def hook(consumer, state, payload, runtime="claude", *args, env=None):
    return run_hook(
        consumer,
        payload,
        runtime,
        *args,
        env={ops.STATE_ENV: str(state), **(env or {})},
    )


def submit_long_job(consumer, state, clock, job, outputs=("build/report.xml",)):
    supervisor = make_supervisor(consumer, state, clock, {"local-process": job})
    record = supervisor.submit(
        "local-process",
        {"argv": ["make", "test"], "cwd": str(consumer)},
        list(outputs),
    )
    return supervisor, record


def gaps(times):
    return [b - a for a, b in itertools.pairwise(times)]


# ------------------------------------------------- 1. synchronous completion


def test_synchronous_job_completes_without_status_queries(consumer, state):
    env = {**os.environ, ops.STATE_ENV: str(state)}
    result = subprocess.run(
        [
            str(consumer / "gov"),
            *["op", "run", "--profile", "local-command", "--"],
            sys.executable,
            "-c",
            "print('built')",
        ],
        cwd=consumer,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout[result.stdout.index("{") :])
    assert report["status"] == SUCCEEDED and report["verification"] is True
    # The invocation awaited exit itself: one blocking request, no status polling.
    assert report["mechanism"] == "synchronous_return" and report["requests_used"] == 1
    assert all(t["to"] != POLLING for t in report["transitions"])
    assert [r["status"] for r in ops.Registry(consumer, state).records()] == [SUCCEEDED]


def test_real_long_local_job_needs_one_call_and_one_request(consumer, state):
    env = {**os.environ, ops.STATE_ENV: str(state)}
    result = subprocess.run(
        [
            str(consumer / "gov"),
            *["op", "run", "--profile", "local-process", "--"],
            "/bin/sh",
            "-c",
            "sleep 1; echo finished",
        ],
        cwd=consumer,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == SUCCEEDED and report["verification"] is True
    # The launching supervisor blocks on the child: one observation, no polling.
    assert report["requests_used"] == 1 and report["mechanism"] == "blocking_wait"
    assert "finished" in report["log_tail"]


@pytest.mark.parametrize(
    ("text", "status", "code"),
    [
        ("0\n", SUCCEEDED, 0),
        ("3", ops.FAILED, 3),
        ("-15", ops.FAILED, -15),
        (" 7 ", ops.FAILED, 7),
        ("killed", ops.UNKNOWN, None),
        ("", ops.UNKNOWN, None),
    ],
)
def test_local_exit_file_parses_with_int_not_digit_checks(tmp_path, text, status, code):
    exit_file = tmp_path / "exit"
    exit_file.write_text(text, encoding="utf-8")
    record = {"artifacts": {"exit_file": str(exit_file)}, "identity": {}}
    observed = ops.LocalProcessService().status(record)
    assert observed.state == status
    assert observed.exit_status == code


# ------------------------------------- 2. long jobs: one submission, one wait


@pytest.mark.parametrize("duration", [TEN_MINUTES, 1500.0])
def test_long_job_needs_constant_agent_calls_and_bounded_requests(
    consumer, state, duration
):
    clock = SimClock()
    job = SimJob(clock, done_at=duration)
    supervisor, record = submit_long_job(consumer, state, clock, job)
    agent_calls = 2  # submit + wait, independent of job length
    result = supervisor.wait(record["id"])
    assert result.status == SUCCEEDED
    assert job.starts == 1
    wait = limits("local-process")
    assert len(job.calls) == result.requests_used <= wait["max_requests"]
    ramp, step = 0, wait["min_interval_seconds"]
    while step < wait["max_interval_seconds"]:
        ramp, step = ramp + 1, step * wait["backoff_factor"]
    # Backoff to the maximum interval, never a fixed fast cadence.
    assert len(job.calls) <= ramp + duration / wait["max_interval_seconds"] + 2
    spacing = gaps(job.calls)
    assert all(g >= wait["min_interval_seconds"] for g in spacing)
    assert all(g <= wait["max_interval_seconds"] for g in spacing)
    assert spacing == sorted(spacing)  # backoff never speeds up
    assert job.calls[-1] - duration < wait["max_interval_seconds"]
    assert agent_calls == 2


def test_agent_probes_are_refused_while_the_supervised_wait_delivers(consumer, state):
    clock = SimClock()
    job = SimJob(clock, done_at=TEN_MINUTES)
    supervisor, record = submit_long_job(consumer, state, clock, job)
    log = record["artifacts"]["log"]
    denied = hook(consumer, state, claude_payload(f"tail -n 20 {log}"))
    assert denied.returncode == 2
    assert f"operation={record['id']}" in denied.stderr
    assert f"op wait {record['id']}" in denied.stderr  # the permitted next action
    assert job.calls == []  # the probe never reached the service
    assert supervisor.wait(record["id"]).status == SUCCEEDED
    after = hook(consumer, state, claude_payload(f"tail -n 20 {log}"))
    assert after.returncode == 0  # completed operations are no longer restricted


# ----------------------------------------------- 3. controlled continuation


def test_continuations_use_enforced_blocking_interval(consumer, state):
    clock = SimClock()
    tool = SimContinuation(clock, done_at=95.0)
    supervisor = make_supervisor(consumer, state, clock, {"continuation-handle": tool})
    record = supervisor.submit("continuation-handle", {"handle": "h-77"})
    result = supervisor.wait(record["id"])
    block = limits("continuation-handle")["block_seconds"]
    assert result.status == SUCCEEDED
    assert [b for _, b in tool.calls] == [block] * len(tool.calls)
    assert all(g >= block for g in gaps([t for t, _ in tool.calls]))
    assert len(tool.calls) == 4  # 0, 30, 60, 90 -> completes at 95


def test_immediate_completion_on_continuation_is_valid(consumer, state):
    clock = SimClock()
    tool = SimContinuation(clock, done_at=0.0, honours_block=False)
    supervisor = make_supervisor(consumer, state, clock, {"continuation-handle": tool})
    record = supervisor.submit("continuation-handle", {"handle": "h-1"})
    assert supervisor.wait(record["id"]).status == SUCCEEDED
    assert len(tool.calls) == 1


# ---------------------- 4. probes cannot circumvent on covered interfaces


def tool_payload(tool: str, tool_input: dict) -> dict:
    return {**claude_payload(""), "tool_name": tool, "tool_input": tool_input}


def probe_payloads(record, consumer):
    log = record["artifacts"]["log"]
    exit_file = record["artifacts"]["exit_file"]
    directory = record["artifacts"]["directory"]
    return {
        "status_log_tail": claude_payload(f"tail -n 50 {log}"),
        "log_cat": claude_payload(f"cat {log} | grep -c PASS"),
        "exit_file_exists": claude_payload(f"test -f {exit_file} && echo done"),
        "output_exists": claude_payload("ls -la build/report.xml"),
        "artifact_listing": claude_payload(f"ls {directory}"),
        "process_ps": claude_payload(f"ps -p {FAKE_PID} -o stat="),
        "process_kill0": claude_payload(f"kill -0 {FAKE_PID}"),
        "synthetic_sleep": claude_payload("sleep 120 && echo checked"),
        "duplicate_submission": claude_payload("make   test"),
        "read_tool": tool_payload("Read", {"file_path": log}),
        "grep_tool": tool_payload("Grep", {"pattern": "FAIL", "path": log}),
        "monitor_tool": claude_payload(f"tail -f {log}", tool="Monitor"),
        "codex_exec": codex_payload(["bash", "-lc", f"tail -n 5 {log}"]),
    }


@pytest.mark.parametrize(
    "probe",
    [
        "status_log_tail", "log_cat", "exit_file_exists", "output_exists",
        "artifact_listing", "process_ps", "process_kill0", "synthetic_sleep",
        "duplicate_submission", "read_tool", "grep_tool", "monitor_tool", "codex_exec",
    ],
)  # fmt: skip
def test_probes_of_a_pending_operation_are_refused(consumer, state, probe):
    clock = SimClock()
    job = SimJob(clock, done_at=TEN_MINUTES)
    _, record = submit_long_job(consumer, state, clock, job)
    payload = probe_payloads(record, consumer)[probe]
    payload = {**payload, "cwd": str(consumer)}
    runtime = "codex" if probe == "codex_exec" else "claude"
    result = hook(consumer, state, payload, runtime)
    assert result.returncode == 2, (probe, result.stderr)
    assert "OPERATION_PENDING" in result.stderr
    assert record["id"] in result.stderr and "Permitted next action" in result.stderr
    assert "Only this call is blocked" in result.stderr
    assert job.calls == [] and job.starts == 1


def test_remote_run_status_queries_and_reruns_are_refused(consumer, state):
    clock = SimClock()
    remote = SimJob(clock, done_at=TEN_MINUTES)
    supervisor = make_supervisor(consumer, state, clock, {"github-actions-run": remote})
    record = supervisor.submit(
        "github-actions-run",
        {"run_id": "424242", "repository": "o/r", "head_sha": "a" * 40},
    )
    for command in (
        "gh run view 424242 --json status",
        "gh api repos/o/r/actions/runs/424242",
        "gh run rerun 424242",
    ):
        result = hook(consumer, state, claude_payload(command))
        assert result.returncode == 2, command
        assert record["id"] in result.stderr
    mcp = {
        **claude_payload(""),
        "tool_name": "mcp__github__get_workflow_run",
        "tool_input": {"owner": "o", "repo": "r", "run_id": 424242},
    }
    assert hook(consumer, state, mcp).returncode == 2
    assert remote.calls == []


def test_concurrent_or_redundant_waits_are_refused(consumer, state):
    clock = SimClock()
    job = SimJob(clock, done_at=TEN_MINUTES)
    supervisor, record = submit_long_job(consumer, state, clock, job)
    lock = supervisor.registry.wait_lock_path(record["id"])
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)  # another waiter is attached
        with pytest.raises(Refused) as refusal:
            supervisor.wait(record["id"])
        assert "already attached" in refusal.value.reason
        for action in ("wait", "result"):
            denied = hook(
                consumer, state, claude_payload(f"./gov op {action} {record['id']}")
            )
            assert denied.returncode == 2 and "REDUNDANT_WAIT" in denied.stderr
        fcntl.flock(held, fcntl.LOCK_UN)
    assert job.calls == []
    assert supervisor.wait(record["id"]).status == SUCCEEDED


# -------------------------- 5. interface switches and session restoration


def test_restrictions_survive_interfaces_sessions_and_compaction(consumer, state):
    clock = SimClock()
    job = SimJob(clock, done_at=TEN_MINUTES)
    _, record = submit_long_job(consumer, state, clock, job)
    log = record["artifacts"]["log"]
    sequence = [
        ("claude", claude_payload(f"tail {log}", session="s1")),
        ("codex", codex_payload(f"tail {log}", turn="t9", session="c9")),
        ("claude", {**claude_payload(f"tail {log}", session="s2"), "prompt_id": "p2"}),
    ]
    for runtime, payload in sequence:
        assert hook(consumer, state, payload, runtime).returncode == 2
    # A new user prompt and loss of all per-session hook state do not reset it.
    prompt = {"session_id": "s3", "hook_event_name": "UserPromptSubmit"}
    assert hook(consumer, state, prompt, "claude", "prompt").returncode == 0
    for path in (consumer / "tmp").rglob("*"):
        if path.is_file():
            path.unlink()
    resumed = hook(consumer, state, claude_payload(f"tail {log}", session="s4"))
    assert resumed.returncode == 2 and record["id"] in resumed.stderr
    assert job.calls == []


# ------------------------------ 6. repeated immediate pending stops retries


def test_repeated_immediate_pending_is_an_adapter_fault_without_retry_loop(
    consumer, state
):
    clock = SimClock()
    tool = SimContinuation(clock, done_at=10_000.0, honours_block=False)
    supervisor = make_supervisor(consumer, state, clock, {"continuation-handle": tool})
    record = supervisor.submit("continuation-handle", {"handle": "h-broken"})
    result = supervisor.wait(record["id"])
    wait = limits("continuation-handle")
    assert result.status == ADAPTER_FAULT and result.exit_code == 13
    assert len(tool.calls) == wait["max_immediate_pending"]
    # Even the faulty tool was not hammered: the enforced interval was kept.
    assert all(g >= wait["block_seconds"] for g in gaps([t for t, _ in tool.calls]))
    with pytest.raises(Refused) as refusal:
        supervisor.wait(record["id"])  # no further automatic retries
    assert "new user instruction" in refusal.value.reason
    assert len(tool.calls) == wait["max_immediate_pending"]
    supervisor.registry.advance_user_epoch()
    supervisor.wait(record["id"])
    assert len(tool.calls) == 2 * wait["max_immediate_pending"]


# --------------------------------- 7. internal polling stays within budget


def test_internal_polling_respects_interval_backoff_and_deadline(consumer, state):
    clock = SimClock()
    job = SimJob(clock, done_at=float("inf"))
    supervisor, record = submit_long_job(consumer, state, clock, job)
    result = supervisor.wait(record["id"])
    wait = limits("local-process")
    assert result.status == DEADLINE_EXPIRED and result.exit_code == 11
    assert job.calls[-1] <= wait["deadline_seconds"]
    assert len(job.calls) <= wait["max_requests"]
    expected = [wait["min_interval_seconds"]]
    while len(expected) < len(job.calls) - 1:
        expected.append(
            min(expected[-1] * wait["backoff_factor"], wait["max_interval_seconds"])
        )
    assert gaps(job.calls) == expected
    assert clock.now() == pytest.approx(wait["deadline_seconds"])


def test_request_budget_stops_polling_exactly(consumer, state):
    profiles = ops.load_profiles()
    profiles["local-process"] = json.loads(json.dumps(profiles["local-process"]))
    profiles["local-process"]["budget"].update(max_requests=5, deadline_seconds=10**6)
    clock = SimClock()
    job = SimJob(clock, done_at=float("inf"))
    supervisor = make_supervisor(
        consumer, state, clock, {"local-process": job}, profiles
    )
    record = supervisor.submit("local-process", {"argv": ["x"], "cwd": "."})
    result = supervisor.wait(record["id"])
    assert (result.status, result.reason) == (
        BUDGET_EXHAUSTED,
        "request_budget_exhausted",
    )
    assert len(job.calls) == 5


POLLING_PATH = ("completion", POLLING)
CONTRADICTIONS = {
    "zero_interval": (POLLING_PATH, {"min_interval_seconds": 0}),
    "sub_second_interval": (POLLING_PATH, {"min_interval_seconds": 0.1}),
    "speeding_backoff": (POLLING_PATH, {"backoff_factor": 0.5}),
    "max_below_min": (
        POLLING_PATH,
        {"min_interval_seconds": 60, "max_interval_seconds": 5},
    ),
    "interval_above_deadline": (
        POLLING_PATH,
        {"min_interval_seconds": 5000, "max_interval_seconds": 5000},
    ),
    "no_requests": (("budget",), {"max_requests": 0}),
    "no_deadline": (("budget",), {"deadline_seconds": 0}),
    "fractional_waits": (("budget",), {"max_waits": 1.5}),
    "window_above_deadline": (
        ("completion", "blocking_wait"),
        {"response_window_seconds": 7200},
    ),
    "window_missing": (
        ("completion", "blocking_wait"),
        {"response_window_seconds": None},
    ),
    "polling_preferred": (("completion",), {"preference": ["adapter_polling"]}),
    "unknown_mechanism": (("completion",), {"preference": ["status_check"]}),
    "repeated_preference": (
        ("completion",),
        {"preference": ["blocking_wait", "blocking_wait"]},
    ),
    "fallback_on_failure": (("completion",), {"fallback_on_failure": POLLING}),
    "authorization_not_boolean": (POLLING_PATH, {"authorized": "yes"}),
    "no_mechanism_at_all": (
        ("completion",),
        {"preference": [], "blocking_wait": None, POLLING: {"authorized": False}},
    ),
    "deadline_restarts": ((), {"on_budget_exhausted": "restart"}),
    "window_cancels": ((), {"on_response_window": "cancel"}),
    "unknown_key": ((), {"agent_may_choose": True}),
}


@pytest.mark.parametrize("defect", sorted(CONTRADICTIONS))
def test_incomplete_or_contradictory_profiles_are_rejected(defect):
    profile = json.loads(json.dumps(ops.load_profiles()["local-process"]))
    path, change = CONTRADICTIONS[defect]
    target = profile
    for key in path:
        target = target[key]
    for key, value in change.items():
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value
    with pytest.raises(ValueError):
        ops.validate_profile(defect, profile)


def test_contradictory_profiles_fail_pack_validation_before_any_start(tmp_path):
    root = build_checkout(tmp_path / "consumer")
    pack = root / "tools/workspace_governance"
    path = pack / "policies/operation_profiles.json"
    data = json.loads(path.read_text())
    data["profiles"]["local-process"]["completion"][POLLING]["max_interval_seconds"] = 1
    path.write_text(json.dumps(data))
    manifest = json.loads((pack / "MANIFEST.json").read_text())
    with pytest.raises(ValueError, match="max_interval_seconds"):
        load_policy(pack, manifest)
    with pytest.raises(ValueError):
        ops.load_profiles(path)


def test_github_adapter_makes_one_request_per_observation():
    calls = []

    def runner(command, **_):
        calls.append(command)
        payload = {"status": "completed", "conclusion": "success", "headSha": "a" * 40}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    service = ops.GitHubRunService(runner)
    record = {"identity": {"run_id": "7", "repository": "o/r", "head_sha": "a" * 40}}
    assert service.status(record).state == SUCCEEDED
    assert len(calls) == 1 and "watch" not in calls[0]
    record["identity"]["head_sha"] = "b" * 40
    with pytest.raises(ops.MechanismError):
        service.status(record)  # another commit's run is never this operation


# --------------------- 8. deadline expiry preserves without resubmission


def test_deadline_expiry_preserves_the_original_operation(consumer, state):
    clock = SimClock()
    job = SimJob(clock, done_at=2500.0)
    supervisor, record = submit_long_job(consumer, state, clock, job)
    first = supervisor.wait(record["id"])
    assert first.status == DEADLINE_EXPIRED and first.operation == record["id"]
    assert (
        "Do not resubmit" in first.next_action
        and "user instruction" in first.next_action
    )
    stored = supervisor.registry.load(record["id"])
    assert stored["identity"] == record["identity"] and job.starts == 1
    with pytest.raises(Refused) as duplicate:
        supervisor.submit(
            "local-process", {"argv": ["make", "test"], "cwd": str(consumer)}
        )
    assert record["id"] in duplicate.value.reason
    with pytest.raises(Refused):
        supervisor.wait(record["id"])  # no automatic re-wait loop
    calls_before = len(job.calls)
    supervisor.registry.advance_user_epoch()  # a new user instruction
    resumed = supervisor.wait(record["id"])
    assert resumed.status == SUCCEEDED and resumed.operation == record["id"]
    assert job.starts == 1 and job.cancels == 0
    assert len(job.calls) > calls_before


def test_mechanism_failure_is_an_error_not_success(consumer, state):
    class Broken:
        def status(self, record):
            raise ops.MechanismError("service unreachable")

    clock = SimClock()
    supervisor = make_supervisor(consumer, state, clock, {"local-process": Broken()})
    record = supervisor.submit("local-process", {"argv": ["x"], "cwd": "."})
    result = supervisor.wait(record["id"])
    assert (
        result.status == MECHANISM_ERROR and result.as_dict()["verification"] is False
    )


def test_unsupported_operation_reports_the_limitation(consumer, state):
    env = {**os.environ, ops.STATE_ENV: str(state)}
    result = subprocess.run(
        [str(consumer / "gov"), "op", "run", "--profile", "uncharted", "--", "true"],
        cwd=consumer,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == ops.REFUSED_EXIT
    assert "no supported completion mechanism" in result.stdout


# ------------- 9. unrelated work stays available after denials and failures


UNRELATED = ["git status", "git diff", "cat src/app.py", "python -m pytest -q"]


def test_unrelated_work_remains_available_after_a_denial(consumer, state):
    clock = SimClock()
    _, record = submit_long_job(consumer, state, clock, SimJob(clock, TEN_MINUTES))
    log = record["artifacts"]["log"]
    assert hook(consumer, state, claude_payload(f"tail {log}")).returncode == 2
    for command in UNRELATED:
        allowed = hook(
            consumer, state, {**claude_payload(command), "cwd": str(consumer)}
        )
        assert (allowed.returncode, allowed.stderr) == (0, ""), command
    read = tool_payload("Read", {"file_path": str(consumer / "AGENTS.md")})
    assert hook(consumer, state, read).returncode == 0


@pytest.mark.parametrize("defect", ["missing_interpreter", "broken_supervisor"])
def test_unrelated_work_remains_available_after_a_hook_failure(consumer, state, defect):
    clock = SimClock()
    submit_long_job(consumer, state, clock, SimJob(clock, TEN_MINUTES))
    env = {}
    if defect == "missing_interpreter":
        env["GOVERNANCE_PYTHON"] = str(consumer / "missing-python")
    else:
        (consumer / "tools/workspace_governance/operation_supervisor.py").write_text(
            "raise\n"
        )
    for command in UNRELATED:
        allowed = hook(consumer, state, claude_payload(command), env=env)
        assert allowed.returncode == 0, (command, allowed.stderr)
        assert "GUARD_DEGRADED" in allowed.stderr
    blocked = hook(consumer, state, claude_payload("sleep 60"), env=env)
    assert blocked.returncode == 2 and "GUARD_FAILURE" in blocked.stderr


def test_profiles_declare_coverage_and_pack_exports_them():
    data = json.loads((PACK / "policies/operation_profiles.json").read_text())
    assert data["policy"] == ops.POLICY_ID
    assert data["coverage"]["enforced_interfaces"]["claude"]
    assert data["coverage"]["not_covered"]
    for name, profile in data["profiles"].items():
        ops.validate_profile(name, profile)
    manifest = json.loads((PACK / "MANIFEST.json").read_text())
    exported = {entry["path"] for entry in manifest["artifact_roles"]}
    assert {"operation_supervisor.py", "policies/operation_profiles.json"} <= exported


def test_claude_adapter_binds_file_tools(consumer):
    matcher = json.loads((consumer / ".claude/settings.json").read_text())["hooks"][
        "PreToolUse"
    ][0]["matcher"]
    assert {"Bash", "Monitor", "Read", "Grep", "Glob"} <= set(
        matcher.replace("|mcp__.*", "").split("|")
    )


def test_resumed_waiter_uses_authorized_polling_and_records_it(consumer, state):
    env = {**os.environ, ops.STATE_ENV: str(state)}
    gov = str(consumer / "gov")
    submitted = subprocess.run(
        [
            gov,
            *["op", "submit", "--profile", "local-process", "--"],
            "/bin/sh",
            "-c",
            "sleep 3; echo late",
        ],
        cwd=consumer,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert submitted.returncode == 0, submitted.stderr
    op_id = json.loads(submitted.stdout)["operation"]
    waited = subprocess.run(
        [gov, "op", "wait", op_id],
        cwd=consumer,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    report = json.loads(waited.stdout)
    assert waited.returncode == 0 and report["status"] == SUCCEEDED
    # A separate waiter cannot block on the child. The profile authorizes adapter
    # polling as the fallback; the transition is recorded, never silent.
    assert report["mechanism"] == POLLING and report["requests_used"] <= 3
    fallback = [t for t in report["transitions"] if t["to"] == POLLING]
    assert fallback[0]["reason"] == "preferred_unavailable_authorized_polling"
    assert fallback[0]["unavailable"] == ["blocking_wait"]
    assert "late" in report["log_tail"]


# ------------------ 10. deterministic selection and authorized fallback only


class Interrupted(BaseException):
    """The waiting process is stopped (its tool response window ran out)."""


class SimBlocking(SimJob):
    """A service that can block on the operation for a bounded window."""

    def __init__(self, clock, done_at, *, fail_at=None, stop_after=None, **kw):
        super().__init__(clock, done_at, **kw)
        self.blocks: list[tuple[float, float]] = []
        self.fail_at, self.stop_after = fail_at, stop_after

    def block(self, record, timeout):
        if self.stop_after is not None and len(self.blocks) >= self.stop_after:
            raise Interrupted
        self.blocks.append((self.clock.now(), timeout))
        if self.fail_at is not None and len(self.blocks) > self.fail_at:
            raise ops.MechanismError("blocking call lost its connection")
        self.clock.t += min(timeout, max(self.done_at - self.clock.now(), 0.0))
        if self.clock.now() >= self.done_at:
            return Observation(self.outcome, "exit 0")
        return Observation(PENDING, "running")


class SimNotifier(SimBlocking):
    def __init__(self, clock, done_at):
        super().__init__(clock, done_at)
        self.notifications: list[float] = []

    def wait_notification(self, record, timeout):
        self.notifications.append(self.clock.now())
        return self.block(record, timeout)


def reasons(result):
    return [t["reason"] for t in result.transitions]


def test_selection_follows_the_profile_preference_order(consumer, state):
    clock = SimClock()
    notifier = SimNotifier(clock, done_at=100.0)
    supervisor = make_supervisor(
        consumer, state, clock, {"notified-operation": notifier}
    )
    record = supervisor.submit("notified-operation", {"subscription": "s-1"})
    result = supervisor.wait(record["id"])
    assert (result.status, result.mechanism) == (SUCCEEDED, "notification")
    assert len(notifier.notifications) == 1 and notifier.calls == []

    blocker = SimBlocking(clock, done_at=clock.now() + 100.0)  # no notification path
    supervisor = make_supervisor(
        consumer, state, clock, {"notified-operation": blocker}
    )
    record = supervisor.submit("notified-operation", {"subscription": "s-2"})
    result = supervisor.wait(record["id"])
    assert (result.status, result.mechanism) == (SUCCEEDED, "blocking_wait")
    selected = result.transitions[0]
    assert selected["reason"] == "selected" and selected["unavailable"] == [
        "notification"
    ]
    assert blocker.calls == []  # never fell through to status requests


def test_polling_without_authorization_is_rejected_not_attempted(consumer, state):
    clock = SimClock()
    poller = SimJob(clock, done_at=10.0)  # only answers status requests
    supervisor = make_supervisor(consumer, state, clock, {"notified-operation": poller})
    record = supervisor.submit("notified-operation", {"subscription": "s-3"})
    result = supervisor.wait(record["id"])
    assert (result.status, result.reason) == (MECHANISM_ERROR, "no_supported_mechanism")
    assert result.as_dict()["verification"] is False
    assert poller.calls == []  # the service was never polled
    assert result.transitions[-2]["unavailable"] == ["notification", "blocking_wait"]


def test_mechanism_failure_never_switches_to_status_polling(consumer, state):
    clock = SimClock()
    job = SimBlocking(clock, done_at=TEN_MINUTES, fail_at=1)
    supervisor, record = submit_long_job(consumer, state, clock, job)
    result = supervisor.wait(record["id"])
    assert (result.status, result.reason) == (MECHANISM_ERROR, "mechanism_failure")
    assert result.mechanism == "blocking_wait" and result.exit_code == 13
    assert job.calls == []  # polling is authorized only when blocking is unavailable
    assert len(job.blocks) == 2 and "lost its connection" in result.detail


def test_response_window_expiry_continues_the_same_handle(consumer, state):
    clock = SimClock()
    job = SimBlocking(clock, done_at=1000.0)
    supervisor, record = submit_long_job(consumer, state, clock, job)
    result = supervisor.wait(record["id"])
    window = limits("local-process")["response_window_seconds"]
    assert result.status == SUCCEEDED and result.operation == record["id"]
    assert [t for t, _ in job.blocks] == [0.0, window, 2 * window, 3 * window]
    assert reasons(result).count("response_window_expired") == 3
    assert job.starts == 1 and job.calls == []
    assert result.consumed["requests"] == 4 and result.consumed["waits"] == 1


def test_budget_survives_fallback_interfaces_and_session_restoration(consumer, state):
    clock = SimClock()
    deadline = limits("local-process")["deadline_seconds"]
    first = SimBlocking(clock, done_at=10**9, stop_after=2)
    supervisor, record = submit_long_job(consumer, state, clock, first)
    with pytest.raises(Interrupted):
        supervisor.wait(record["id"])  # the calling tool was stopped mid-wait
    stored = supervisor.registry.load(record["id"])
    assert stored["consumed"]["wait_seconds"] == pytest.approx(600.0)
    log = record["artifacts"]["log"]
    # Another interface and a new session still see the same pending operation.
    assert (
        hook(
            consumer, state, codex_payload(f"tail {log}", session="c2"), "codex"
        ).returncode
        == 2
    )
    assert (
        hook(consumer, state, claude_payload(f"tail {log}", session="s9")).returncode
        == 2
    )
    # A restored session has no child handle: authorized polling, same budget.
    second = SimJob(clock, done_at=10**9)
    restored = make_supervisor(consumer, state, clock, {"local-process": second})
    began = clock.now()
    result = restored.wait(record["id"])
    assert result.status == DEADLINE_EXPIRED and result.operation == record["id"]
    assert clock.now() - began == pytest.approx(deadline - 600.0)
    assert result.consumed["wait_seconds"] == pytest.approx(deadline)
    assert result.consumed["waits"] == 2
    # The block call cut off by the interruption still counts against the budget.
    assert result.requests_used == len(first.blocks) + 1 + len(second.calls)
    assert {"waiter_interrupted", "preferred_unavailable_authorized_polling"} <= set(
        reasons(result)
    )
    assert first.starts == 1 and second.starts == 0


def test_interrupted_waits_cannot_speed_up_the_cadence(consumer, state):
    class Flaky(SimJob):
        def status(self, record):
            super().status(record)
            raise Interrupted  # every wait is stopped right after one request

    clock = SimClock()
    job = Flaky(clock, done_at=10**9)
    supervisor, record = submit_long_job(consumer, state, clock, job)
    budget = limits("local-process")
    for _ in range(budget["max_waits"]):
        with pytest.raises(Interrupted):
            supervisor.wait(record["id"])  # `timeout N gov op wait` as a status check
    spacing = gaps(job.calls)
    assert spacing == [
        budget["min_interval_seconds"],
        budget["min_interval_seconds"] * budget["backoff_factor"],
    ]
    with pytest.raises(Refused) as refusal:
        supervisor.wait(record["id"])
    assert "wait_budget_exhausted" in refusal.value.reason
    assert len(job.calls) == budget["max_waits"]


# ------------------------------- 11. status reports and composed commands


def gov(consumer, state, *args):
    env = {**os.environ, ops.STATE_ENV: str(state)}
    return subprocess.run(
        [str(consumer / "gov"), "op", *args],
        cwd=consumer,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_supervisor_status_is_reported_once_per_user_instruction(consumer, state):
    clock = SimClock()
    job = SimJob(clock, done_at=TEN_MINUTES)
    _, record = submit_long_job(consumer, state, clock, job)
    op_id = record["id"]
    assert hook(consumer, state, claude_payload("./gov op list")).returncode == 0
    assert gov(consumer, state, "list").returncode == 0
    for command in ("./gov op list", f"./gov op result {op_id}"):
        denied = hook(consumer, state, claude_payload(command))
        assert denied.returncode == 2 and "STATUS_QUERY" in denied.stderr, command
    # Without the hook the supervisor refuses the repetition itself.
    assert gov(consumer, state, "list").returncode == ops.REFUSED_EXIT
    assert gov(consumer, state, "result", op_id).returncode == ops.REFUSED_EXIT
    assert job.calls == []
    prompt = {"session_id": "s1", "hook_event_name": "UserPromptSubmit"}
    assert hook(consumer, state, prompt, "claude", "prompt").returncode == 0
    assert hook(consumer, state, claude_payload("./gov op list")).returncode == 0


@pytest.mark.parametrize(
    "template",
    [
        "./gov op list; tail -n 5 {log}",
        "./gov op wait {op} && cat {log}",
        "./gov op result {op} || sleep 30",
        "./gov op list | grep {op}; ls {directory}",
    ],
)
def test_composed_supervisor_commands_get_every_probe_check(consumer, state, template):
    clock = SimClock()
    _, record = submit_long_job(consumer, state, clock, SimJob(clock, TEN_MINUTES))
    command = template.format(
        log=record["artifacts"]["log"],
        op=record["id"],
        directory=record["artifacts"]["directory"],
    )
    denied = hook(consumer, state, {**claude_payload(command), "cwd": str(consumer)})
    assert denied.returncode == 2 and "OPERATION_PENDING" in denied.stderr, command


def test_denied_requests_never_reach_the_service(consumer, state):
    clock = SimClock()
    job = SimJob(clock, done_at=TEN_MINUTES)
    supervisor, record = submit_long_job(consumer, state, clock, job)
    op_id, log = record["artifacts"], record["artifacts"]["log"]
    dispatched = []

    def dispatch(payload):
        """A harness dispatch path: PreToolUse first, the tool only if allowed."""
        verdict = hook(consumer, state, {**payload, "cwd": str(consumer)})
        if verdict.returncode == 0:
            dispatched.append(payload)
            if "op wait" in payload["tool_input"].get("command", ""):
                supervisor.wait(record["id"])
        return verdict.returncode

    lock = supervisor.registry.wait_lock_path(record["id"])
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)  # a supervised wait is attached
        for payload in (
            claude_payload(f"./gov op wait {record['id']}"),
            claude_payload(f"tail -n 5 {log}"),
            claude_payload(f"./gov op list; cat {log}"),
            tool_payload("Read", {"file_path": log}),
            claude_payload("make test"),
        ):
            assert dispatch(payload) == 2
        fcntl.flock(held, fcntl.LOCK_UN)
    assert dispatched == [] and job.calls == [] and job.starts == 1
    stored = supervisor.registry.load(record["id"])
    assert stored["total_consumed"] == {"wait_seconds": 0.0, "requests": 0, "waits": 0}
    assert op_id  # the record itself is untouched by denials


# ------------------------------------- 12. outcomes and decision records


def test_cancellation_is_terminal_with_evidence_and_never_verification(consumer, state):
    def runner(command, **_):
        payload = {
            "status": "completed",
            "conclusion": "cancelled",
            "headSha": "a" * 40,
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    clock = SimClock()
    service = ops.GitHubRunService(runner)
    supervisor = make_supervisor(
        consumer, state, clock, {"github-actions-run": service}
    )
    record = supervisor.submit(
        "github-actions-run", {"run_id": "9", "repository": "o/r", "head_sha": "a" * 40}
    )
    result = supervisor.wait(record["id"]).as_dict()
    assert (result["status"], result["reason"]) == (ops.CANCELLED, "completed")
    assert (
        result["verification"] is False
        and result["last_state"] == "cancelled (cancelled)"
    )
    assert result["mechanism"] == POLLING and result["requests_used"] == 1


def test_results_expose_mechanism_versions_transitions_budgets_and_errors(
    consumer, state
):
    clock = SimClock()
    job = SimJob(clock, done_at=60.0)
    supervisor, record = submit_long_job(consumer, state, clock, job)
    # A hook enforcement error while the operation is pending is recorded with it.
    classifier = consumer / "tools/workspace_governance/command_policy.py"
    classifier.write_text(
        classifier.read_text()
        + "\n\ndef evaluate(*_, **__):\n    raise RuntimeError('broken classifier')\n"
    )
    degraded = hook(
        consumer, state, {**claude_payload("git status"), "cwd": str(consumer)}
    )
    assert degraded.returncode == 0 and "GUARD_DEGRADED" in degraded.stderr
    report = supervisor.wait(record["id"]).as_dict()
    assert report["status"] == SUCCEEDED and report["mechanism"] == POLLING
    assert report["profile_version"] == {
        "profiles_id": "managed-operation-profiles-v1",
        "profile_sha256": ops.profile_digest(ops.load_profiles()["local-process"]),
    }
    assert report["budget"] == ops.load_profiles()["local-process"]["budget"]
    assert report["consumed"]["requests"] == len(job.calls)
    assert report["remaining"]["requests"] == report["budget"]["max_requests"] - len(
        job.calls
    )
    assert [t["reason"] for t in report["transitions"]] == [
        "preferred_unavailable_authorized_polling",
        "completed",
    ]
    assert any(
        "command classifier failed" in e["detail"] for e in report["enforcement_errors"]
    )


def test_frozen_profile_cannot_be_weakened_while_pending(consumer, state):
    clock = SimClock()
    job = SimJob(clock, done_at=TEN_MINUTES)
    supervisor, record = submit_long_job(consumer, state, clock, job)
    stored = supervisor.registry.load(record["id"])
    stored["spec"]["completion"][POLLING]["min_interval_seconds"] = 1
    supervisor.registry.save(stored)
    result = supervisor.wait(record["id"])
    assert (result.status, result.reason) == (MECHANISM_ERROR, "mechanism_failure")
    assert "modified after submission" in result.detail and job.calls == []


def test_synchronous_deadline_preserves_the_running_process(consumer, state):
    profiles = ops.load_profiles()
    profiles["local-command"] = json.loads(json.dumps(profiles["local-command"]))
    profiles["local-command"]["budget"]["deadline_seconds"] = 2
    profiles["local-command"]["completion"]["synchronous_return"] = {
        "response_window_seconds": 1
    }
    profiles["local-command"]["completion"][POLLING]["min_interval_seconds"] = 1
    gate = consumer / "release-gate"
    script = f"while [ ! -e {gate} ]; do sleep 0.1; done; echo done"
    supervisor = Supervisor(consumer, profiles=profiles, state_base=state)
    record = supervisor.submit(
        "local-command", {"argv": ["/bin/sh", "-c", script], "cwd": str(consumer)}
    )
    first = supervisor.wait(record["id"])
    assert (first.status, first.mechanism) == (DEADLINE_EXPIRED, "synchronous_return")
    assert first.reason == "deadline_expired" and first.requests_used == 2
    pid = supervisor.registry.load(record["id"])["identity"]["pid"]
    assert ops._alive(pid)  # never cancelled on deadline
    gate.touch()
    supervisor.registry.advance_user_epoch()
    resumed = Supervisor(consumer, profiles=profiles, state_base=state)
    second = resumed.wait(record["id"])
    assert second.status == SUCCEEDED and second.operation == record["id"]
    assert second.allotment == 2 and second.mechanism == POLLING
    assert "user_instruction_renewed_budget" in reasons(second)
    assert second.log_tail.count("done") == 1  # started exactly once
