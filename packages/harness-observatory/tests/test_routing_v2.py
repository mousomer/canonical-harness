"""Synthetic and adversarial fixtures; deliberately no real rollout content."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from routing_v2_core import classify, decode_output, parse_wrapper, source_output_owner  # noqa: E402


@pytest.mark.parametrize(
    "command,expected",
    [
        ("cat a.md", "WHOLE_READ"),
        ("cat", "OTHER_OR_UNKNOWN"),
        ("cat -", "OTHER_OR_UNKNOWN"),
        ("cat > a.md", "OTHER_OR_UNKNOWN"),
        ("cat a.md | head -2", "OTHER_OR_UNKNOWN"),
        ("echo 'cat a.md'", "OTHER_OR_UNKNOWN"),
        ("printf '%s' a.md", "OTHER_OR_UNKNOWN"),
        ("# cat a.md\npwd", "METADATA"),
        ("nl -ba scripts/generate_index.py", "METADATA"),
        ("python scripts/generate_index.py", "GENERATED_REFRESH"),
        ("python scripts/validate_config.py a.md", "VERIFY"),
        ("cat scripts/validate_config.py", "WHOLE_READ"),
        ("./scripts/verify.sh", "VERIFY"),
        ("python -m pytest tests/test_a.py", "VERIFY"),
        ("sed -n '20,40p' a.md", "BOUNDED_READ"),
        ("sed -i 's/a/b/' a.md", "OTHER_OR_UNKNOWN"),
        ("rg foo a.md", "SEARCH"),
        ("git diff -- a.md", "GIT_INSPECT"),
        ("git show HEAD:a.md", "GIT_INSPECT"),
        ("./gov explain sample", "GOV_QUERY"),
        ("./gov sync", "OTHER_OR_UNKNOWN"),
        ("cmake --build build", "RUNTIME_OR_BUILD"),
        ("python - <<'PY'\nprint('cat a.md')\nPY", "RUNTIME_OR_BUILD"),
        ("cat $(echo a.md)", "OTHER_OR_UNKNOWN"),
    ],
)
def test_classification(command, expected):
    assert classify(command)[0]["class"] == expected


def test_compound_surfaces():
    rows = classify("cat a.md && rg 'b.md' c.md; git diff -- d.md")
    assert [r["class"] for r in rows] == ["WHOLE_READ", "SEARCH", "GIT_INSPECT"]
    assert rows[1]["paths"] == ["c.md"]
    assert rows[2]["paths"] == ["d.md"]


def test_search_glob_is_not_read_path():
    assert classify("rg --files -g a.md")[0]["paths"] == []


def test_redirect_not_model_materialization():
    row = classify("cat a.md > out.md")[0]
    assert row["paths"] == ["a.md"]
    assert row["redirected_writes"] == ["out.md"]
    assert not row["materialization_to_tool_output"]


def test_patch():
    assert (
        classify("*** Begin Patch\n*** Update File: a.md\n*** End Patch", "apply_patch")[0]["class"]
        == "EDIT"
    )


def test_constant_patch_array_and_single_identity():
    parsed = parse_wrapper(
        'const patch="*** Begin Patch\\n*** Update File: a.md\\n*** End Patch";const r=await tools.apply_patch(patch);text(r);'
    )
    assert len(parsed["sites"]) == 1
    assert parsed["sites"][0]["arguments"].startswith("*** Begin Patch")
    parsed = parse_wrapper(
        'const r=await tools.exec_command({cmd:"pwd",prefix_rule:["pwd"]});text(r)'
    )
    assert parsed["reason"] == "resolvable_nested_invocation"


def test_multi_and_symbolic_array():
    parsed = parse_wrapper(
        'const rs=await Promise.all([tools.exec_command({cmd:"pwd"}),tools.exec_command({cmd:"ls"})]);text(rs)'
    )
    assert len(parsed["sites"]) == 2
    assert parsed["emissions"][0]["value"] == [{"toolref": 0}, {"toolref": 1}]


def test_literal_map_calls():
    parsed = parse_wrapper(
        'const files=["a.md","b.md"];const r=await Promise.all(files.map(f=>tools.exec_command({cmd:"cat "+f})));text(r)'
    )
    assert [s["arguments"]["cmd"] for s in parsed["sites"]] == ["cat a.md", "cat b.md"]


def test_quoted_call_and_unused_function_not_execution():
    parsed = parse_wrapper(
        'text("tools.exec_command({cmd:\\"cat a.md\\"})");function unused(){return tools.exec_command({cmd:"pwd"})}'
    )
    assert not parsed["sites"]


def test_conditional_is_not_certain():
    parsed = parse_wrapper('if (unknown) { await tools.exec_command({cmd:"pwd"}); }')
    assert parsed["sites"][0]["conditional"]


def test_truncation_body_false_positive():
    rows = decode_output(
        "Chunk ID: sample\nWall time: 1 seconds\nProcess exited with code 0\nOutput:\nOriginal token count: 123\nexample"
    )
    assert rows[0]["original_tokens"] is None
    assert rows[0]["body"].startswith("Original token count:")


def test_original_and_retained_separate():
    row = decode_output("Chunk ID: sample\nOriginal token count: 100000\nOutput:\né")[0]
    assert row["original_tokens"] == 100000
    assert row["body_bytes"] == 2


def test_warning_and_serialization():
    row = decode_output(
        '{"output":"Warning: truncated output (original token count: 100)\\nshort","wall_time_seconds":1}'
    )[0]
    assert row["truncated"] and row["original_tokens"] == 100
    assert row["body"] == "short"
    assert row["raw_bytes"] > row["body_bytes"]


def test_async_handle():
    row = decode_output("Script running with cell ID 42\nWall time 10 seconds\nOutput:\n")[0]
    assert row["cell_id"] == "42"


def test_audit_metrics_do_not_certify_sparse_samples():
    from routing_v2_audit import metrics, wilson

    interval = wilson(12, 12)
    assert interval[0] < 0.95
    assert wilson(0, 0) is None
    evidence = {
        "operations": [
            {
                "operation_id": "one",
                "sampling": "hash_priority_predicted_class",
                "predicted": "VERIFY",
            }
        ]
    }
    result = metrics(
        evidence, {"one": {"gold": "GENERATED_REFRESH"}}, {"VERIFY": 100, "GENERATED_REFRESH": 100}
    )
    assert result["classes"]["VERIFY"]["sample_precision"] == 0


def test_legacy_output_envelopes():
    row = decode_output("Exit code: 0\nWall time: 0.1 seconds\nOutput:\nshort")[0]
    assert row["body"] == "short"
    row = decode_output('{"output":"patched","metadata":{"exit_code":0}}')[0]
    assert row["body"] == "patched"


def test_historical_reconciliation_rule_is_first_match_and_not_certification():
    from routing_v2_reconcile import historical_output_measurement

    text, count, truncated, recognized = historical_output_measurement(
        {
            "output": "Warning: truncated output (original token count: 9); nested original_token_count: 12"
        }
    )
    assert '"output"' in text
    assert count == 9
    assert truncated
    assert recognized


def test_unrelated_print_not_leaf_output():
    parsed = parse_wrapper('const r=await tools.exec_command({cmd:"pwd"});text("unrelated a.md")')
    call = {**parsed, "wrapper": True, "sites": [{**s, "id": "leaf"} for s in parsed["sites"]]}
    parts = decode_output("unrelated a.md")
    assert source_output_owner(call, parts[0], parts)[0] is None


def test_unknown_wrapper_preserved():
    parsed = parse_wrapper("const r=await tools.exec_command(dynamic);text(r)")
    assert parsed["reason"] == "dynamic_arguments"
    assert len(parsed["sites"]) == 1


def test_git_printed_filename_not_operand():
    row = classify("printf 'git diff -- a.md'")[0]
    assert row["paths"] == []
    assert row["class"] == "OTHER_OR_UNKNOWN"


def test_graph_replay_synthetic(tmp_path):
    import argparse
    import hashlib
    import json
    import sqlite3
    from harness_observatory.data_root import init_data_root
    from routing_v2 import run

    root = init_data_root(tmp_path / "private")
    baseline = root.destination("analysis/results/baseline")
    baseline.mkdir()
    db = sqlite3.connect(baseline / "routing.sqlite")
    db.execute("create table logical_calls(id text,data text)")
    db.close()
    meta = {
        "id": "synthetic-session",
        "cwd": str(tmp_path),
        "source": "cli",
        "cli_version": "fixture",
    }
    events = [dict(type="session_meta", payload=meta)]

    def event(payload):
        events.append(dict(type="response_item", timestamp="2020-01-01T00:00:00Z", payload=payload))

    event(
        {
            "type": "custom_tool_call",
            "name": "exec",
            "call_id": "async",
            "input": 'const r=await tools.exec_command({cmd:"sleep 1"});text(r)',
        }
    )
    event(
        {
            "type": "custom_tool_call_output",
            "call_id": "async",
            "output": "Script running with cell ID 7\nWall time 1 seconds\nOutput:\n",
        }
    )
    event(
        {
            "type": "function_call",
            "name": "wait",
            "call_id": "wait",
            "arguments": json.dumps({"cell_id": "7"}),
        }
    )
    event({"type": "function_call_output", "call_id": "wait", "output": "finished"})
    event(
        {
            "type": "custom_tool_call",
            "name": "exec",
            "call_id": "multi",
            "input": 'const r=await Promise.all([tools.exec_command({cmd:"cat a.md"}),tools.exec_command({cmd:"cat b.md"})]);text(r)',
        }
    )
    event(
        {
            "type": "custom_tool_call_output",
            "call_id": "multi",
            "output": json.dumps(
                [{"output": "a", "exit_code": 0}, {"output": "b", "exit_code": 0}]
            ),
        }
    )
    event(
        {
            "type": "custom_tool_call",
            "name": "exec",
            "call_id": "unknown",
            "input": 'tools.exec_command(dynamic);text("constant")',
        }
    )
    event({"type": "custom_tool_call_output", "call_id": "unknown", "output": "constant"})
    raw = ("\n".join(json.dumps(e) for e in events) + "\n").encode()
    snapshot = root.preserve_raw("codex", raw)
    entries = [
        {
            "source_identity": "synthetic",
            "snapshot": str(snapshot),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "session_metadata": meta,
        }
    ]
    identity = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    inventory = root.destination("analysis/results/inventory.json")
    inventory.write_text(
        json.dumps(
            {"inventory_id": identity, "sources": entries, "baseline_checkpoint": str(baseline)}
        )
    )
    run(
        argparse.Namespace(
            data_root=str(root.path),
            inventory=str(inventory),
            output="analysis/results/replay",
            repo=str(tmp_path),
        )
    )
    db = sqlite3.connect(root.destination("analysis/results/replay/measurement.sqlite"))
    outputs = [json.loads(r[0]) for r in db.execute("select data from outputs")]
    assert (
        next(o for o in outputs if o["measured"]["body"] == "finished")["inferred"]["action_id"]
        == "async:site:0"
    )
    assert (
        next(o for o in outputs if o["measured"]["body"] == "a")["inferred"]["action_id"]
        == "multi:site:0"
    )
    assert (
        next(o for o in outputs if o["measured"]["body"] == "b")["inferred"]["action_id"]
        == "multi:site:1"
    )
    assert (
        next(o for o in outputs if o["measured"]["body"] == "constant")["inferred"]["action_id"]
        is None
    )
    edges = [json.loads(r[0]) for r in db.execute("select data from logical_action_edges")]
    assert any(
        e["relation"] == "continues_observed_handle" and e["target"] == "async:site:0"
        for e in edges
    )
    assert db.execute("select count(*) from logical_actions where id='multi'").fetchone()[0] == 1
    assert (
        db.execute("select count(*) from logical_actions where id like 'multi:site:%'").fetchone()[
            0
        ]
        == 2
    )
    assert any(o["inferred"]["action_id"] is None for o in outputs)
