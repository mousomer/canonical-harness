import copy
import json
import subprocess
from pathlib import Path

import duckdb
import pytest
from jsonschema import ValidationError

from harness_observatory.cli import main, write_bundle
from harness_observatory.enrichers import enrich_actions, enrich_git, governance_snapshot
from harness_observatory.extractors import extract, normalize_repository
from harness_observatory.model import dumps, timestamp, validate, validate_bundle, validator
from harness_observatory.redaction import sanitize
from harness_observatory.warehouse import build_warehouse

FIXTURES = None  # Per-test temporary provider inputs from conftest.py.


def sample(provider="codex"):
    return extract((FIXTURES / f"{provider}.jsonl").read_bytes(), provider)


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return (
            subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)
            .decode()
            .strip()
        )

    git("init", "-b", "main")
    git("config", "user.name", "Synthetic Test")
    git("config", "user.email", "test@example.invalid")
    (repo / "AGENTS.md").write_text("First governance\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "VERIFY.md").write_text("Run synthetic tests\n")
    (repo / "nested").mkdir()
    (repo / "nested" / "AGENTS.md").write_text("Nested instructions\n")
    git("add", ".")
    git("commit", "-m", "Synthetic starting state")
    start = git("rev-parse", "HEAD")
    (repo / "AGENTS.md").write_text("Changed governance\n")
    (repo / "example.py").write_text("print('synthetic')\n")
    git("add", ".")
    git("commit", "-m", "Synthetic ending state")
    end = git("rev-parse", "HEAD")
    return repo, start, end


def test_equivalent_actions_and_idempotence():
    a, b = sample(), sample("claude")
    assert a == sample()
    assert json.loads(dumps(a)) == a

    def actions(bundle):
        return [e["event_type"] for e in bundle["events"] if e["event_type"] != "run.started"]

    assert (
        actions(a)
        == actions(b)
        == [
            "user.message",
            "tool.started",
            "command.started",
            "ci.status_query",
            "tool.finished",
            "agent.message",
        ]
    )
    for bundle in [a, b]:
        validate_bundle(bundle)
        assert bundle["run"]["facts"]["user_turn_count"]["value"] == 1
        assert bundle["run"]["facts"]["end_sha"]["value"] is None
        assert "synthetic success" not in dumps(bundle)


def test_schema_rejects_untyped_data():
    e = copy.deepcopy(sample()["events"][0])
    e["data"]["arbitrary"] = {"untyped": True}
    with pytest.raises(ValidationError):
        validate("event", e)
    e["data"] = {}
    e["timestamp"] = "bad"
    with pytest.raises(ValidationError):
        validate("event", e)
    p = e["provenance"]
    p["confidence"] = 1.1
    with pytest.raises(ValidationError):
        validate("provenance", p)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_malformed_records_are_reported(provider):
    b = extract(b'not-json\n[]\n{"type":"assistant","message":42}\n', provider)
    assert sum(w["code"] == "malformed_record" for w in b["warnings"]) == 3


def test_schema_validators_are_cached():
    assert validator("event") is validator("event")


@pytest.mark.parametrize(
    ("repository", "expected"),
    [
        ("git@Example.COM:owner/repo.git", "https://example.com/owner/repo"),
        (
            "https://user:secret@Example.COM/owner/repo.git?token=secret",
            "https://example.com/owner/repo",
        ),
        ("owner/repo.git", "owner/repo"),
    ],
)
def test_repository_normalization(repository, expected):
    assert normalize_repository(repository) == expected


def test_unsupported_record_type_is_preserved():
    raw = b'{"type":"future-record","timestamp":"2026-01-01T00:00:00Z","payload":{}}\n'
    warning = extract(raw, "codex")["warnings"][0]
    assert warning["code"] == "unsupported_or_mirror_record"
    assert warning["source_record_type"] == "future-record"


def test_schema_0_2_tokens_compaction_lifecycle_and_ci_taxonomy():
    rows = [
        {
            "type": "session_" + "meta",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"id": "synthetic"},
        },
        {"type": "compacted", "timestamp": "2026-01-01T00:00:01Z", "payload": {}},
        {
            "type": "token_usage_record",
            "timestamp": "2026-01-01T00:00:02Z",
            "payload": {"usage": {"input_tokens": 10, "output_tokens": 4, "cached_tokens": 3}},
        },
    ]
    for index, command in enumerate(
        [
            "gh run view 123",
            "gh run watch 123",
            "gh run list",
            "gh pr checks 4",
            "gh run download 123",
        ]
    ):
        rows.append(
            {
                "type": "response_" + "item",
                "timestamp": f"2026-01-01T00:00:{index + 3:02d}Z",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": f"call-{index}",
                    "arguments": json.dumps({"cmd": command}),
                },
            }
        )
    bundle = extract(("\n".join(json.dumps(row) for row in rows) + "\n").encode(), "codex")
    run = bundle["run"]
    assert run["schema_version"] == "0.2" and run["extractor_version"] == "0.4.0"
    assert run["facts"]["input_tokens"]["value"] == 10
    assert run["facts"]["output_tokens"]["value"] == 4
    assert run["facts"]["cache_read_tokens"]["value"] == 3
    assert run["facts"]["compaction_count"]["value"] == 1
    assert run["facts"]["lifecycle_status"]["value"] == "finish_unobserved"
    assert run["facts"]["ci_observation_count"]["value"] == 5
    assert run["facts"]["ci_poll_count"]["value"] == 0
    assert {e["event_type"] for e in bundle["events"] if e["event_type"].startswith("ci.")} == {
        "ci.status_query",
        "ci.blocking_watch",
        "ci.run_list_query",
        "ci.pr_check_query",
        "ci.artifact_query",
    }
    (violation,) = [e for e in bundle["events"] if e["event_type"] == "execution.polling_violation"]
    assert violation["data"]["violation"] == "TOOL_WATCH_MODE"
    assert violation["data"]["status"] == "outcome_unobserved"


def test_legacy_0_1_schema_remains_loadable():
    legacy_provenance = {
        "schema_version": "0.1",
        "source": "codex",
        "method": "unavailable",
        "confidence": 1,
        "evidence_refs": ["sha256:" + "a" * 64],
        "extractor": "harness-observatory/0.1.0",
    }
    validate("provenance", legacy_provenance)


def test_bounded_structured_tool_results_and_poll_v2():
    sha = "a" * 40
    surrounding = "private surrounding text must disappear"
    rows = [
        {
            "type": "session_" + "meta",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"id": "synthetic"},
        },
        {
            "type": "response_" + "item",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "git-1",
                "arguments": json.dumps({"cmd": "git rev-parse HEAD"}),
            },
        },
        {
            "type": "response_" + "item",
            "timestamp": "2026-01-01T00:00:02Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "git-1",
                "output": f"{surrounding}\n{sha}\n",
            },
        },
    ]
    for index, status in enumerate(["in_progress", "success"]):
        rows.extend(
            [
                {
                    "type": "response_" + "item",
                    "timestamp": f"2026-01-01T00:00:{index * 2 + 3:02d}Z",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": f"ci-{index}",
                        "arguments": json.dumps(
                            {"cmd": "gh run view 123 --json databaseId,status,conclusion,createdAt"}
                        ),
                    },
                },
                {
                    "type": "response_" + "item",
                    "timestamp": f"2026-01-01T00:00:{index * 2 + 4:02d}Z",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": f"ci-{index}",
                        "output": json.dumps(
                            {
                                "databaseId": 123,
                                "status": status,
                                "conclusion": "success" if status == "success" else "",
                                "createdAt": "2026-01-01T00:00:00Z",
                                "private": surrounding,
                            }
                        ),
                    },
                },
            ]
        )
    bundle = extract(
        ("\n".join(json.dumps(row) for row in rows) + "\n").encode(),
        "codex",
        sanitize_output=False,
    )
    assert surrounding not in dumps(bundle["events"])
    assert bundle["run"]["extractor_version"] == "0.4.0"
    assert bundle["run"]["poll_definition_version"] == "2"
    assert bundle["run"]["facts"]["start_sha"]["value"] == sha
    assert bundle["run"]["facts"]["end_sha"]["value"] == sha
    assert bundle["run"]["facts"]["ci_poll_count"]["value"] == 1
    finishes = [event for event in bundle["events"] if event["event_type"] == "tool.finished"]
    assert finishes[0]["data"]["observed_sha"]["provenance"]["method"] == "git_command_output"
    assert finishes[-1]["data"]["ci_status"]["value"] == "success"
    assert finishes[-1]["data"]["ci_run_id"] == "123"


def test_structured_parser_rejects_ambiguous_sha():
    from harness_observatory.extractors import parse_tool_result

    result = parse_tool_result(
        {"name": "Bash", "args": {"command": "git rev-parse HEAD"}},
        ("a" * 40) + "\n" + ("b" * 40),
        "claude",
        "sha256:" + "c" * 64,
        1,
    )
    assert result == {}


def test_timezone():
    assert timestamp("2026-01-01T12:00:00+02:00") == "2026-01-01T10:00:00Z"
    with pytest.raises(ValueError):
        timestamp("2026-01-01T10:00:00")


def test_governance_hashing(history):
    repo, start, end = history
    a = governance_snapshot(repo, start, ["docs/VERIFY.md", "optional.md"])
    b = governance_snapshot(repo, start, ["optional.md", "docs/VERIFY.md"])
    assert a == b
    assert a["missing_optional"] == ["optional.md"]
    assert len(a["documents"]) == 3
    assert (
        a["governance_snapshot_id"]
        != governance_snapshot(repo, end, ["docs/VERIFY.md", "optional.md"])[
            "governance_snapshot_id"
        ]
    )
    assert (
        a["governance_snapshot_id"]
        == governance_snapshot(repo, start, ["docs/VERIFY.md", "optional.md"])[
            "governance_snapshot_id"
        ]
    )
    with pytest.raises(ValueError):
        governance_snapshot(repo, start, ["../escape"])


def test_governance_scope_claude_and_states(history, tmp_path):
    repo, start, _ = history
    nested = governance_snapshot(repo, start, scope_path="nested")
    applicability = {doc["path"]: doc["applicable"] for doc in nested["documents"]}
    assert applicability["AGENTS.md"] is True
    assert applicability["nested/AGENTS.md"] is True
    assert nested["state"] == "reconstructed"
    assert nested["governance_discovery_version"] == "2"

    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "-C", str(other), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "Synthetic"], check=True)
    subprocess.run(
        ["git", "-C", str(other), "config", "user.email", "test@example.invalid"], check=True
    )
    (other / "elsewhere").mkdir()
    (other / "elsewhere" / "CLAUDE.md").write_text("Synthetic Claude governance\n")
    subprocess.run(["git", "-C", str(other), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(other), "commit", "-m", "Synthetic governance"],
        check=True,
        capture_output=True,
    )
    sha = subprocess.check_output(["git", "-C", str(other), "rev-parse", "HEAD"]).decode().strip()
    snapshot = governance_snapshot(other, sha, scope_path="src")
    assert snapshot["state"] == "not_applicable"
    assert snapshot["documents"][0]["kind"] == "claude"
    assert snapshot["documents"][0]["applicable"] is False

    empty = tmp_path / "empty"
    empty.mkdir()
    subprocess.run(["git", "-C", str(empty), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(empty), "config", "user.name", "Synthetic"], check=True)
    subprocess.run(
        ["git", "-C", str(empty), "config", "user.email", "test@example.invalid"], check=True
    )
    (empty / "README.md").write_text("Synthetic\n")
    subprocess.run(["git", "-C", str(empty), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(empty), "commit", "-m", "Synthetic empty"],
        check=True,
        capture_output=True,
    )
    sha = subprocess.check_output(["git", "-C", str(empty), "rev-parse", "HEAD"]).decode().strip()
    assert governance_snapshot(empty, sha)["state"] == "none_existed"


def test_governance_uses_historical_tree_only(history):
    repo, start, _ = history
    (repo / "CLAUDE.md").write_text("Uncommitted future governance\n")
    snapshot = governance_snapshot(repo, start, scope_path=".")
    assert "CLAUDE.md" not in {doc["path"] for doc in snapshot["documents"]}


def test_governance_reconstruction_failed_without_sha(history):
    repo, _, _ = history
    bundle = enrich_git(sample(), repo)
    assert bundle["run"]["governance_state"] == "reconstruction_failed"
    assert bundle["governance_snapshots"] == []


def test_git_evidence_and_unknown(history):
    repo, start, end = history
    original = sample()
    assert enrich_git(original, repo)["run"]["governance_snapshot_id"] is None
    with pytest.raises(ValueError):
        enrich_git(original, repo, start, end)
    b = enrich_git(original, repo, start, end, "synthetic:endpoints", scope_path=".")
    assert original["git_changes"] == []
    assert b["run"]["facts"]["end_sha"]["provenance"]["evidence_refs"] == ["synthetic:endpoints"]
    assert sum(c["kind"] == "commit" for c in b["git_changes"]) == 1
    assert {c["path"] for c in b["git_changes"] if c["kind"] == "diff"} == {
        "AGENTS.md",
        "example.py",
    }


def test_secret_boundary():
    secrets = [
        "ghp_" + "a" * 36,
        "sk-" + "b" * 32,
        "github_pat_" + "x" * 40,
        "password=" + "synthetic-secret",
        ("-----BEGIN " + "PRIVATE KEY-----\nsynthetic\n-----END PRIVATE KEY-----"),
    ]
    for secret in secrets:
        assert secret not in dumps(sanitize({"nested": [secret]}))
        raw = json.dumps(
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"content": "ANY_ENV_VALUE=arbitrary\n" + secret},
            }
        ).encode()
        safe = dumps(extract(raw, "claude"))
        assert secret not in safe and "ANY_ENV_VALUE" not in safe


def test_actions_and_warehouse_roundtrip(history, tmp_path):
    repo, start, end = history
    bundles = []
    for provider in ["codex", "claude"]:
        b = enrich_git(sample(provider), repo, start, end, "synthetic:endpoints", scope_path=".")
        b = enrich_actions(
            b,
            [
                dict(
                    id=123,
                    head_sha=end,
                    created_at="2026-01-01T10:00:00Z",
                    run_started_at="2026-01-01T10:00:01Z",
                    updated_at="2026-01-01T10:00:03Z",
                    conclusion="success",
                    status="completed",
                ),
                dict(id=999, head_sha="f" * 40),
            ],
            "synthetic/example",
        )
        assert len(b["ci_runs"]) == 1
        assert b["ci_runs"][0]["poll_count"] == 0
        assert b["ci_runs"][0]["completed_at"] is None
        bundles.append(b)
    dbpath = build_warehouse(bundles + [bundles[0]], tmp_path / "warehouse")
    with duckdb.connect(str(dbpath), read_only=True) as db:
        result = db.execute(
            "SELECT agent, governance_snapshot_id, count(*), avg(ci_poll_count) FROM runs GROUP BY 1,2 ORDER BY 1"
        ).fetchall()
        assert [(r[0], r[2], r[3]) for r in result] == [("claude", 1, 0.0), ("codex", 1, 0.0)]
        assert result[0][1] == result[1][1]
        assert (
            db.execute(
                "SELECT count(*) FROM read_parquet(?)", [str(dbpath.parent / "events.parquet")]
            ).fetchone()[0]
            == 13
        )
        saved = json.loads(db.execute("SELECT manifest FROM runs LIMIT 1").fetchone()[0])
        assert saved["facts"]["end_sha"]["provenance"]["confidence"] == 0.8
    with pytest.raises(FileExistsError):
        build_warehouse(bundles, dbpath.parent)


def test_cli_safe_output(tmp_path, monkeypatch):
    from harness_observatory.data_root import init_data_root

    tmp_path = init_data_root(tmp_path / "corpus").path
    monkeypatch.setenv("HARNESS_OBSERVATORY_DATA_ROOT", str(tmp_path))
    with pytest.raises(ValueError):
        write_bundle(Path(__file__).resolve().parents[1] / "private" / "unsafe.json", sample())
    output = tmp_path / "private" / "sample.json"
    main(["extract", "codex", str(FIXTURES / "codex.jsonl"), "--output", str(output)])
    assert json.loads(output.read_text()) == sample()
    with pytest.raises(FileExistsError):
        write_bundle(output, sample())


def test_malformed_record_is_atomic():
    raw = json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "content": [
                    {"type": "text", "text": "synthetic"},
                    {"type": "tool_use", "id": "x", "name": "Read", "input": {"file_path": 42}},
                ]
            },
        }
    ).encode()
    b = extract(raw, "claude")
    assert b["events"] == []
    assert b["warnings"][-1]["code"] == "malformed_record"


def test_no_poll_from_quoted_example():
    raw = (
        (FIXTURES / "claude.jsonl").read_text().replace("gh run view 123", "echo 'gh run view 123'")
    )
    assert extract(raw.encode(), "claude")["run"]["facts"]["ci_poll_count"]["value"] == 0


def test_recorded_start_provenance_survives_end_override(history):
    repo, start, end = history
    b = sample()
    b["run"]["facts"]["start_sha"]["value"] = start
    original = b["run"]["facts"]["start_sha"]["provenance"]["evidence_refs"]
    enriched = enrich_git(b, repo, end_sha=end, evidence="synthetic:end-only", scope_path=".")
    assert enriched["run"]["facts"]["start_sha"]["provenance"]["evidence_refs"] == original
    assert enriched["run"]["facts"]["repository"]["value"] == str(repo)


def test_same_governance_across_unrelated_commits(history):
    repo, _, end = history
    before = governance_snapshot(repo, end)
    (repo / "unrelated.txt").write_text("synthetic unrelated change\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "Synthetic unrelated commit"],
        check=True,
        capture_output=True,
    )
    newer = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip()
    after = governance_snapshot(repo, newer)
    assert before["repo_sha"] != after["repo_sha"]
    assert before["governance_snapshot_id"] == after["governance_snapshot_id"]


def codex_rows(*steps):
    rows = [
        {
            "type": "session_" + "meta",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"id": "synthetic"},
        }
    ]
    for index, (kind, value) in enumerate(steps):
        ts = f"2026-01-01T00:00:{index + 1:02d}Z"
        if kind == "call":
            payload = {
                "type": "function_call",
                "name": "exec_command",
                "call_id": f"c{index}",
                "arguments": json.dumps({"cmd": value}),
            }
        elif kind == "result":
            payload = {"type": "function_call_output", "call_id": f"c{index - 1}", "output": value}
        else:
            payload = {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": value}],
            }
        rows.append({"type": "response_" + "item", "timestamp": ts, "payload": payload})
    raw = "\n".join(json.dumps(row) for row in rows) + "\n"
    return extract(raw.encode(), "codex")


def kinds(bundle, prefix):
    return [e for e in bundle["events"] if e["event_type"].startswith(prefix)]


def test_incident_watch_is_a_polling_violation_not_a_compliant_query():
    bundle = codex_rows(("call", "gh pr checks 176 --watch"), ("result", "All checks passed"))
    (violation,) = kinds(bundle, "execution.polling_violation")
    assert violation["data"]["violation"] == "TOOL_WATCH_MODE"
    assert violation["data"]["status"] == "executed"
    assert violation["data"]["classifier"] == "command-policy-v1"
    assert len(kinds(bundle, "ci.pr_check_query")) == 1  # legacy taxonomy kept
    assert not kinds(bundle, "ci.single_status_query")


def test_blocked_attempt_one_shot_repeat_and_notification_are_distinguished():
    blocked = codex_rows(
        ("call", "gh run watch 12345"),
        ("result", "NO_AUTONOMOUS_POLLING (TOOL_WATCH_MODE): blocked"),
    )
    assert kinds(blocked, "execution.blocked_attempt")
    assert not kinds(blocked, "execution.polling_violation")
    once = codex_rows(("call", "./gov ci-status 176"), ("result", "pending"))
    assert len(kinds(once, "ci.single_status_query")) == 1
    assert not kinds(once, "execution.")
    twice = codex_rows(
        ("call", "gh pr checks 176"),
        ("result", "pending"),
        ("call", "gh pr checks 176"),
        ("result", "pending"),
    )
    (repeat,) = kinds(twice, "execution.polling_violation")
    assert repeat["data"]["violation"] == "REPEATED_STATUS_QUERY"
    resumed = codex_rows(
        ("call", "gh pr checks 176"),
        ("result", "pending"),
        ("user", "<ci-monitor-event>checks finished</ci-monitor-event>"),
        ("call", "gh pr checks 176"),
        ("result", "pass"),
    )
    assert kinds(resumed, "execution.external_notification")
    assert not kinds(resumed, "execution.polling_violation")
    assert len(kinds(resumed, "ci.pr_check_query")) == 2


def test_monitor_polling_is_classified_without_changing_command_count():
    rows = [
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "sessionId": "s",
            "message": {"role": "user", "content": "go"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:01Z",
            "sessionId": "s",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "m1",
                        "name": "Monitor",
                        "input": {"command": "while true; do gh pr checks 1; sleep 30; done"},
                    }
                ],
            },
        },
    ]
    bundle = extract(("\n".join(json.dumps(r) for r in rows) + "\n").encode(), "claude")
    (violation,) = kinds(bundle, "execution.polling_violation")
    assert violation["data"]["violation"] == "POLLING_LOOP"
    assert bundle["run"]["facts"]["command_count"]["value"] == 0


def test_missing_classifier_is_a_visible_coverage_gap(monkeypatch, tmp_path):
    from harness_observatory import polling

    monkeypatch.setenv("HARNESS_COMMAND_POLICY", str(tmp_path / "absent.py"))
    polling.policy.cache_clear()
    try:
        bundle = codex_rows(("call", "gh pr checks 176 --watch"))
    finally:
        polling.policy.cache_clear()
    assert "polling_classifier_unavailable" in {w["code"] for w in bundle["warnings"]}
    assert not kinds(bundle, "ci.single_status_query")
