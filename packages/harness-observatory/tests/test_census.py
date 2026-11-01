import json

from harness_observatory.census import run_census
from harness_observatory.data_root import init_data_root
from harness_observatory.model import EXTRACTOR_VERSION

FIXTURES = None  # Per-test temporary provider inputs from conftest.py.


def sources(tmp_path, count=2):
    result = {}
    for provider in ["codex", "claude"]:
        root = tmp_path / provider
        root.mkdir()
        for index in range(count):
            raw = (
                (FIXTURES / f"{provider}.jsonl")
                .read_text()
                .replace(f"synthetic-{provider}", f"synthetic-{provider}-{index}")
            )
            (root / f"{index}.jsonl").write_text(raw)
        result[provider] = root
    return result


def test_census_complete_and_restartable(tmp_path):
    root = init_data_root(tmp_path / "corpus")
    inputs = sources(tmp_path)
    first = run_census(
        root, inputs, census="synthetic-census", deadline_seconds=30, checkpoint_every=2
    )
    assert first["status"] == "complete"
    assert first["stats"]["normalized_written"] == 4
    second = run_census(
        root, inputs, census="synthetic-resume", deadline_seconds=30, checkpoint_every=2
    )
    assert second["status"] == "complete"
    assert second["stats"]["normalized_skipped"] == 4
    assert second["stats"]["enriched_skipped"] == 4
    final = sorted((root.path / "logs" / "synthetic-resume").glob("checkpoint-*.json"))[-1]
    assert json.loads(final.read_text())["status"] == "complete"


def test_census_deadline_checkpoints_without_partial_bundle(tmp_path):
    root = init_data_root(tmp_path / "corpus")
    result = run_census(root, sources(tmp_path, 1), census="synthetic-timeout", deadline_seconds=0)
    assert result["status"] == "timeout"
    checkpoint = next((root.path / "logs" / "synthetic-timeout").glob("checkpoint-*.json"))
    assert json.loads(checkpoint.read_text())["status"] == "timeout"
    assert not list((root.path / "normalized" / "runs" / "0.2" / EXTRACTOR_VERSION).glob("*.json"))


def test_census_resume_uses_new_checkpoint_numbers(tmp_path):
    root = init_data_root(tmp_path / "corpus")
    inputs = sources(tmp_path, 2)
    first = run_census(
        root, inputs, census="synthetic-checkpoints", deadline_seconds=30, checkpoint_every=2
    )
    assert first["status"] == "complete"
    before = {
        path.name
        for path in (root.path / "logs" / "synthetic-checkpoints").glob("checkpoint-*.json")
    }
    second = run_census(
        root, inputs, census="synthetic-checkpoints", deadline_seconds=30, checkpoint_every=2
    )
    assert second["status"] == "complete"
    after = {
        path.name
        for path in (root.path / "logs" / "synthetic-checkpoints").glob("checkpoint-*.json")
    }
    assert before < after
