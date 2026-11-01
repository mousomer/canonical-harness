"""Private storage is verified entirely with invented data and temporary Git repositories."""

import json
import subprocess
from pathlib import Path

import duckdb
import pytest

from harness_observatory.cli import main
from harness_observatory.data_root import (
    DataRoot,
    ENVIRONMENT_KEY,
    LAYOUT,
    MARKER,
    OutputBoundaryError,
    init_data_root,
    outside_git,
    resolve_data_root,
    _git_probe,
)

FIXTURES = None  # Per-test temporary provider inputs from conftest.py.


def git(repo, *args):
    return (
        subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)
        .decode()
        .strip()
    )


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Synthetic")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "AGENTS.md").write_text("Synthetic verification policy\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Synthetic root")
    return repo


def test_reject_all_git_roots_and_aliases(history, tmp_path, monkeypatch):
    worktree = tmp_path / "linked"
    git(history, "worktree", "add", "-b", "synthetic-linked", str(worktree))
    alias = tmp_path / "alias"
    alias.symlink_to(history, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    for target in [
        history,
        history / "private",
        worktree,
        worktree / "missing" / "data",
        alias / "data",
        Path("missing/../repo/data"),
    ]:
        with pytest.raises(OutputBoundaryError, match="data_inside_git"):
            init_data_root(target)
    assert not (history / "private").exists()
    assert git(history, "status", "--short") == ""
    assert git(worktree, "status", "--short") == ""


def test_git_environment_cannot_override_discovery(history, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(history))
    with pytest.raises(OutputBoundaryError):
        init_data_root(history / "data")
    assert init_data_root(tmp_path / "external").id.startswith("corpus-")


def test_git_boundary_probe_is_cached_and_invalidated(tmp_path):
    directory = tmp_path / "external"
    directory.mkdir()
    _git_probe.cache_clear()
    outside_git(directory / "one")
    first = _git_probe.cache_info()
    outside_git(directory / "two")
    second = _git_probe.cache_info()
    assert second.hits == first.hits + 1
    git(directory, "init")
    with pytest.raises(OutputBoundaryError, match="data_inside_git"):
        outside_git(directory / "three")


def test_initialization_and_resolution(tmp_path, monkeypatch):
    monkeypatch.delenv(ENVIRONMENT_KEY, raising=False)
    with pytest.raises(OutputBoundaryError, match="data_root_required"):
        resolve_data_root()
    root = init_data_root(tmp_path / "nested" / "external" / "corpus")
    assert all((root.path / d).is_dir() for d in LAYOUT)
    assert not (root.path / ".git").exists()
    assert root.path.stat().st_mode & 0o777 == 0o700
    assert (root.path / MARKER).stat().st_mode & 0o777 == 0o600
    assert init_data_root(root.path).id == root.id
    another = init_data_root(tmp_path / "another")
    monkeypatch.setenv(ENVIRONMENT_KEY, str(root.path))
    assert resolve_data_root().id == root.id
    assert resolve_data_root(str(another.path)).id == another.id
    with pytest.raises(OutputBoundaryError, match="not_initialized"):
        resolve_data_root(tmp_path / "uninitialized")


def test_unsafe_root_locations(tmp_path):
    with pytest.raises(OutputBoundaryError):
        init_data_root(Path.home())
    with pytest.raises(OutputBoundaryError):
        init_data_root(Path(Path.cwd().anchor))
    nonempty = tmp_path / "occupied"
    nonempty.mkdir()
    (nonempty / "precious.txt").write_text("synthetic")
    with pytest.raises(OutputBoundaryError, match="not_empty"):
        init_data_root(nonempty)
    bare = tmp_path / "bare"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    with pytest.raises(OutputBoundaryError):
        init_data_root(bare / "data")


def test_outputs_cannot_escape_or_enter_nested_git(tmp_path):
    root = init_data_root(tmp_path / "corpus")
    with pytest.raises(OutputBoundaryError, match="outside_data_root"):
        root.write("../escape.json", {})
    alias = root.path / "escape"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OutputBoundaryError):
        root.write("escape/out.json", {})
    nested = root.path / "nested"
    nested.mkdir()
    git(nested, "init")
    with pytest.raises(OutputBoundaryError):
        root.write("nested/out.json", {})
    # Root validity is re-evaluated even after initialization.
    git(root.path, "init")
    with pytest.raises(OutputBoundaryError):
        DataRoot(root.path)


def test_case_aliases_on_case_insensitive_filesystem(history, tmp_path):
    alias = history.with_name(history.name.upper())
    if not alias.exists():
        pytest.skip("Filesystem is case-sensitive")
    assert alias.samefile(history)
    with pytest.raises(OutputBoundaryError):
        outside_git(alias / "missing" / "data")
    root = init_data_root(tmp_path / "corpus")
    alternate = root.path.with_name(root.path.name.upper())
    assert root.destination(alternate / "normalized" / "future.json").parent.samefile(
        root.path / "normalized"
    )


def test_cli_workflow_is_scoped_private_and_read_only(history, tmp_path, monkeypatch):
    root = init_data_root(tmp_path / "corpus")
    monkeypatch.setenv(ENVIRONMENT_KEY, str(root.path))
    before = (FIXTURES / "codex.jsonl").read_bytes()
    main(["extract", "codex", str(FIXTURES / "codex.jsonl")])
    normalized = next((root.path / "normalized" / "runs").glob("*.json"))
    payload = json.loads(normalized.read_text())
    assert payload["run"]["facts"]["cwd"]["value"] == "synthetic-repo"
    sha = git(history, "rev-parse", "HEAD")
    main(
        [
            "enrich-git",
            str(normalized),
            "--repo",
            str(history),
            "--start-sha",
            sha,
            "--evidence",
            "synthetic:test-endpoint",
        ]
    )
    enriched = next((root.path / "enriched" / "git").glob("*.json"))
    actions = root.path / "raw" / "github" / "synthetic.json"
    actions.write_text(json.dumps([{"id": 123, "head_sha": sha, "status": "completed"}]))
    main(
        [
            "enrich-ci",
            str(enriched),
            "--repository",
            "synthetic/example",
            "--actions-json",
            str(actions),
        ]
    )
    ci = next((root.path / "enriched" / "ci").glob("*.json"))
    main(["build-warehouse", str(ci)])
    dbpath = next((root.path / "warehouse").glob("*/observatory.duckdb"))
    with duckdb.connect(str(dbpath), read_only=True) as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    assert len(list((root.path / "logs").glob("operation-*.json"))) == 4
    assert (FIXTURES / "codex.jsonl").read_bytes() == before
    assert git(history, "status", "--short") == ""
    with pytest.raises(SystemExit):
        main(
            [
                "extract",
                "codex",
                str(FIXTURES / "codex.jsonl"),
                "--output",
                str(tmp_path / "outside.json"),
            ]
        )
    assert not (tmp_path / "outside.json").exists()


def test_missing_root_fails_before_source_read(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(ENVIRONMENT_KEY, raising=False)
    with pytest.raises(SystemExit):
        main(["extract", "codex", str(tmp_path / "does-not-exist")])
    error = capsys.readouterr().err
    assert "data_root_required" in error and "does-not-exist" not in error


def test_bounded_smoke_with_synthetic_sources_only(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "smoke_local", Path(__file__).parents[1] / "scripts" / "smoke_local.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sources = {}
    for provider in ["codex", "claude"]:
        source = tmp_path / provider
        source.mkdir()
        (source / "session.jsonl").write_bytes((FIXTURES / f"{provider}.jsonl").read_bytes())
        sources[provider] = source
    root = init_data_root(tmp_path / "corpus")
    result = module.smoke(root, sources)
    assert result["status"] == "passed"
    assert len(result["records"]) == 2
    assert (root.path / result["warehouse"] / "observatory.duckdb").is_file()
    for record in result["records"]:
        preserved = root.path / record["raw"]
        assert preserved.read_bytes() == Path(record["original_source"]).read_bytes()
        assert preserved.stat().st_mode & 0o777 == 0o400


def test_private_cli_preserves_supported_identifiers(tmp_path, monkeypatch):
    root = init_data_root(tmp_path / "corpus")
    monkeypatch.setenv(ENVIRONMENT_KEY, str(root.path))
    source = tmp_path / "synthetic.jsonl"
    # Deliberately key-like synthetic identifier proves ingestion is not a public projection.
    identity = "sk-" + "x" * 24
    record = json.loads((FIXTURES / "codex.jsonl").read_text().splitlines()[0])
    record["payload"]["cwd"] = identity
    source.write_text(json.dumps(record))
    main(["extract", "codex", str(source)])
    bundle = json.loads(next((root.path / "normalized" / "runs").glob("*.json")).read_text())
    assert bundle["run"]["facts"]["cwd"]["value"] == identity
