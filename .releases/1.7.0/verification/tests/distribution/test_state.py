from __future__ import annotations

import hashlib
import json

import pytest

from tools.policy_kit_distribution import state


def fixture(tmp_path):
    source = tmp_path / "session"
    source.mkdir()
    (source / "receipt.json").write_text('{"backup": "preserved preimage"}\n')
    (source / "runner.py").write_text("print('fixture')\n")
    (source / "runner.py").chmod(0o755)
    configuration = tmp_path / "kit" / "_machine" / "local.json"
    configuration.parent.mkdir(parents=True)
    settings = {
        "private_bundle": str(source),
        **{
            key: str(source / (key + ".json"))
            for key in ["registry", "plan", "evidence", "audit"]
        },
    }
    configuration.write_bytes(state.canonical(settings))
    before = hashlib.sha256(configuration.read_bytes()).hexdigest()
    return source, configuration, before


def migrate(tmp_path, **kwargs):
    source, configuration, before = fixture(tmp_path)
    return (
        source,
        configuration,
        state.migrate_state(
            source,
            tmp_path / "permanent" / "live",
            tmp_path / "backup" / "original",
            configuration,
            tmp_path / "kit" / "_machine" / "lock",
            tmp_path / "migration.json",
            expected_configuration=kwargs.pop("expected", before),
            **kwargs,
        ),
    )


def test_complete_migration_and_restore_without_session(tmp_path):
    source, configuration, result = migrate(tmp_path)
    assert result["copied_files"] == 2 and result["backup_verified"]
    settings = json.loads(configuration.read_text())
    assert settings["private_bundle"] == str(tmp_path / "permanent" / "live")
    assert (tmp_path / "permanent/live/runner.py").stat().st_mode & 0o777 == 0o700
    source.rename(tmp_path / "session-unavailable")
    restored = state.restore_snapshot(
        tmp_path / "backup/original", tmp_path / "restore", tmp_path / "migration.json"
    )
    assert restored["files"] == 2
    assert (tmp_path / "restore/receipt.json").read_bytes() == (
        tmp_path / "session-unavailable/receipt.json"
    ).read_bytes()


def test_changed_configuration_rejects_before_copy(tmp_path):
    with pytest.raises(state.StateError, match="reviewed preimage"):
        migrate(tmp_path, expected="0" * 64)
    assert not (tmp_path / "permanent/live").exists()


def test_interrupted_copy_never_activates(tmp_path, monkeypatch):
    original = state.copy_snapshot
    count = 0

    def interrupted(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("interrupted backup")
        return original(*args)

    monkeypatch.setattr(state, "copy_snapshot", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        migrate(tmp_path)
    assert json.loads((tmp_path / "kit/_machine/local.json").read_text())[
        "private_bundle"
    ] == str(tmp_path / "session")


def test_changed_backup_bytes_reject_restore(tmp_path):
    migrate(tmp_path)
    (tmp_path / "backup/original/receipt.json").write_text("changed")
    with pytest.raises(state.StateError, match="file changed"):
        state.restore_snapshot(
            tmp_path / "backup/original",
            tmp_path / "restore",
            tmp_path / "migration.json",
        )


def test_insecure_backup_mode_reject_restore(tmp_path):
    migrate(tmp_path)
    (tmp_path / "backup/original/receipt.json").chmod(0o644)
    with pytest.raises(state.StateError, match="file changed"):
        state.restore_snapshot(
            tmp_path / "backup/original",
            tmp_path / "restore",
            tmp_path / "migration.json",
        )


def test_symlink_source_rejects_without_activation(tmp_path):
    source, configuration, before = fixture(tmp_path)
    (source / "link").symlink_to(source / "receipt.json")
    with pytest.raises(state.StateError, match="Symlink"):
        state.migrate_state(
            source,
            tmp_path / "live",
            tmp_path / "backup",
            configuration,
            tmp_path / "lock",
            tmp_path / "migration.json",
            expected_configuration=before,
        )
    assert hashlib.sha256(configuration.read_bytes()).hexdigest() == before


def test_writer_lock_blocks_migration(tmp_path):
    source, configuration, before = fixture(tmp_path)
    with (
        state.distribution_lock(tmp_path / "lock"),
        pytest.raises(state.StateError, match="writer is active"),
    ):
        state.migrate_state(
            source,
            tmp_path / "live",
            tmp_path / "backup",
            configuration,
            tmp_path / "lock",
            tmp_path / "migration.json",
            expected_configuration=before,
            lock_timeout=0,
        )
    assert not (tmp_path / "live").exists()


def test_source_changes_during_snapshot_are_detected(tmp_path, monkeypatch):
    original = state.copy_snapshot

    def changed(source, destination, snapshot):
        original(source, destination, snapshot)
        (source / "receipt.json").write_text("owner change")

    monkeypatch.setattr(state, "copy_snapshot", changed)
    with pytest.raises(state.StateError, match="source changed|Source changed"):
        migrate(tmp_path)
    assert json.loads((tmp_path / "kit/_machine/local.json").read_text())[
        "private_bundle"
    ] == str(tmp_path / "session")


def test_existing_destination_is_preserved(tmp_path):
    (tmp_path / "permanent/live").mkdir(parents=True)
    (tmp_path / "permanent/live/owner").write_text("owner")
    with pytest.raises(state.StateError, match="already exists"):
        migrate(tmp_path)
    assert (tmp_path / "permanent/live/owner").read_text() == "owner"


def test_pointer_outside_source_never_activates(tmp_path):
    source, configuration, _ = fixture(tmp_path)
    settings = json.loads(configuration.read_text())
    settings["registry"] = str(tmp_path / "foreign.json")
    configuration.write_bytes(state.canonical(settings))
    before = hashlib.sha256(configuration.read_bytes()).hexdigest()
    with pytest.raises(state.StateError, match="outside reviewed bundle"):
        state.migrate_state(
            source,
            tmp_path / "live",
            tmp_path / "backup",
            configuration,
            tmp_path / "lock",
            tmp_path / "migration.json",
            expected_configuration=before,
        )
    assert hashlib.sha256(configuration.read_bytes()).hexdigest() == before


def test_recover_crash_after_configuration_switch(tmp_path, monkeypatch):
    original = state.atomic_private

    def interrupted(path, data, **kwargs):
        if (
            path.name == "migration.json"
            and json.loads(data).get("status") == "activated"
        ):
            raise OSError("interrupted activation journal")
        return original(path, data, **kwargs)

    monkeypatch.setattr(state, "atomic_private", interrupted)
    with pytest.raises(OSError, match="interrupted activation"):
        migrate(tmp_path)
    monkeypatch.setattr(state, "atomic_private", original)
    recovered = state.recover_activation(
        tmp_path / "migration.json",
        tmp_path / "kit/_machine/local.json",
        tmp_path / "kit/_machine/lock",
    )
    assert recovered == {"status": "activated", "backup_verified": True}
    assert (
        json.loads((tmp_path / "migration.json").read_text())["status"] == "activated"
    )


def test_recovery_preserves_owner_configuration(tmp_path):
    migrate(tmp_path)
    path = tmp_path / "kit/_machine/local.json"
    path.write_text('{"owner":"changed"}\n')
    with pytest.raises(state.StateError, match="intervening configuration"):
        state.recover_activation(
            tmp_path / "migration.json", path, tmp_path / "kit/_machine/lock"
        )
    assert json.loads(path.read_text()) == {"owner": "changed"}
