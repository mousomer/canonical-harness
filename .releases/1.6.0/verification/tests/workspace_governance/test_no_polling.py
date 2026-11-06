"""NO_AUTONOMOUS_POLLING: shared evaluator, runtime adapters, ci-status, review."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from support import PACK, build_checkout
from tools.workspace_governance import ci_status, command_policy, hook_adapters
from tools.workspace_governance.execution_policy import review_execution
from tools.workspace_governance.validators.core import validate_pack

ALIASES = {
    "co": "pr checkout",
    "w": "run watch",
    "chk": "pr checks",
    "sh-w": "!gh run watch $1",
}

DENY_CASES = [
    # mandatory incident and watch forms
    ("gh pr checks 176 --watch", "TOOL_WATCH_MODE"),
    ("gh pr checks --watch 176", "TOOL_WATCH_MODE"),
    ("gh pr checks 176 --watch --interval 5", "TOOL_WATCH_MODE"),
    ("gh run watch 12345", "TOOL_WATCH_MODE"),
    ("gh run watch 12345 -i 5", "TOOL_WATCH_MODE"),
    ("watch gh pr checks 176", "WATCH_WRAPPER"),
    ("while true; do gh pr checks 176; sleep 10; done", "POLLING_LOOP"),
    ("bash -lc 'gh pr checks 176 --watch'", "TOOL_WATCH_MODE"),
    ("zsh -lc 'gh run watch 12345'", "TOOL_WATCH_MODE"),
    # flag positions and interval modes
    ("gh pr checks 176 --watch=true", "TOOL_WATCH_MODE"),
    ("gh -R o/r pr checks --watch", "TOOL_WATCH_MODE"),
    ("gh pr checks 176 -i 5", "TOOL_WATCH_MODE"),
    ("gh pr checks 176 --interval=5", "TOOL_WATCH_MODE"),
    ("gh pr checks 176 --fail-fast", "TOOL_WATCH_MODE"),
    ("/opt/homebrew/bin/gh run watch 1", "TOOL_WATCH_MODE"),
    # aliases
    ("gh w 12345", "TOOL_WATCH_MODE"),
    ("gh chk 176 --watch", "TOOL_WATCH_MODE"),
    ("gh sh-w 12345", "TOOL_WATCH_MODE"),
    # wrappers, nesting, chains, pipes, heredocs
    ("env GH_PAGER= timeout 600 gh run watch 1", "TOOL_WATCH_MODE"),
    ("nohup gh pr checks 1 --watch > out.log 2>&1 &", "TOOL_WATCH_MODE"),
    ("git push && gh pr checks 176 --watch", "TOOL_WATCH_MODE"),
    ("sh -c \"bash -lc 'gh run watch 9'\"", "TOOL_WATCH_MODE"),
    ("bash -o pipefail -c 'gh run watch 1'", "TOOL_WATCH_MODE"),
    ("echo 'gh run watch 1' | bash", "TOOL_WATCH_MODE"),
    ("bash <<'EOF'\ngh run watch 1\nEOF", "TOOL_WATCH_MODE"),
    ("bash <<< 'gh run watch 1'", "TOOL_WATCH_MODE"),
    ("eval 'gh run watch 1'", "TOOL_WATCH_MODE"),
    ('echo "$(gh run watch 1)"', "TOOL_WATCH_MODE"),
    # loops and synthetic waiting
    ("until gh pr checks 176 --json state; do sleep 30; done", "POLLING_LOOP"),
    (
        (
            "until [ \"$(gh pr checks 176 --json bucket -q '.[0].bucket')\" = pass ]; "
            "do sleep 5; done"
        ),
        "POLLING_LOOP",
    ),
    ("while :\ndo\n  gh run view 1 --json status\ndone", "POLLING_LOOP"),
    ("for i in 1 2 3; do gh pr checks 176; done", "POLLING_LOOP"),
    ("for i in $(seq 9); do gh pr checks $i; sleep 5; done", "POLLING_LOOP"),
    ("f() { gh pr checks 176; }; while :; do f; done", "POLLING_LOOP"),
    ("while true; do curl -s https://api.github.com/x; done", "POLLING_LOOP"),
    ("sleep 60 && gh pr checks 176", "SYNTHETIC_WAIT"),
    ("sleep 30; ./gov ci-status 176", "SYNTHETIC_WAIT"),
    ("gh pr checks 176; gh pr checks 176", "REPEATED_STATUS_QUERY"),
    ("glab ci status --live", "TOOL_WATCH_MODE"),
    # review reproductions: repeaters, recursion, other CLIs, spellings
    ("seq 30 | xargs -I{} sh -c 'gh pr checks 1; sleep 30'", "POLLING_LOOP"),
    ("find . -maxdepth 0 -exec sh -c 'gh pr checks 1; sleep 30' \\;", "POLLING_LOOP"),
    ("repeat 30 { gh pr checks 1; sleep 30 }", "POLLING_LOOP"),
    ("f(){ gh pr checks 1; sleep 30; f; }; f", "POLLING_LOOP"),
    ("sudo gh pr checks 1 --watch", "TOOL_WATCH_MODE"),
    ("kubectl get pods -w", "TOOL_WATCH_MODE"),
    ("kubectl rollout status deploy/x", "TOOL_WATCH_MODE"),
    ("aws cloudformation wait stack-create-complete --stack-name s", "TOOL_WATCH_MODE"),
    ("gcloud builds log abc --stream", "TOOL_WATCH_MODE"),
    (
        "gh pr checks 7; gh pr checks https://github.com/o/r/pull/7",
        "REPEATED_STATUS_QUERY",
    ),
    (
        "gh pr checks 7 && gh pr view 7 --json statusCheckRollup",
        "REPEATED_STATUS_QUERY",
    ),
    ("gh pr checks 7; ./gov ci-status 7", "REPEATED_STATUS_QUERY"),
    (
        "until gh pr view 7 --json state -q .state | grep -q MERGED; do sleep 9; done",
        "POLLING_LOOP",
    ),
]

UNDETERMINED_CASES = [
    "case $x in a) gh pr checks 1;; esac",
    "gh pr checks 176 'unterminated",
    "$GH pr checks 176",
    "gh some-extension status",
    "python3 -c 'import subprocess,time\nwhile True:\n subprocess.run([\"gh\"]); time.sleep(5)'",
]

ALLOW_CASES = [
    # review reproductions: ordinary reads, distinct values, text, retries
    "gh run view 99 --json jobs && gh run view 99 --log-failed",
    "gh pr view 5 && gh pr view 5 --comments",
    "gh pr view 1 --json title && gh pr view 1 --json body",
    'f(){ gh pr view "$1"; }; f 1; f 2',
    'while read -r pr; do gh pr view "$pr"; done < prs.txt',
    "gh pr list --json number -q '.[].number' | while read n; do gh pr view $n; done",
    'gh pr list --search "--watch"',
    "gh pr list --state open",
    "for i in 1 2 3; do gh pr create --fill && break; sleep 5; done",
    "perl -pi -e 's/gh pr checks --watch//; s/sleep 30//' a.md",
    "python3 - <<'X'\nt = t.replace('use gh pr checks --watch', 'never sleep and re-run gh')\nX",
    (
        'python3 -c "import time; from urllib.request import urlopen; time.sleep(1); '
        "urlopen('http://localhost:8000')\""
    ),
    "kubectl get pods",
    # mandatory acceptance cases
    "gh pr checks 176 --json name,state,bucket",
    "gh run view 12345 --json status,conclusion",
    "git status",
    "git diff --check",
    "CODEX_MODE=1 ./scripts/verify.sh",
    # text that merely contains watch/polling syntax
    "grep -rn -- '--watch' tools/",
    "rg 'gh pr checks --watch' docs",
    "git commit -m 'forbid gh pr checks --watch and gh run watch'",
    "cat > t.py <<'EOF'\nwhile True:\n    run('gh run watch 1')\n    sleep(5)\nEOF",
    "printf '%s\\n' 'while true; do gh pr checks 1; sleep 9; done' > docs/x.md",
    "sed -n 1,20p tests/test_watch.py",
    # local process synchronization and ordinary loops
    "until curl -s localhost:8000/health; do sleep 1; done",
    "tail -f build.log",
    "npm run dev -- --watch",
    "pytest -q tests/",
    "for f in *.py; do ruff check $f; done",
    "while read -r line; do echo $line; done < input.txt",
    "python3 -c 'for i in range(3): print(i)'",
    # one-shot remote reads and actions
    "gh pr checks 176 -w",
    "gh pr view 176 --json headRefOid,state",
    "gh co 176",
    "./gov ci-status 176",
    "for pr in 1 2 3; do gh pr checks $pr --json state; done",
    "gh pr merge 176 --merge",
    "gh pr checks 176 --json bucket && gh run view 9 --json status",
    "gh api repos/o/r/pulls/1 -X PATCH -f title=x; sleep 1",
]


@pytest.mark.parametrize(("command", "violation"), DENY_CASES)
def test_polling_forms_are_denied(command, violation):
    decision = command_policy.evaluate({"tool": "Bash", "command": command}, ALIASES)
    assert (decision.decision, decision.violation) == ("DENY", violation), command
    assert decision.as_dict()["code"] == "NO_AUTONOMOUS_POLLING"


@pytest.mark.parametrize("command", UNDETERMINED_CASES)
def test_ambiguous_remote_invocations_are_undetermined(command):
    decision = command_policy.evaluate({"tool": "Bash", "command": command}, ALIASES)
    assert decision.decision == "UNDETERMINED", command


@pytest.mark.parametrize("command", ALLOW_CASES)
def test_unrelated_and_one_shot_commands_are_allowed(command):
    decision = command_policy.evaluate({"tool": "Bash", "command": command}, ALIASES)
    assert decision.decision == "ALLOW", (command, decision)


def test_one_shot_reads_report_their_observation_targets():
    decision = command_policy.evaluate(
        {"tool": "Bash", "command": "gh pr checks 176 --json name,state,bucket"}
    )
    assert decision.observations == ("pr:176",)
    for command, key in (
        ("gh pr checks https://github.com/o/r/pull/176", "pr:176"),
        ("gh pr view 176 --json statusCheckRollup", "pr:176"),
        ("./gov ci-status 176", "pr:176"),
        ("gh run view 5 --json status,conclusion", "run:5"),
        ("gh api repos/o/r/commits/abc1234/check-runs", "sha:abc1234"),
        ("gh api repos/o/r/actions/runs/9", "run:9"),
    ):
        observed = command_policy.evaluate({"tool": "Bash", "command": command})
        assert observed.observations == (key,), command
    for command in ("gh pr view 5 --comments", "gh pr list", "gh api repos/o/r"):
        observed = command_policy.evaluate({"tool": "Bash", "command": command})
        assert observed.observations == (), command


def test_script_files_are_classified_through_the_reader(tmp_path):
    poll = "#!/bin/sh\nwhile true; do gh pr checks 1; sleep 30; done\n"
    files = {"poll.sh": poll, "./poll.sh": poll, "ok.sh": "#!/bin/bash\necho hi\n"}

    def reader(path):
        return files.get(path)

    for command in ("bash poll.sh", "./poll.sh", "source poll.sh", ". poll.sh"):
        decision = command_policy.evaluate(
            {"tool": "Bash", "command": command}, None, reader
        )
        assert decision.decision == "DENY", command
    ok = command_policy.evaluate(
        {"tool": "Bash", "command": "bash ok.sh"}, None, reader
    )
    assert ok.decision == "ALLOW"
    missing = command_policy.evaluate(
        {"tool": "Bash", "command": "bash gone.sh"}, None, reader
    )
    assert missing.decision == "ALLOW" and missing.limits
    verifier = (
        '#!/usr/bin/env bash\nrun_step() { local name=$1; shift; "$@"; }\n'
        "run_step doctor ./gov doctor --json\n"
        'run_step lint "$PYTHON_BIN" tools/check.py --checker "$name" --run "$@"\n'
        "run_step sanitation bash scripts/missing_$VARIANT.sh\n"
    )
    files["scripts/verify.sh"] = verifier
    real = command_policy.evaluate(
        {"tool": "Bash", "command": "CODEX_MODE=1 ./scripts/verify.sh"}, None, reader
    )
    assert real.decision == "ALLOW", real
    files["a.sh"] = "#!/bin/sh\nbash b.sh\n"
    files["b.sh"] = "#!/bin/sh\nbash a.sh\nbash c.sh\n"
    files["c.sh"] = "#!/bin/sh\nuntil gh pr checks 9; do sleep 5; done\n"
    chained = command_policy.evaluate(
        {"tool": "Bash", "command": "bash a.sh"}, None, reader
    )
    assert (chained.decision, chained.violation) == ("DENY", "POLLING_LOOP")
    dynamic = command_policy.evaluate({"tool": "Bash", "command": "$GH pr checks 1"})
    assert dynamic.decision == "UNDETERMINED"
    binary = command_policy.evaluate(
        {"tool": "Bash", "command": "./bin/tool"}, None, reader
    )
    assert binary.decision == "ALLOW"
    unread = command_policy.evaluate({"tool": "Bash", "command": "bash poll.sh"})
    assert unread.decision == "ALLOW" and unread.limits


def test_monitor_may_not_stream_remote_state():
    remote = command_policy.evaluate({"tool": "Monitor", "command": "gh run view 1"})
    local = command_policy.evaluate(
        {"tool": "Monitor", "command": "tail -f x | grep E"}
    )
    assert (remote.decision, remote.violation) == ("DENY", "MONITOR_REMOTE_STATE")
    assert local.decision == "ALLOW"


def test_evaluator_is_bounded_and_executes_nothing(tmp_path, monkeypatch):
    marker = tmp_path / "called"
    stub = tmp_path / "bin/gh"
    stub.parent.mkdir()
    stub.write_text(f"#!/bin/sh\ntouch {marker}\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub.parent}:{os.environ['PATH']}")
    command_policy.evaluate({"tool": "Bash", "command": "gh run watch 1"})
    command_policy.evaluate({"tool": "Bash", "command": "gh pr checks 1"})
    huge = "echo " + "x" * (command_policy.MAX_COMMAND_CHARS + 1)
    assert (
        command_policy.evaluate({"tool": "Bash", "command": huge}).decision == "ALLOW"
    )
    deep = "gh run watch 1"
    for _ in range(command_policy.MAX_DEPTH + 3):
        deep = "bash -c " + "'" + deep.replace("'", "'\\''") + "'"
    assert (
        command_policy.evaluate({"tool": "Bash", "command": deep}).decision != "ALLOW"
    )
    assert not marker.exists()


# ------------------------------------------------------------ runtime adapter


def run_hook(root: Path, payload: dict, *args: str, env: dict | None = None):
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(root),
        "TMPDIR": str(root / "tmp"),
        "GOVERNANCE_PYTHON": sys.executable,
        "GH_CONFIG_DIR": str(root / "gh"),
        **(env or {}),
    }
    (root / "tmp").mkdir(exist_ok=True)
    return subprocess.run(
        [str(root / hook_adapters.GUARD), *args],
        input=json.dumps(payload) if isinstance(payload, dict) else payload,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )


def claude_payload(command: str, tool: str = "Bash", session: str = "s1") -> dict:
    return {
        "session_id": session,
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"command": command, "description": "x"},
        "tool_use_id": "toolu_1",
    }


def codex_payload(command, turn: str = "t1", session: str = "c1") -> dict:
    return {
        "session_id": session,
        "turn_id": turn,
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


@pytest.fixture
def consumer(tmp_path):
    return build_checkout(tmp_path / "consumer")


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_launcher_blocks_incident_before_execution(consumer, runtime):
    payload = (claude_payload if runtime == "claude" else codex_payload)(
        "gh pr checks 176 --watch"
    )
    result = run_hook(consumer, payload, runtime)
    assert result.returncode == 2
    assert "NO_AUTONOMOUS_POLLING (TOOL_WATCH_MODE)" in result.stderr
    assert "gh pr checks N --json" in result.stderr
    allowed = run_hook(consumer, claude_payload("git status"), runtime)
    assert (allowed.returncode, allowed.stderr) == (0, "")


def test_codex_argv_commands_and_exec_command_are_classified(consumer):
    payload = codex_payload(["bash", "-lc", "gh run watch 12345"])
    payload["tool_name"] = "exec_command"
    assert run_hook(consumer, payload, "codex").returncode == 2


def test_user_gh_aliases_are_expanded(consumer):
    (consumer / "gh").mkdir()
    (consumer / "gh/config.yml").write_text(
        "version: 1\naliases:\n    ww: run watch\n    co: pr checkout\n"
    )
    assert run_hook(consumer, claude_payload("gh ww 1"), "claude").returncode == 2
    assert run_hook(consumer, claude_payload("gh co 1"), "claude").returncode == 0


def test_repeated_observation_needs_a_new_prompt_claude(consumer):
    first = run_hook(
        consumer, claude_payload("gh pr checks 176 --json state"), "claude"
    )
    again = run_hook(
        consumer, claude_payload("gh pr checks 176 --json bucket"), "claude"
    )
    assert first.returncode == 0
    assert again.returncode == 2 and "REPEATED_STATUS_QUERY" in again.stderr
    other = run_hook(
        consumer, claude_payload("gh pr checks 176", session="s2"), "claude"
    )
    assert other.returncode == 0
    prompt = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "x"}
    assert run_hook(consumer, prompt, "claude", "prompt").returncode == 0
    resumed = run_hook(consumer, claude_payload("gh pr checks 176"), "claude")
    assert resumed.returncode == 0


def test_repeated_observation_is_scoped_to_codex_turn(consumer):
    assert (
        run_hook(consumer, codex_payload("./gov ci-status 5"), "codex").returncode == 0
    )
    assert (
        run_hook(consumer, codex_payload("./gov ci-status 5"), "codex").returncode == 2
    )
    next_turn = codex_payload("./gov ci-status 5", turn="t2")
    assert run_hook(consumer, next_turn, "codex").returncode == 0


def test_hook_reads_script_files_relative_to_cwd(consumer):
    (consumer / "poll.sh").write_text("while :; do gh run view 1; sleep 5; done\n")
    payload = {**claude_payload("bash poll.sh"), "cwd": str(consumer)}
    result = run_hook(consumer, payload, "claude")
    assert result.returncode == 2 and "POLLING_LOOP" in result.stderr


def test_mcp_status_matching_uses_name_tokens():
    from tools.workspace_governance.hooks.no_polling import mcp_status_tool

    assert mcp_status_tool("mcp__ccd_pr__get_status")
    assert mcp_status_tool("mcp__github__list_check_runs")
    assert not mcp_status_tool("mcp__x__record_decision")
    assert not mcp_status_tool("mcp__x__git_checkout")


def test_repeated_status_mcp_tool_is_denied(consumer):
    payload = {
        "session_id": "m",
        "tool_name": "mcp__ccd_pr__get_status",
        "tool_input": {"pr": 1},
    }
    assert run_hook(consumer, payload, "claude").returncode == 0
    assert run_hook(consumer, payload, "claude").returncode == 2
    other = {**payload, "tool_name": "mcp__files__read_file"}
    assert run_hook(consumer, other, "claude").returncode == 0


@pytest.mark.parametrize(
    "defect",
    ["no_interpreter", "broken_classifier", "malformed_input", "unknown_runtime"],
)
def test_guard_failure_blocks_tool_calls(consumer, defect):
    payload: dict | str = claude_payload("git status")
    env: dict[str, str] = {}
    runtime = "claude"
    if defect == "no_interpreter":
        env["GOVERNANCE_PYTHON"] = str(consumer / "missing-python")
    elif defect == "broken_classifier":
        (consumer / "tools/workspace_governance/command_policy.py").write_text(
            "raise\n"
        )
    elif defect == "malformed_input":
        payload = "{not json"
    else:
        runtime = "other"
    result = run_hook(consumer, payload, runtime, env=env)
    assert result.returncode == 2
    assert "NO_AUTONOMOUS_POLLING (GUARD_FAILURE)" in result.stderr


def test_guard_failure_never_blocks_a_user_prompt(consumer):
    env = {"GOVERNANCE_PYTHON": str(consumer / "missing-python")}
    prompt = {"session_id": "s", "hook_event_name": "UserPromptSubmit"}
    assert run_hook(consumer, prompt, "claude", "prompt", env=env).returncode == 0


def test_corrupt_observation_state_fails_closed_then_recovers(consumer):
    payload = claude_payload("gh pr checks 1")
    assert run_hook(consumer, payload, "claude").returncode == 0
    state_dir = consumer / "tmp" / "workspace-governance-no-polling"
    for path in state_dir.iterdir():
        path.write_text("[]")
    failed = run_hook(consumer, payload, "claude")
    assert failed.returncode == 2 and "GUARD_FAILURE" in failed.stderr
    assert run_hook(consumer, payload, "claude").returncode == 0


# ------------------------------------------------------------ adapter binding


def test_adapters_are_bound_and_rendering_is_idempotent(consumer):
    assert hook_adapters.check(consumer) == []
    rendered = hook_adapters.render(consumer)
    for name, data in rendered.items():
        assert (consumer / name).read_bytes() == data
    assert hook_adapters.render(consumer) == rendered


def test_merge_preserves_unrelated_configuration(tmp_path):
    settings = {
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {
            "PreToolUse": [
                {"matcher": "Edit", "hooks": [{"type": "command", "command": "fmt"}]}
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "notify"}]}],
        },
    }
    path = tmp_path / hook_adapters.CLAUDE_PATH
    path.parent.mkdir()
    path.write_text(json.dumps(settings))
    merged = json.loads(hook_adapters.render(tmp_path)[hook_adapters.CLAUDE_PATH])
    assert merged["permissions"] == settings["permissions"]
    assert merged["hooks"]["Stop"] == settings["hooks"]["Stop"]
    assert merged["hooks"]["PreToolUse"][0] == settings["hooks"]["PreToolUse"][0]
    assert (
        merged["hooks"]["PreToolUse"][1] == hook_adapters.claude_entries()["PreToolUse"]
    )
    again = hook_adapters.merge(merged, hook_adapters.claude_entries())
    assert again == merged


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "malformed",
        "unbound",
        "altered",
        "disabled",
        "not_executable",
        "ignored",
    ],
)
def test_missing_or_malformed_hook_configuration_fails_gov_check(consumer, defect):
    claude = consumer / hook_adapters.CLAUDE_PATH
    data = json.loads(claude.read_text())
    if defect == "missing":
        (consumer / hook_adapters.CODEX_PATH).unlink()
    elif defect == "malformed":
        claude.write_text("{")
    elif defect == "unbound":
        data["hooks"]["PreToolUse"] = []
        claude.write_text(json.dumps(data))
    elif defect == "altered":
        data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] += " ; true"
        claude.write_text(json.dumps(data))
    elif defect == "disabled":
        data["disableAllHooks"] = True
        claude.write_text(json.dumps(data))
    elif defect == "ignored":
        subprocess.run(["git", "init", "-q", str(consumer)], check=True)
        (consumer / ".gitignore").write_text(".claude/*\n")
    else:
        (consumer / hook_adapters.GUARD).chmod(0o644)
    assert hook_adapters.check(consumer)
    workspace = json.loads((consumer / ".governance/workspace.json").read_text())
    issues = validate_pack(consumer, workspace)
    assert any(i.fact == "execution.enforcement" for i in issues)


def test_pack_exports_enforcement_artifacts():
    manifest = json.loads((PACK / "MANIFEST.json").read_text())
    exported = {entry["path"] for entry in manifest["artifact_roles"]}
    assert {
        "command_policy.py",
        "hook_adapters.py",
        "hooks/no_polling.py",
        "hooks/no_polling.sh",
        "ci_status.py",
    } <= exported
    assert manifest["execution_policy"]["id"] == "synchronous-completion-v2"
    assert "ci-status" in manifest["commands"]
    text = (PACK / "policies/execution.md").read_text()
    assert "blocking watch's internal" not in text
    assert "does not add remote CI" in text


# ------------------------------------------------------------ ci-status


def check_run(name, status="COMPLETED", conclusion="SUCCESS", required=True):
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "isRequired": required,
    }


def response(nodes, *, head="a" * 40, more=False):
    return {
        "data": {
            "repository": {
                "nameWithOwner": "o/r",
                "pullRequest": {
                    "number": 7,
                    "state": "OPEN",
                    "headRefName": "feature",
                    "headRefOid": head,
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "oid": head,
                                    "statusCheckRollup": {
                                        "contexts": {
                                            "pageInfo": {"hasNextPage": more},
                                            "nodes": nodes,
                                        }
                                    },
                                }
                            }
                        ]
                    },
                },
            }
        }
    }


class FakeGit:
    def __init__(self, reply, code=0, branch="other"):
        self.reply, self.code, self.branch = reply, code, branch
        self.gh_calls: list[list[str]] = []

    def __call__(self, argv, cwd):
        if argv[0] == "gh":
            self.gh_calls.append(argv)
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.code, json.dumps(self.reply), "boom" if self.code else ""
        if argv[1:3] == ["config", "--get"]:
            return 0, "git@github.com:o/r.git\n", ""
        if argv[1] == "symbolic-ref":
            return 0, self.branch + "\n", ""
        return 0, "b" * 40 + "\n", ""


@pytest.mark.parametrize(
    ("nodes", "state"),
    [
        ([check_run("ci")], "PASS"),
        ([check_run("ci", conclusion="FAILURE")], "FAIL"),
        ([check_run("ci", conclusion="CANCELLED")], "FAIL"),
        ([check_run("ci", conclusion="TIMED_OUT")], "FAIL"),
        ([check_run("ci", status="IN_PROGRESS", conclusion=None)], "PENDING"),
        (
            [check_run("ci"), check_run("lint", status="QUEUED", conclusion=None)],
            "PENDING",
        ),
        ([], "UNKNOWN"),
        (
            [{"__typename": "StatusContext", "context": "ext", "state": "PENDING"}],
            "PENDING",
        ),
        (
            [check_run("ci"), check_run("opt", conclusion="FAILURE", required=False)],
            "PASS",
        ),
    ],
)
def test_ci_status_states_from_one_request(tmp_path, nodes, state):
    fake = FakeGit(response(nodes))
    result = ci_status.ci_status(tmp_path, 7, runner=fake)
    assert result["state"] == state
    assert result["exit_code"] == ci_status.EXIT[state]
    assert result["verification"] is (state == "PASS")
    assert len(fake.gh_calls) == 1
    argv = " ".join(fake.gh_calls[0])
    assert "--watch" not in argv and "--interval" not in argv
    if state == "PENDING":
        assert result["code"] == "PENDING_EXTERNAL" and result["next"]


def test_ci_status_without_required_checks_is_unknown_unless_explicit(tmp_path):
    optional = response([check_run("fast-lint", required=False)])
    assert (
        ci_status.ci_status(tmp_path, 7, runner=FakeGit(optional))["state"] == "UNKNOWN"
    )
    explicit = ci_status.ci_status(
        tmp_path, 7, use_observed_set=True, runner=FakeGit(optional)
    )
    assert explicit["state"] == "PASS"
    declared = ci_status.ci_status(
        tmp_path, 7, require=["fast-lint"], runner=FakeGit(optional)
    )
    assert declared["state"] == "PASS"


def test_ci_status_unknown_and_error_are_never_pass(tmp_path):
    missing = ci_status.ci_status(
        tmp_path, 7, require=["deploy"], runner=FakeGit(response([check_run("ci")]))
    )
    assert missing["state"] == "UNKNOWN"
    truncated = ci_status.ci_status(
        tmp_path, 7, runner=FakeGit(response([check_run("ci")], more=True))
    )
    assert truncated["state"] == "UNKNOWN"
    stale = ci_status.ci_status(
        tmp_path, 7, expected_sha="c" * 40, runner=FakeGit(response([check_run("ci")]))
    )
    assert stale["state"] == "UNKNOWN" and not stale["head_sha_verified"]
    zero = ci_status.ci_status(
        tmp_path, 7, allow_zero=True, runner=FakeGit(response([]))
    )
    assert zero["state"] == "PASS"
    failed = ci_status.ci_status(tmp_path, 7, runner=FakeGit({}, code=1))
    assert (failed["state"], failed["exit_code"]) == ("ERROR", 13)
    errors = ci_status.ci_status(tmp_path, 7, runner=FakeGit({"errors": [{"m": 1}]}))
    assert errors["state"] == "ERROR"
    timeout = subprocess.TimeoutExpired("gh", 60)
    assert ci_status.ci_status(tmp_path, 7, runner=FakeGit(timeout))["state"] == "ERROR"
    assert ci_status.EXIT["FAIL"] != ci_status.EXIT["ERROR"]


def test_ci_status_verifies_local_head_when_on_the_pr_branch(tmp_path):
    fake = FakeGit(response([check_run("ci")], head="b" * 40), branch="feature")
    result = ci_status.ci_status(tmp_path, 7, runner=fake)
    assert result["head_sha_verified"] is True
    moved = FakeGit(response([check_run("ci")], head="d" * 40), branch="feature")
    assert ci_status.ci_status(tmp_path, 7, runner=moved)["state"] == "UNKNOWN"


# ------------------------------------------------------------ behaviour review

SHA = "a" * 40


def start():
    return [{"event": "start", "id": "pr-176", "mode": "remote", "sha": SHA}]


def test_pending_is_one_observation_then_stop():
    trace = start() + [{"event": "observation", "id": "pr-176", "result": "pending"}]
    result = review_execution(trace)
    assert result["status"] == "pending_external"
    again = trace + [{"event": "observation", "id": "pr-176", "result": "pass"}]
    assert review_execution(again)["violations"] == ["repeated_status_query"]


def test_notification_authorizes_exactly_one_new_observation():
    trace = start() + [
        {"event": "observation", "id": "pr-176", "result": "pending"},
        {"event": "notification", "id": "pr-176"},
        {"event": "observation", "id": "pr-176", "result": "pass"},
    ]
    assert review_execution(trace)["status"] == "success"
    user = start() + [
        {"event": "observation", "id": "pr-176", "result": "pending"},
        {"event": "user_instruction", "id": "pr-176"},
        {"event": "observation", "id": "pr-176", "result": "fail"},
    ]
    assert review_execution(user)["status"] == "failure"


@pytest.mark.parametrize("result", ["unknown", "error", "nonsense"])
def test_unknown_and_error_observations_never_verify(result):
    trace = start() + [{"event": "observation", "id": "pr-176", "result": result}]
    assert review_execution(trace)["status"] == "unknown"


def test_unnamed_event_reauthorizes_and_failure_outranks_pending():
    trace = start() + [
        {"event": "observation", "id": "pr-176", "result": "pending"},
        {"event": "user_instruction"},
        {"event": "observation", "id": "pr-176", "result": "pass"},
    ]
    assert review_execution(trace)["status"] == "success"
    mixed = start() + [
        {"event": "observation", "id": "pr-176", "result": "pending"},
        {"event": "start", "id": "local", "mode": "local"},
        {"event": "finish", "id": "local", "exit_code": 1},
    ]
    assert review_execution(mixed)["status"] == "failure"
    late = start() + [
        {"event": "observation", "id": "pr-176", "result": "pass"},
        {"event": "notification", "id": "pr-176"},
        {"event": "command", "command": "gh pr checks 1"},
        {"event": "notification", "id": "pr-176"},
        {"event": "command", "command": "gh pr checks 1"},
    ]
    result = review_execution(late)
    assert result["violations"] == [] and result["status"] == "success"


def test_local_gate_needs_no_remote_ci():
    trace = [
        {"event": "start", "id": "verify-master", "mode": "local"},
        {"event": "finish", "id": "verify-master", "exit_code": 0},
    ]
    result = review_execution(trace)
    assert result["status"] == "success"
    assert result["observations"] == []


def test_command_traces_distinguish_blocked_executed_and_repeated():
    blocked = review_execution(
        [{"event": "command", "command": "gh pr checks 176 --watch", "executed": False}]
    )
    assert blocked["violations"] == []
    assert blocked["blocked"][0]["event"] == "execution.blocked_attempt"
    executed = review_execution(
        [{"event": "command", "command": "gh pr checks 176 --watch", "executed": True}]
    )
    assert executed["violations"] == ["polling_executed"]
    one = {"event": "command", "command": "gh pr checks 176 --json state"}
    assert review_execution([one])["violations"] == []
    assert review_execution([one, one])["violations"] == ["repeated_status_query"]
    resumed = review_execution([one, {"event": "notification"}, one])
    assert resumed["violations"] == []
    assert resumed["classifier"] == command_policy.VERSION
