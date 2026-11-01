"""Bind reviewed software bytes even when a public baseline exists."""

import subprocess

import pytest
from test_publication import PUBLICATION, ROOT, codes, git, make_repository, manifest


@pytest.fixture
def repository(tmp_path):
    return make_repository(tmp_path)


def test_worktree_cannot_revert_an_allowlisted_change_to_baseline(repository):
    root, base = repository
    original = (root / "README.md").read_text()
    (root / "README.md").write_text("Reviewed changed documentation.\n")
    manifest(root, base, ["README.md"])
    (root / "README.md").write_text(original)
    result = PUBLICATION.scan(root, base)
    assert not result["ok"] and "unallowlisted_source_bytes" in codes(result)


def test_staged_manifest_requires_the_reviewed_index_not_a_partial_stage(repository):
    root, base = repository
    (root / "README.md").write_text("Reviewed changed documentation.\n")
    manifest(root, base, ["README.md"])
    assert PUBLICATION.scan(root, base)["ok"]
    git(root, "add", PUBLICATION.MANIFEST)
    result = PUBLICATION.scan(root, base)
    assert not result["ok"] and "unallowlisted_source_bytes" in codes(result)
    git(root, "add", "README.md")
    assert PUBLICATION.scan(root, base)["ok"]


def test_wrapper_refuses_published_base_and_repository_overrides():
    wrapper = ROOT / "scripts/verify_publication.sh"
    for args in (["--published-base", "a" * 40], ["--repo", "/"], ["--current-only"]):
        result = subprocess.run(
            ["/bin/bash", str(wrapper), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "unsupported arguments" in result.stderr


def test_staged_manifest_cannot_omit_a_new_source_from_index(repository):
    root, base = repository
    (root / "software.py").write_text("VALUE = 1\n")
    manifest(root, base, ["software.py"])
    git(root, "add", PUBLICATION.MANIFEST)
    result = PUBLICATION.scan(root, base)
    assert not result["ok"]
    assert "allowlisted_source_missing_from_index" in codes(result)


def test_published_software_cannot_be_deleted_without_a_new_contract(repository):
    root, base = repository
    (root / "README.md").unlink()
    git(root, "add", "-A")
    result = PUBLICATION.scan(root, base)
    assert not result["ok"]
    assert "published_source_deleted_from_index" in codes(result)
