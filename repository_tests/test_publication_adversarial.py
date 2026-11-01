"""Adversarial publication probes use disposable, entirely synthetic Git inputs."""

import json
from pathlib import Path

import pytest
from test_publication import PUBLICATION, codes, git, make_repository, manifest


@pytest.fixture
def repository(tmp_path):
    return make_repository(tmp_path)


def metadata_path(root, name):
    path = Path(git(root, "rev-parse", "--git-path", name))
    return path if path.is_absolute() else root / path


def test_grafted_history_cannot_hide_a_deleted_secret_commit(repository):
    root, base = repository
    token = "ghp_" + "g" * 36
    source = root / "software.py"
    source.write_text("VALUE = " + repr(token) + "\n")
    manifest(root, base, ["software.py"])
    git(root, "add", ".")
    git(root, "commit", "-m", "Add synthetic unsafe source")
    unsafe = git(root, "rev-parse", "HEAD")
    source.unlink()
    manifest(root, base)
    git(root, "add", "-A")
    git(root, "commit", "-m", "Remove synthetic unsafe source")
    head = git(root, "rev-parse", "HEAD")
    assert "parent " + unsafe in git(root, "cat-file", "-p", head)
    assert "credential_token" in codes(PUBLICATION.scan(root, base))

    grafts = metadata_path(root, "info/grafts")
    grafts.parent.mkdir(parents=True, exist_ok=True)
    grafts.write_text(head + " " + base + "\n")
    result = PUBLICATION.scan(root, base)
    assert not result["ok"] and result["input_refusal"]
    assert token not in json.dumps(result)


def test_shallow_boundary_is_not_complete_publication_evidence(repository):
    root, base = repository
    assert PUBLICATION.scan(root, base)["ok"]
    metadata_path(root, "shallow").write_text(base + "\n")
    result = PUBLICATION.scan(root, base)
    assert not result["ok"] and result["input_refusal"]


def test_commit_identity_headers_are_scanned_without_echoing_values(repository):
    root, base = repository
    token = "ghp_" + "a" * 36
    source = root / "software.py"
    source.write_text("VALUE = 1\n")
    manifest(root, base, ["software.py"])
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=" + token,
        "commit",
        "-m",
        "Add innocuous synthetic source",
    )
    result = PUBLICATION.scan(root, base)
    assert "credential_token" in codes(result)
    assert token not in json.dumps(result)


def test_annotated_tag_header_is_scanned_without_echoing_its_name(repository):
    root, base = repository
    token = "ghp_" + "t" * 36
    git(root, "tag", "-a", token, "-m", "Innocuous synthetic tag message")
    result = PUBLICATION.scan(root, base)
    assert "credential_token" in codes(result)
    assert token not in json.dumps(result)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("extensions.partialClone", "synthetic-promisor"),
        ("remote.synthetic-promisor.promisor", "true"),
    ],
)
def test_partial_clone_declarations_refuse_without_contacting_a_remote(
    repository, tmp_path, name, value
):
    root, base = repository
    assert PUBLICATION.scan(root, base)["ok"]
    # All fixture objects are present; no clone or fetch is used. Even an
    # unexpected transport attempt is confined to a nonexistent local path and
    # denied by protocol policy, so this regression cannot use the network.
    git(root, "config", "remote.synthetic-promisor.url", str(tmp_path / "no-remote"))
    git(root, "config", "protocol.allow", "never")
    git(root, "config", name, value)
    result = PUBLICATION.scan(root, base)
    assert not result["ok"] and result["input_refusal"]
    assert str(tmp_path) not in json.dumps(result)
