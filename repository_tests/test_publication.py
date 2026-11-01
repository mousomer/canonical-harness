"""Publication regressions use only invocation-owned synthetic Git repositories."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "publication_under_test", ROOT / "tools/publication/verify.py"
)
PUBLICATION = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PUBLICATION
SPEC.loader.exec_module(PUBLICATION)


def git(root, *args):
    environment = {
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    return (
        subprocess.check_output(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=Synthetic",
                "-c",
                "user.email=synthetic@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            env=environment,
            stderr=subprocess.PIPE,
        )
        .decode()
        .strip()
    )


def make_repository(tmp_path):
    root = tmp_path / "software"
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / "README.md").write_text("Synthetic software repository.\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Synthetic public base")
    base = git(root, "rev-parse", "HEAD")
    manifest(root, base)
    return root, base


@pytest.fixture
def repository(tmp_path):
    return make_repository(tmp_path)


def manifest(root, base, names=()):
    artifacts = []
    for name in names:
        path = root / name
        artifacts.append(
            {
                "path": name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "mode": "100755" if path.stat().st_mode & 0o111 else "100644",
                "kind": "test",
            }
        )
    target = root / PUBLICATION.MANIFEST
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {"schema_version": 1, "published_base": base, "artifacts": artifacts}
        )
    )
    return target


def codes(result):
    return {finding["code"] for finding in result["failures"]}


def test_proposal_before_staging_and_newly_committed_source_both_pass(repository):
    root, base = repository
    (root / "software.py").write_text("VALUE = 1\n")
    manifest(root, base, ["software.py"])
    assert PUBLICATION.scan(root, base)["ok"]
    git(root, "add", ".")
    git(root, "commit", "-m", "Add reviewed synthetic software")
    result = PUBLICATION.scan(root, base)
    assert result["ok"] and result["reachable_commits_checked"] == 1


def test_index_cannot_hide_behind_a_safe_worktree(repository):
    root, base = repository
    token = "ghp_" + "s" * 36
    source = root / "software.py"
    source.write_text("VALUE = " + repr(token) + "\n")
    manifest(root, base, ["software.py"])
    git(root, "add", ".")
    source.write_text("VALUE = 1\n")
    manifest(root, base, ["software.py"])
    result = PUBLICATION.scan(root, base)
    assert not result["ok"] and "credential_token" in codes(result)
    assert token not in json.dumps(result)


@pytest.mark.parametrize(
    "name",
    [
        "_machine/config.json",
        "state/evidence.json",
        "raw/input.json",
        "logs/run.txt",
        "source.JSONL",
        "warehouse/results.parquet",
        "../escape.py",
    ],
)
def test_manifest_cannot_qualify_private_or_data_paths(repository, name):
    root, base = repository
    target = root / PUBLICATION.MANIFEST
    value = json.loads(target.read_text())
    value["artifacts"] = [
        {"path": name, "sha256": "a" * 64, "mode": "100644", "kind": "test"}
    ]
    target.write_text(json.dumps(value))
    assert PUBLICATION.scan(root, base)["input_refusal"] == "source_manifest_invalid"


def test_unknown_source_and_generic_json_evidence_are_rejected(repository):
    root, base = repository
    (root / "unreviewed.py").write_text("VALUE = 1\n")
    assert "unallowlisted_source_bytes" in codes(PUBLICATION.scan(root, base))
    (root / "unreviewed.py").unlink()
    (root / "schema.json").write_text(json.dumps({"run_id": "sha256:" + "a" * 64}))
    manifest(root, base, ["schema.json"])
    assert "canonical_run_record" in codes(PUBLICATION.scan(root, base))


def test_digest_kind_and_base_are_closed_contracts(repository):
    root, base = repository
    (root / "software.py").write_text("VALUE = 1\n")
    target = manifest(root, base, ["software.py"])
    value = json.loads(target.read_text())
    value["artifacts"][0]["kind"] = "data"
    target.write_text(json.dumps(value))
    assert PUBLICATION.scan(root, base)["input_refusal"] == "source_manifest_invalid"
    target = manifest(root, base, ["software.py"])
    (root / "software.py").write_text("VALUE = 2\n")
    assert "unallowlisted_source_bytes" in codes(PUBLICATION.scan(root, base))
    value = json.loads(target.read_text())
    value["published_base"] = "b" * 40
    target.write_text(json.dumps(value))
    assert PUBLICATION.scan(root, base)["input_refusal"] == "source_manifest_invalid"


def test_symlinked_source_is_not_read_or_qualified(repository, tmp_path):
    root, base = repository
    external = tmp_path / "outside.py"
    external.write_text("VALUE = 1\n")
    (root / "software.py").symlink_to(external)
    manifest(root, base, ["software.py"])
    result = PUBLICATION.scan(root, base)
    assert result["input_refusal"] == "source_symlink"
    assert str(external) not in json.dumps(result)


def test_ambient_git_overrides_cannot_select_another_repository(
    repository, tmp_path, monkeypatch
):
    root, base = repository
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init")
    token = "github_pat_" + "s" * 40
    (root / "software.py").write_text("VALUE = " + repr(token) + "\n")
    manifest(root, base, ["software.py"])
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git/index"))
    assert "credential_token" in codes(PUBLICATION.scan(root, base))


def test_deleted_new_history_and_replacement_refs_cannot_hide_a_secret(repository):
    root, base = repository
    token = "ghp_" + "q" * 36
    source = root / "software.py"
    source.write_text("VALUE = " + repr(token) + "\n")
    manifest(root, base, ["software.py"])
    git(root, "add", ".")
    git(root, "commit", "-m", "Synthetic unsafe source")
    unsafe = git(root, "rev-parse", "HEAD")
    source.unlink()
    manifest(root, base)
    git(root, "add", "-A")
    git(root, "commit", "-m", "Remove synthetic unsafe source")
    git(root, "replace", unsafe, base)
    result = PUBLICATION.scan(root, base)
    assert not result["ok"] and "credential_token" in codes(result)
    assert token not in json.dumps(result)


def test_tag_messages_are_publication_inputs(repository):
    root, base = repository
    token = "ghp_" + "t" * 36
    git(root, "tag", "-a", "synthetic-tag", "-m", token)
    result = PUBLICATION.scan(root, base)
    assert "credential_token" in codes(result)
    assert token not in json.dumps(result)


@pytest.mark.parametrize(
    "limits",
    [
        PUBLICATION.Limits(blob_bytes=8),
        PUBLICATION.Limits(total_bytes=8),
        PUBLICATION.Limits(entries=1),
        PUBLICATION.Limits(git_output_bytes=1),
        PUBLICATION.Limits(seconds=0),
    ],
)
def test_input_limits_refuse_instead_of_skipping(repository, limits):
    root, base = repository
    result = PUBLICATION.scan(root, base, limits=limits)
    assert not result["ok"] and result["input_refusal"]


def test_existing_synthetic_source_exception_binds_rule_path_and_exact_bytes(
    repository,
):
    root, base = repository
    name = "tests/workspace_governance/test_fleet_metadata_projection.py"
    target = root / name
    target.parent.mkdir(parents=True)
    target.write_bytes((ROOT / name).read_bytes())
    manifest(root, base, [name])
    result = PUBLICATION.scan(root, base)
    assert result["ok"] and result["reviewed_synthetic_source_finding_count"] == 1
    target.write_bytes(target.read_bytes() + b"\n# Changed synthetic source\n")
    manifest(root, base, [name])
    assert "absolute_home" in codes(PUBLICATION.scan(root, base))
