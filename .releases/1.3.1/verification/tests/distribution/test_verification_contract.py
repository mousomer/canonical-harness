"""Reject wrong component origins, malformed releases and unavailable test contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import types
from contextlib import contextmanager
from pathlib import Path

import pytest
from tools.policy_kit_distribution import contract, verification


def release_fixture(root: Path):
    root.mkdir()
    components = {}
    for component, destination in (
        ("payload", "tools/workspace_governance/MANIFEST.json"),
        ("verification", "tests/verification-dependencies.json"),
        ("distribution", "tools/policy_kit_distribution/contract.py"),
    ):
        source = component + "/" + destination
        path = root / source
        path.parent.mkdir(parents=True)
        path.write_text("synthetic " + component + "\n")
        path.chmod(0o644)
        row = {
            "source": source,
            "path": destination,
            "sha256": contract.digest(path.read_bytes()),
            "mode": 0o644,
        }
        components[component] = {
            "schema_version": 1,
            "files": [row],
            "identity_sha256": contract.digest(contract.canonical([row])),
        }
    release = {
        "schema_version": 2,
        "name": "workspace/policy-kit",
        "version": "test",
        "revision": "synthetic",
        "release_root": ".",
        "files": components["payload"]["files"],
        "verification": components["verification"],
        "distribution": components["distribution"],
        "provenance": {
            "payload_identity": contract.digest(
                contract.canonical(components["payload"]["files"])
            )
        },
    }
    manifest = root / "release.json"
    manifest.write_bytes(contract.canonical(release))
    return manifest, release


def test_selected_distribution_module_origins():
    root = Path(
        os.environ.get(
            "POLICY_KIT_DISTRIBUTION_ROOT",
            str(Path(__file__).resolve().parents[2] / "tools/policy_kit_distribution"),
        )
    ).resolve()
    for module in (contract, verification):
        assert Path(module.__file__).resolve().is_relative_to(root)


def test_all_selected_component_bytes_are_verified(tmp_path):
    path, release = release_fixture(tmp_path / "release")
    identity = contract.digest(contract.canonical(release))
    assert contract.validate_release(path, identity) == release
    target = path.parent / release["verification"]["files"][0]["source"]
    target.write_text("tampered")
    with pytest.raises(ValueError, match="bytes"):
        contract.validate_release(path, identity)


def test_declared_payload_identity_is_computed_from_selected_rows(tmp_path):
    path, release = release_fixture(tmp_path / "release")
    assert contract.validate_release(path) == release
    release["provenance"]["payload_identity"] = "b" * 64
    path.write_bytes(contract.canonical(release))
    with pytest.raises(ValueError, match="identity|provenance|payload"):
        contract.validate_release(path)


@pytest.mark.parametrize(
    "change", ["mode", "missing", "symlink", "undeclared", "wrong_identity", "preview"]
)
def test_incomplete_or_changed_release_is_refused(tmp_path, change):
    path, release = release_fixture(tmp_path / "release")
    identity = contract.digest(contract.canonical(release))
    target = path.parent / release["verification"]["files"][0]["source"]
    if change == "mode":
        target.chmod(0o755)
    elif change == "missing":
        target.unlink()
    elif change == "symlink":
        saved = tmp_path / "outside"
        target.rename(saved)
        target.symlink_to(saved)
    elif change == "undeclared":
        (path.parent / "extra").write_text("unexpected")
    elif change == "wrong_identity":
        identity = "b" * 64
    else:
        release["preview_only"] = True
        path.write_bytes(contract.canonical(release))
        identity = contract.digest(contract.canonical(release))
    with pytest.raises(ValueError):
        contract.validate_release(path, identity)


@pytest.mark.parametrize(
    "malformed",
    [
        [],
        None,
        {"schema_version": 2, "files": None},
        {"schema_version": 2, "files": [], "verification": None, "distribution": []},
    ],
)
def test_malformed_release_is_controlled_refusal(tmp_path, malformed):
    path = tmp_path / "release.json"
    path.write_text(json.dumps(malformed))
    with pytest.raises(ValueError):
        contract.validate_release(path)


@pytest.mark.parametrize("name", ["verification", "distribution"])
def test_component_cannot_move_sources_to_runtime_payload(tmp_path, name):
    path, release = release_fixture(tmp_path / "release")
    row = release[name]["files"][0]
    original = path.parent / row["source"]
    row["source"] = "payload/foreign-" + name
    destination = path.parent / row["source"]
    original.rename(destination)
    release[name]["identity_sha256"] = contract.digest(
        contract.canonical(release[name]["files"])
    )
    path.write_bytes(contract.canonical(release))
    with pytest.raises(ValueError):
        contract.validate_release(path)


@pytest.mark.parametrize("family", ["workspace_governance", "policy_kit_distribution"])
def test_preimported_package_must_match_its_specific_component(
    tmp_path, monkeypatch, family
):
    from tools.workspace_governance.validators import core

    runtime = Path(core.__file__).resolve().parents[1]
    distribution = Path(contract.__file__).resolve().parent
    # Positive control: current imported modules match these exact selections.
    contract.anchor_components(runtime, distribution)
    other = distribution if family == "workspace_governance" else runtime
    module = types.ModuleType("tools." + family + ".wrong_component")
    module.__file__ = str(other / "wrong_component.py")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(ValueError, match="foreign|component"):
        contract.anchor_components(runtime, distribution)


def test_candidate_selection_rejects_foreign_exporter_before_using_it(monkeypatch):
    from tools.workspace_governance.validators import core

    runtime = Path(core.__file__).resolve().parents[1]
    distribution = Path(contract.__file__).resolve().parent
    contract.anchor_components(runtime, distribution)
    # Immutable runtime/control components live in separate directories. Bind
    # this ordering test to their actual origins; candidate layout is covered
    # separately by the origin and immutable-selection tests above.
    monkeypatch.setattr(
        verification,
        "anchor_components",
        lambda *_args: contract.anchor_components(runtime, distribution),
    )
    root = runtime.parents[1]
    foreign = types.ModuleType("tools.workspace_governance.release")
    foreign.__file__ = str(Path(contract.__file__).resolve().parent / "release.py")

    def contaminated(*_args, **_kwargs):
        raise AssertionError("foreign exporter was invoked")

    foreign.collect_payload = contaminated
    foreign.collect_component = contaminated
    monkeypatch.setitem(sys.modules, foreign.__name__, foreign)
    with pytest.raises(ValueError, match="foreign|component"):
        verification.selected_components(root, root)


def test_dependency_contract_uses_its_declared_python_requirement():
    with pytest.raises(ValueError):
        verification.dependency_check(
            {
                "schema_version": 1,
                "python_requires": ">=99",
                "requirements": [],
                "executables": [],
            }
        )


@pytest.mark.parametrize("executable", ["git", "bash"])
def test_declared_missing_executable_rejects_before_test_dispatch(
    monkeypatch, executable
):
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: None)
    with pytest.raises(ValueError):
        verification.dependency_check(
            {
                "schema_version": 1,
                "python_requires": ">=3.11",
                "requirements": [],
                "executables": [executable],
            }
        )


def test_unavailable_python_dependency_rejects():
    with pytest.raises(
        (ValueError, verification.importlib.metadata.PackageNotFoundError)
    ):
        verification.dependency_check(
            {
                "schema_version": 1,
                "python_requires": ">=3.11",
                "requirements": ["fixture-missing-package>=1"],
                "executables": [],
            }
        )


def test_default_selection_uses_immutable_component_paths(tmp_path, monkeypatch):
    path, release = release_fixture(tmp_path / "release")
    monkeypatch.setattr(
        verification, "active_configuration", lambda _kit: ({}, {}, path, release)
    )
    runtime, control, tests, identity = verification.selected_components(
        tmp_path / "kit", None
    )
    assert runtime == path.parent / "payload/tools/workspace_governance"
    assert control == path.parent / "distribution/tools/policy_kit_distribution"
    assert tests == path.parent / "verification/tests"
    assert identity["selection"] == "active-immutable"
    assert (
        identity["verification_identity"] == release["verification"]["identity_sha256"]
    )


def export_sources(tmp_path):
    from tools.workspace_governance.release import BASELINE_FILES

    pack = tmp_path / "pack"
    kit = tmp_path / "kit"
    tests = tmp_path / "verification"
    distribution = tmp_path / "distribution"
    for root in (pack, kit, tests, distribution):
        root.mkdir()
    (pack / "MANIFEST.json").write_text(
        json.dumps(
            {
                "name": "workspace-governance",
                "version": "0.5.0",
                "revision": "synthetic-runtime-revision",
                "artifact_roles": [
                    {"path": "MANIFEST.json", "role": "executable_machinery"},
                    {"path": "VERSION", "role": "executable_machinery"},
                ],
            }
        )
    )
    (pack / "VERSION").write_text("0.5.0\n")
    for source, _ in BASELINE_FILES:
        target = kit / source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"policy_version": "1.1.3"})
            if source == "POLICY_VERSION.json"
            else "synthetic baseline\n"
        )
    test_file = tests / "tests/test_contract.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_contract():\n    assert True\n")
    control_file = distribution / "tools/policy_kit_distribution/control.py"
    control_file.parent.mkdir(parents=True)
    control_file.write_text('VERSION = "synthetic"\n')
    for root in (pack, kit, tests, distribution):
        for path in root.rglob("*"):
            if path.is_file():
                path.chmod(0o644)
    return pack, kit, tests, distribution


def export_components(sources, output):
    from tools.workspace_governance.release import export_release

    pack, kit, tests, distribution = sources
    return export_release(
        pack,
        kit,
        output,
        verification_root=tests,
        distribution_root=distribution,
    )


@pytest.mark.parametrize("component", ["verification", "distribution"])
@pytest.mark.parametrize("change", ["bytes", "mode"])
def test_component_bytes_and_modes_change_release_identity(tmp_path, component, change):
    sources = export_sources(tmp_path)
    first = export_components(sources, tmp_path / "first")
    _pack, _kit, tests, distribution = sources
    selected = tests if component == "verification" else distribution
    target = next(path for path in selected.rglob("*") if path.is_file())
    if change == "bytes":
        target.write_text(target.read_text() + "# semantic fixture change\n")
    else:
        target.chmod(0o755)
    second = export_components(sources, tmp_path / "second")
    assert second["identity"] != first["identity"]
    old = json.loads(Path(first["release"]).read_bytes())
    new = json.loads(Path(second["release"]).read_bytes())
    assert old[component]["identity_sha256"] != new[component]["identity_sha256"]
    other = "distribution" if component == "verification" else "verification"
    assert old[other] == new[other]
    assert old["files"] == new["files"]
    contract.validate_release(Path(first["release"]), first["identity"])
    contract.validate_release(Path(second["release"]), second["identity"])
    with pytest.raises(ValueError, match="different identity"):
        export_components(sources, tmp_path / "first")


def test_incidental_git_head_does_not_change_retained_export_identity(tmp_path):
    sources = export_sources(tmp_path)
    _, kit, _, _ = sources
    environment = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }

    def git(*arguments):
        result = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Synthetic Test",
                "-c",
                "user.email=synthetic@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *arguments,
            ],
            cwd=kit,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        return result.stdout.strip()

    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "Synthetic retained baseline")
    first_head = git("rev-parse", "HEAD")
    first = export_components(sources, tmp_path / "first")
    (kit / "incidental-publication-note.txt").write_text(
        "Separate publication history.\n"
    )
    git("add", "incidental-publication-note.txt")
    git("commit", "-qm", "Synthetic unrelated publication history")
    assert git("rev-parse", "HEAD") != first_head
    second = export_components(sources, tmp_path / "second")
    assert second["identity"] == first["identity"]


def test_runtime_source_mode_is_part_of_the_retained_identity(tmp_path):
    sources = export_sources(tmp_path)
    first = export_components(sources, tmp_path / "first")
    (sources[0] / "VERSION").chmod(0o755)
    second = export_components(sources, tmp_path / "second")
    assert second["identity"] != first["identity"]
    old = json.loads(Path(first["release"]).read_bytes())
    new = json.loads(Path(second["release"]).read_bytes())
    assert (
        new["provenance"]["payload_identity"] != old["provenance"]["payload_identity"]
    )
    assert old["verification"] == new["verification"]
    assert old["distribution"] == new["distribution"]
    version_path = "tools/workspace_governance/VERSION"
    old_row = next(row for row in old["files"] if row["path"] == version_path)
    new_row = next(row for row in new["files"] if row["path"] == version_path)
    assert old_row["mode"] == 0o644
    assert new_row == {**old_row, "mode": 0o755}
    contract.validate_release(Path(first["release"]), first["identity"])
    contract.validate_release(Path(second["release"]), second["identity"])


def test_runtime_export_rejects_unapproved_source_mode(tmp_path):
    sources = export_sources(tmp_path)
    (sources[0] / "VERSION").chmod(0o600)
    with pytest.raises(ValueError, match="mode"):
        export_components(sources, tmp_path / "release")


def test_activation_rechecks_receipt_absence_after_writer_lock(tmp_path, monkeypatch):
    from tools.policy_kit_distribution import activation

    target = tmp_path / "new-source.py"
    journal = tmp_path / "receipt.json"
    prior_receipt = b'{"kind":"prior-writer-receipt"}\n'
    operations = [activation.file_operation(target, b"new source\n")]

    @contextmanager
    def earlier_writer_completed(_path):
        # The public precheck happened before this writer acquired the lock.
        # Preserve the first writer's durable receipt when the lock is gained.
        activation.atomic_private(journal, prior_receipt)
        yield

    monkeypatch.setattr(activation, "distribution_lock", earlier_writer_completed)
    with pytest.raises(activation.StateError, match="receipt|journal"):
        activation.apply(operations, journal, tmp_path / "distribution.lock")
    assert journal.read_bytes() == prior_receipt
    assert not target.exists()


@pytest.mark.parametrize("kind", ["empty_directory", "symlink_directory"])
def test_snapshot_verification_refuses_undeclared_directories(tmp_path, kind):
    from tools.policy_kit_distribution import state

    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    (source / "record.json").write_text("{}\n")
    (source / "record.json").chmod(0o600)
    snapshot = state.inventory(source)
    saved = tmp_path / "saved"
    state.copy_snapshot(source, saved, snapshot)
    state.verify_snapshot(saved, snapshot)
    if kind == "empty_directory":
        (saved / "undeclared").mkdir(mode=0o700)
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (saved / "undeclared").symlink_to(outside, target_is_directory=True)
    with pytest.raises(state.StateError, match="(?i)directory|symlink|extra|changed"):
        state.verify_snapshot(saved, snapshot)


def test_private_artifact_dotdot_cannot_escape_the_declared_bundle(tmp_path):
    from tools.policy_kit_distribution import cli, state

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    assert cli._private_path(str(root / "record.json"), {"private_bundle": str(root)})
    with pytest.raises((cli.ControlError, state.StateError)):
        cli._private_path(str(root / "../foreign.json"), {"private_bundle": str(root)})


@pytest.mark.parametrize("row_kind", ["file", "directory"])
def test_malformed_snapshot_cannot_write_above_its_staged_root(tmp_path, row_kind):
    from tools.policy_kit_distribution import state

    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    data = b"synthetic owner bytes\n"
    (tmp_path / "escaped").write_bytes(data)
    malformed = {"schema_version": 1, "files": [], "directories": []}
    if row_kind == "file":
        malformed["files"] = [
            {"path": "../escaped", "sha256": state.digest(data), "mode": 0o600}
        ]
    else:
        malformed["directories"] = [{"path": "../escaped", "mode": 0o700}]
    destination = tmp_path / "permanent/saved"
    with pytest.raises(state.StateError):
        state.copy_snapshot(source, destination, malformed)
    assert not (destination.parent / "escaped").exists()
    assert not destination.exists()


def test_snapshot_copy_refuses_a_group_accessible_declared_file_mode(tmp_path):
    from tools.policy_kit_distribution import state

    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    record = source / "record.json"
    record.write_text("{}\n")
    record.chmod(0o600)
    snapshot = state.inventory(source)
    assert snapshot["files"][0]["mode"] == 0o600
    snapshot["files"][0]["mode"] = 0o644
    destination = tmp_path / "saved"
    with pytest.raises(state.StateError, match="mode|permission|snapshot"):
        state.copy_snapshot(source, destination, snapshot)
    assert not destination.exists()


def test_verification_argument_errors_never_echo_private_tokens_or_dispatch(
    tmp_path, capsys, monkeypatch
):
    sentinel = "SYNTHETIC_PRIVATE_ARG_TOKEN"

    def unexpected_dispatch(*_args, **_kwargs):
        raise AssertionError("invalid arguments dispatched a gate")

    monkeypatch.setattr(verification, "run_gate", unexpected_dispatch)
    with pytest.raises((ValueError, SystemExit)):
        verification.main(tmp_path, ["--private=" + sentinel])
    captured = capsys.readouterr()
    assert sentinel not in captured.out + captured.err


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_activation_preimages_must_be_owned_including_discovery_links(
    tmp_path, monkeypatch, kind
):
    from tools.policy_kit_distribution import activation

    target = tmp_path / "target"
    if kind == "file":
        target.write_text("synthetic owner bytes\n")
    else:
        directory = tmp_path / "immutable"
        directory.mkdir()
        target.symlink_to(directory, target_is_directory=True)
    assert activation.snapshot(target)["kind"] == kind
    uid = os.getuid()
    monkeypatch.setattr(activation.os, "getuid", lambda: uid + 1)
    with pytest.raises(activation.StateError, match="owner"):
        activation.snapshot(target)


def test_migration_cannot_overwrite_a_prior_durable_journal(tmp_path):
    from tools.policy_kit_distribution import state

    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    (source / "record.json").write_text("{}\n")
    (source / "record.json").chmod(0o600)
    settings = {
        "private_bundle": str(source),
        **{
            key: str(source / (key + ".json"))
            for key in ("registry", "plan", "evidence", "audit")
        },
    }
    configuration = tmp_path / "configuration.json"
    state.atomic_private(configuration, state.canonical(settings))
    expected = state.digest(configuration.read_bytes())
    journal = tmp_path / "migration.json"
    prior = b'{"kind":"prior-migration-journal"}\n'
    state.atomic_private(journal, prior)
    with pytest.raises(state.StateError, match="journal|receipt"):
        state.migrate_state(
            source,
            tmp_path / "live",
            tmp_path / "backup",
            configuration,
            tmp_path / "distribution.lock",
            journal,
            expected_configuration=expected,
        )
    assert journal.read_bytes() == prior
    assert state.digest(configuration.read_bytes()) == expected
    assert not (tmp_path / "live").exists()
    assert not (tmp_path / "backup").exists()
