from __future__ import annotations

import os

import pytest
from tools.policy_kit_distribution import activation as a


def test_activation_pins_link_and_recovers_exact_preimage(tmp_path):
    source = tmp_path / "source.py"
    source.write_bytes(b"old")
    source.chmod(0o644)
    old = tmp_path / "old"
    old.mkdir()
    release = tmp_path / "immutable"
    release.mkdir()
    link = tmp_path / "discovery"
    link.symlink_to(old)
    operations = [a.file_operation(source, b"new"), a.link_operation(link, release)]
    receipt = tmp_path / "receipt.json"
    assert a.apply(operations, receipt, tmp_path / "lock")["writes"] == 2
    assert os.readlink(link) == str(release)
    import json

    journal = json.loads(receipt.read_bytes())
    a.rollback(journal, receipt)
    assert source.read_bytes() == b"old" and os.readlink(link) == str(old)


def test_changed_preimage_rejects_whole_transaction(tmp_path):
    one = tmp_path / "one"
    one.write_bytes(b"old")
    two = tmp_path / "two"
    two.write_bytes(b"old")
    operations = [a.file_operation(one, b"new"), a.file_operation(two, b"new")]
    two.write_bytes(b"owner")
    with pytest.raises(a.StateError, match="preimage changed"):
        a.apply(operations, tmp_path / "receipt", tmp_path / "lock")
    assert one.read_bytes() == b"old" and two.read_bytes() == b"owner"


def test_failed_write_rolls_back_only_unchanged_postimages(tmp_path, monkeypatch):
    one = tmp_path / "one"
    one.write_bytes(b"old")
    two = tmp_path / "two"
    two.write_bytes(b"old")
    operations = [a.file_operation(one, b"new"), a.file_operation(two, b"new")]
    original = a.write_state

    def fail(path, state, content=None):
        if path == two and state == operations[1]["after"]:
            raise OSError("interrupt")
        return original(path, state, content)

    monkeypatch.setattr(a, "write_state", fail)
    with pytest.raises(OSError):
        a.apply(operations, tmp_path / "receipt", tmp_path / "lock")
    assert one.read_bytes() == two.read_bytes() == b"old"


def test_intervening_owner_edit_is_never_clobbered(tmp_path):
    import json

    target = tmp_path / "source"
    target.write_bytes(b"old")
    receipt = tmp_path / "receipt"
    a.apply([a.file_operation(target, b"new")], receipt, tmp_path / "lock")
    target.write_bytes(b"owner")
    journal = json.loads(receipt.read_bytes())
    a.rollback(journal, receipt)
    assert target.read_bytes() == b"owner" and journal["status"] == "rollback_conflict"


def test_no_write_reapplication_and_symlink_file_refusal(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"new")
    source.chmod(0o644)
    assert (
        a.apply(
            [a.file_operation(source, b"new")], tmp_path / "receipt", tmp_path / "lock"
        )["writes"]
        == 0
    )
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(a.StateError, match="symlink"):
        a.file_operation(link, b"new")
