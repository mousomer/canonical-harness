"""Synthetic routing-analysis regression fixtures; no private evidence."""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from harness_observatory.data_root import init_data_root

SPEC = importlib.util.spec_from_file_location(
    "routing_analysis", Path(__file__).parents[1] / "scripts" / "routing_analysis.py"
)
if not SPEC.origin or not Path(SPEC.origin).exists():
    SPEC = importlib.util.spec_from_file_location(
        "routing_analysis", Path(__file__).with_name("routing_analysis.py")
    )
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class RoutingAnalysisTests(unittest.TestCase):
    def test_empty_selections_reject_before_creating_private_outputs(self):
        for has_unselected_source in (False, True):
            with self.subTest(has_unselected_source=has_unselected_source):
                with tempfile.TemporaryDirectory() as directory:
                    temporary = Path(directory)
                    data_root = init_data_root(temporary / "data")
                    sources = temporary / "sources"
                    sources.mkdir()
                    if has_unselected_source:
                        record = dict(
                            type="session_meta",
                            payload={
                                "id": "synthetic-unselected-session",
                                "cwd": str(temporary / "unselected-project"),
                            },
                        )
                        (sources / "rollout-synthetic.jsonl").write_text(json.dumps(record) + "\n")
                    source_bytes = {path.name: path.read_bytes() for path in sources.iterdir()}
                    root_paths = {
                        str(path.relative_to(data_root.path)) for path in data_root.path.rglob("*")
                    }
                    arguments = [
                        "routing_analysis.py",
                        "--data-root",
                        str(data_root.path),
                        "--repo",
                        str(temporary),
                        "--source-root",
                        str(sources),
                        "--cwd",
                        str(temporary / "selected-project"),
                        "--output",
                        "analysis/new-parent/synthetic-report",
                    ]
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with (
                        patch.object(sys, "argv", arguments),
                        patch.object(module.sqlite3, "connect") as connect,
                        patch.object(module.DataRoot, "preserve_raw") as preserve_raw,
                        redirect_stdout(stdout),
                        redirect_stderr(stderr),
                        self.assertRaises(SystemExit) as error,
                    ):
                        module.main()
                    self.assertEqual(error.exception.code, 2)
                    self.assertEqual(stderr.getvalue(), "routing-analysis: no_matching_inputs\n")
                    self.assertEqual(stdout.getvalue(), "")
                    connect.assert_not_called()
                    preserve_raw.assert_not_called()
                    self.assertFalse((data_root.path / "analysis/new-parent").exists())
                    self.assertEqual(
                        {
                            str(path.relative_to(data_root.path))
                            for path in data_root.path.rglob("*")
                        },
                        root_paths,
                    )
                    self.assertEqual(
                        {path.name: path.read_bytes() for path in sources.iterdir()}, source_bytes
                    )

    def test_report_uses_only_explicit_synthetic_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            data_root = init_data_root(temporary / "data")
            sources = temporary / "sources"
            sources.mkdir()
            selected = [str(temporary / "project-one"), str(temporary / "project-two")]
            home = temporary / "synthetic-home"
            # This invented path exercises the former named-project fallback.
            unselected = str(home / ".codex" / "worktrees" / "synthetic" / "tet4d")
            accepted_bytes = 0
            for index, cwd in enumerate([*selected, unselected]):
                record = dict(
                    type="session_meta",
                    payload={
                        "id": f"synthetic-session-{index}",
                        "cwd": cwd,
                        "timestamp": "2000-01-01T00:00:00Z",
                    },
                )
                raw = (json.dumps(record) + "\n").encode()
                (sources / f"rollout-synthetic-{index}.jsonl").write_bytes(raw)
                if cwd in selected:
                    accepted_bytes += len(raw)
            arguments = [
                "routing_analysis.py",
                "--data-root",
                str(data_root.path),
                "--repo",
                str(temporary),
                "--source-root",
                str(sources),
                "--output",
                "analysis/synthetic-report",
            ]
            for cwd in selected:
                arguments.extend(["--cwd", cwd])
            with (
                patch.object(sys, "argv", arguments),
                patch.object(module.Path, "home", return_value=home),
                redirect_stdout(io.StringIO()),
            ):
                module.main()
            report = json.loads(
                (data_root.path / "analysis/synthetic-report/corpus_inventory.json").read_text()
            )
            self.assertEqual(report["session_count"], len(selected))
            self.assertEqual(report["bytes"], accepted_bytes)
            self.assertEqual({row["metadata"]["cwd"] for row in report["sources"]}, set(selected))
            self.assertEqual(
                set(report),
                {
                    "parser_version",
                    "session_count",
                    "bytes",
                    "gib",
                    "counts",
                    "logical_calls",
                    "formats",
                    "sources",
                    "current_rds_files_inventory_only",
                    "era_states",
                    "source_kinds",
                    "date_range",
                    "status",
                },
            )

    def test_operation_golden_cases(self):
        for command, expected in [
            ("cat docs/POLICY.md", "WHOLE_READ"),
            ("sed -n '40,90p' docs/POLICY.md", "BOUNDED_READ"),
            ("rg pattern docs/POLICY.md", "SEARCH"),
            ("git diff -- docs/POLICY.md", "GIT_INSPECT"),
            ("./gov explain sample", "GOV_QUERY"),
            ("python scripts/validate_config.py docs/POLICY.md", "VERIFY"),
            ("python -m pytest tests/test_config.py", "VERIFY"),
            ("python scripts/generate_index.py", "GENERATED_REFRESH"),
            ("cmake --build build", "RUNTIME_OR_BUILD"),
            ("pwd", "METADATA"),
            ("printf hello", "OTHER"),
            ("opaque-tool docs/POLICY.md", "UNKNOWN"),
        ]:
            with self.subTest(command=command):
                self.assertEqual(module.classify(command)[0]["operation_class"], expected)

    def test_patch_is_edit(self):
        row = module.classify(
            "*** Begin Patch\n*** Update File: docs/POLICY.md\n*** End Patch", "apply_patch"
        )[0]
        self.assertEqual(row["operation_class"], "EDIT")
        self.assertEqual(row["paths_referenced"], ["docs/POLICY.md"])

    def test_mixed_shell_not_one_whole_read(self):
        self.assertEqual(
            [r["operation_class"] for r in module.classify("cat a.md && rg needle b.md")],
            ["WHOLE_READ", "SEARCH"],
        )

    def test_heredoc_not_parsed_as_commands(self):
        self.assertEqual(
            module.classify("python - <<'PY'\ncat a.md\nPY")[0]["operation_class"], "UNKNOWN"
        )

    def test_wrapper_has_one_action_not_two(self):
        source = 'const r = await tools.exec_command({cmd:"cat docs/POLICY.md",yield_time_ms:1000}); text(r);'
        sites = module.nested_calls(source)
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0]["arguments"]["cmd"], "cat docs/POLICY.md")
        self.assertEqual(sites[0]["tool_name"], "exec_command")

    def test_quoted_source_is_not_call(self):
        self.assertEqual(
            module.nested_calls('text("tools.exec_command({})"); // tools.exec_command({})'), []
        )

    def test_dynamic_template_unknown(self):
        self.assertIsNone(
            module.nested_calls("await tools.exec_command({cmd:`cat ${name}`})")[0]["arguments"]
        )

    def test_dynamic_concatenation_unknown(self):
        self.assertIsNone(
            module.nested_calls('await tools.exec_command({cmd:"cat " + name})')[0]["arguments"]
        )

    def test_loop_not_single_executed_action(self):
        self.assertEqual(
            module.nested_calls('for (const x of xs) { await tools.exec_command({cmd:"pwd"}); }')[
                0
            ]["execution_relation"],
            "conditional_or_loop",
        )

    def test_multisite_preserved(self):
        self.assertEqual(
            len(
                module.nested_calls(
                    'await Promise.all([tools.exec_command({cmd:"pwd"}),tools.exec_command({cmd:"ls"})]);'
                )
            ),
            2,
        )

    def test_retained_not_original(self):
        text = "Chunk ID: synthetic\nOriginal token count: 10000\nOutput:\nshort"
        row = module.output_metrics(text)
        self.assertEqual(row["original_tokens"], 10000)
        self.assertEqual(row["retained_bytes"], len(text.encode()))
        self.assertIsNone(row["retained_tokens"])

    def test_body_count_not_metadata(self):
        row = module.output_metrics("Chunk ID: synthetic\nOutput:\nOriginal token count: 10000")
        self.assertIsNone(row["original_tokens"])

    def test_structured_result(self):
        row = module.output_metrics('{"original_token_count":999,"output":"short"}')
        self.assertEqual(row["original_tokens"], 999)

    def test_truncation(self):
        row = module.output_metrics("Warning: truncated output (original token count: 900)\nshort")
        self.assertTrue(row["truncated"])
        self.assertEqual(row["original_tokens"], 900)

    def test_unicode_and_nontext(self):
        row = module.output_metrics(
            [{"type": "input_text", "text": "é"}, {"type": "image", "data": "blob"}]
        )
        self.assertEqual(row["retained_bytes"], 2)
        self.assertEqual(row["retained_chars"], 1)


if __name__ == "__main__":
    unittest.main()
