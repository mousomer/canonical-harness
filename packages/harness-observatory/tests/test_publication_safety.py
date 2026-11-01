"""All private-looking values are invented and assembled in temporary test repositories."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness_observatory import cli
from harness_observatory.extractors import extract
from synthetic_inputs import synthetic_bytes

ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "scripts" / "public_safe.py"


@pytest.mark.parametrize(
    "suffix", ["out.json", "private/out.json", "generated/warehouse", "private/../out.json"]
)
def test_reject_inside_checkout(tmp_path, monkeypatch, suffix):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    monkeypatch.chdir(repo)
    with pytest.raises(cli.OutputBoundaryError):
        cli.private_destination(suffix)


def test_parent_and_symlink_resolution(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    with pytest.raises(cli.OutputBoundaryError):
        cli.private_destination(external / ".." / "repo" / "missing" / "out.json")
    (external / "alias").symlink_to(repo, target_is_directory=True)
    with pytest.raises(cli.OutputBoundaryError):
        cli.private_destination(external / "alias" / "missing" / "out.json")
    assert (
        cli.private_destination(external / "private" / "out.json")
        == external / "private" / "out.json"
    )
    sibling = tmp_path / "repo-other" / "out.json"
    assert cli.private_destination(sibling) == sibling


def test_real_project_guard():
    with pytest.raises(cli.OutputBoundaryError):
        cli.private_destination(ROOT / "generated" / "warehouse")


def sample():
    return extract(synthetic_bytes("codex"), "codex")


def test_validation_diagnostic_is_sanitized(tmp_path, capsys, monkeypatch):
    from harness_observatory.data_root import init_data_root

    tmp_path = init_data_root(tmp_path / "corpus").path
    monkeypatch.setenv("HARNESS_OBSERVATORY_DATA_ROOT", str(tmp_path))
    secret = "ghp_" + "s" * 36
    private_path = "/" + "Users/" + "synthetic-person/private/source"
    bundle = sample()
    bundle["run"]["agent"] = secret + private_path
    source = tmp_path / "input.json"
    source.write_text(json.dumps(bundle))
    with pytest.raises(SystemExit) as exc:
        cli.main(["build-warehouse", str(source), "--output", str(tmp_path / "external-warehouse")])
    stderr = capsys.readouterr().err
    assert exc.value.code == 2 and "schema_validation" in stderr
    assert secret not in stderr and private_path not in stderr and str(source) not in stderr
    assert "Traceback" not in stderr


def test_subprocess_diagnostic_is_sanitized(tmp_path, capsys, monkeypatch):
    from harness_observatory.data_root import init_data_root

    tmp_path = init_data_root(tmp_path / "corpus").path
    monkeypatch.setenv("HARNESS_OBSERVATORY_DATA_ROOT", str(tmp_path))
    source = tmp_path / "input.json"
    source.write_text(json.dumps(sample()))
    private_path = tmp_path / "synthetic-private-repository"
    with pytest.raises(SystemExit):
        cli.main(
            [
                "enrich-git",
                str(source),
                "--repo",
                str(private_path),
                "--start-sha",
                "a" * 40,
                "--evidence",
                "synthetic",
                "--output",
                str(tmp_path / "output.json"),
            ]
        )
    stderr = capsys.readouterr().err
    assert "subprocess_failure" in stderr
    assert (
        str(source) not in stderr and str(private_path) not in stderr and "Traceback" not in stderr
    )


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "history"
    repo.mkdir()

    def git(*args):
        return (
            subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)
            .decode()
            .strip()
        )

    git("init", "-b", "main")
    git("config", "user.name", "Synthetic")
    git("config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("Synthetic repository\n")
    git("add", ".")
    git("commit", "-m", "Synthetic initial")
    return repo, git


def scan(repo):
    return subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(repo)], capture_output=True, text=True
    )


def test_scanner_deleted_secret_and_ref_history(repository):
    repo, git = repository
    token = "ghp_" + "x" * 36
    (repo / "leak.txt").write_text(token)
    git("add", ".")
    git("commit", "-m", "Synthetic unsafe example")
    unsafe = git("rev-parse", "HEAD")
    (repo / "leak.txt").unlink()
    git("add", "-u")
    git("commit", "-m", "Remove synthetic example")
    result = scan(repo)
    assert result.returncode == 1 and "credential_token" in result.stderr
    assert token not in result.stderr and unsafe in result.stderr
    git("tag", "unsafe-example", unsafe)
    git("checkout", "--orphan", "clean")
    git("commit", "-m", "Clean synthetic root")
    git("branch", "-D", "main")
    assert scan(repo).returncode == 1  # Tag preserves otherwise removed history.
    git("tag", "-d", "unsafe-example")
    assert scan(repo).returncode == 0  # Unreachable objects are not blockers.


@pytest.mark.parametrize(
    "kind", ["home", "provider", "artifact", "directory", "index", "credential_json"]
)
def test_scanner_current_content(repository, kind):
    repo, git = repository
    path = repo / "example.txt"
    value = "synthetic"
    if kind == "home":
        value = "/" + "Users/" + "synthetic-person/private"
    elif kind == "provider":
        value = "." + "codex/" + "sessions/example"
    elif kind == "credential_json":
        value = json.dumps({"password": "s" * 24})
    elif kind == "artifact":
        path = repo / "evidence.jsonl"
    elif kind == "directory":
        path = repo / "runs" / "example.json"
    elif kind == "index":
        value = "sk-" + "z" * 36
    path.parent.mkdir(exist_ok=True)
    path.write_text(value)
    git("add", ".")
    if kind == "index":
        path.write_text("safe working copy")
    result = scan(repo)
    assert result.returncode == 1
    assert value not in result.stderr


def test_serialized_inputs_rejected_even_when_synthetic(repository):
    repo, git = repository
    target = repo / "tests" / "fixtures" / "codex.jsonl"
    target.parent.mkdir(parents=True)
    target.write_bytes(synthetic_bytes("codex"))
    git("add", ".")
    result = scan(repo)
    assert result.returncode == 1
    assert "unapproved_data_artifact" in result.stderr


def test_scanner_parent_symlink_does_not_read_external(repository, tmp_path):
    repo, git = repository
    nested = repo / "nested"
    nested.mkdir()
    (nested / "source.txt").write_text("synthetic")
    git("add", ".")
    git("commit", "-m", "Synthetic nested file")
    (nested / "source.txt").unlink()
    nested.rmdir()
    external = tmp_path / "external"
    external.mkdir()
    nested.symlink_to(external, target_is_directory=True)
    result = scan(repo)
    assert result.returncode == 1 and "external_symlink_ancestor" in result.stderr


def test_scanner_canonical_data_under_generic_extension(repository):
    repo, git = repository
    (repo / "data.json").write_text(json.dumps({"run_id": "sha256:" + "a" * 64}))
    git("add", ".")
    assert "canonical_run_record" in scan(repo).stderr


def test_argument_diagnostic_is_sanitized(capsys):
    private_value = "synthetic-private-argument"
    with pytest.raises(SystemExit):
        cli.main([private_value])
    stderr = capsys.readouterr().err
    assert "invalid_arguments" in stderr and private_value not in stderr


def test_jsonl_rejects_nonsecret_payload(repository):
    repo, git = repository
    fixtures = repo / "tests" / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "codex.jsonl").write_text('{"synthetic": true}\n')
    git("add", ".")
    result = scan(repo)
    assert result.returncode == 1
    assert "unapproved_data_artifact" in result.stderr
