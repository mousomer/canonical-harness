"""Reject the known corpus-comparison literal shape without retaining real values."""

import ast
from pathlib import Path


def test_production_sources_have_no_literal_corpus_comparison_tables():
    component = Path(__file__).resolve().parents[1] / "packages/harness-observatory"
    metric_names = {"sessions", "raw_calls", "bytes"}
    findings = []
    for directory in ("scripts", "src"):
        for path in sorted((component / directory).rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=path.name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                literal_metrics = {
                    key.value
                    for key, value in zip(node.keys, node.values)
                    if isinstance(key, ast.Constant)
                    and key.value in metric_names
                    and isinstance(value, ast.Constant)
                    and type(value.value) in {int, float}
                }
                if len(literal_metrics) >= 2:
                    findings.append(f"{path.relative_to(component)}:{node.lineno}")
    assert not findings, f"Embedded corpus comparison literals: {findings}"
