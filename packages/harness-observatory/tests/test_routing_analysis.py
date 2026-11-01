"""Synthetic routing-analysis regression fixtures; no private evidence."""

import importlib.util
import unittest
from pathlib import Path

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
