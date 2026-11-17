"""Portable fleet contracts: real disposable runners, no product/host fixtures."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.workspace_governance import fleet, fleet_execution, fleet_support


def write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
        env={"PATH": "/usr/bin:/bin", "HOME": str(root), "GIT_CONFIG_NOSYSTEM": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def repository(root: Path, *, product_exit: int = 0) -> Path:
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Disposable fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    write(root / ".gitignore", "*.ran\nfail-baseline\n")
    write(root / "AGENTS.md", "# Human doctrine\nPreserve the owning rules.\n")
    write(root / "README.md", "Product authority.\n")
    write(
        root / "scripts/verify.sh",
        "#!/bin/sh\nprintf checked > product.ran\nexit " + str(product_exit) + "\n",
        0o755,
    )
    write(
        root / "scripts/baseline.sh",
        "#!/bin/sh\nprintf checked >> baseline.ran\n./gov check\n",
        0o755,
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "Disposable source")
    return root


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    roots = [
        repository(tmp_path / "consumer with spaces"),
        repository(tmp_path / "second consumer", product_exit=7),
    ]
    release_root = tmp_path / "release"
    write(
        release_root / "gov",
        '#!/bin/sh\n[ "$1" = check ] || exit 2\n[ ! -f fail-baseline ]\n',
        0o755,
    )
    release = {
        "schema_version": 1,
        "name": "workspace/policy-kit",
        "version": "fixture",
        "revision": "fixture-content",
        "release_root": ".",
        "files": [
            {
                "path": "gov",
                "source": "gov",
                "sha256": fleet.digest((release_root / "gov").read_bytes()),
                "mode": 0o755,
            }
        ],
    }
    release_path = release_root / "release.json"
    release_path.write_bytes(fleet.canonical(release))
    registry = {
        "schema_version": 1,
        "scope": {"whole_computer_coverage": True},
        "checkouts": [
            {
                "id": "first" if index == 0 else "second",
                "path": str(root),
                "repository_id": "fixture-" + str(index),
                "integration_mode": "direct",
                "profile": "tools",
                "authorities": ["README.md"],
                "verification": {
                    "argv": ["./scripts/verify.sh"],
                    "runner": {
                        "kind": "shell",
                        "path": "scripts/verify.sh",
                        "governance_runner": "scripts/baseline.sh",
                    },
                },
            }
            for index, root in enumerate(roots)
        ],
    }
    registry_path = tmp_path / "registry.json"
    registry_path.write_bytes(fleet.canonical(registry))
    return roots, registry_path, release_path


def save_registry(path: Path, registry: dict) -> None:
    path.write_bytes(fleet.canonical(registry))


def install(setup):
    _roots, registry, release = setup
    plan_path = registry.parent / "plan.json"
    plan_path.write_bytes(fleet.canonical(fleet.plan(registry, release)))
    receipt = registry.parent / "receipt.json"
    result = fleet.apply(plan_path, receipt)
    assert all(row["status"] == "installed" for row in result["checkouts"]), result
    return receipt


def collect(setup, *, ids=None, baseline=False):
    _roots, registry, release = setup
    path = registry.parent / ("baselines.json" if baseline else "evidence.json")
    result = fleet.collect_acceptance(
        registry, release, checkout_ids=ids, output_path=path, baseline_only=baseline
    )
    return result, path


@pytest.mark.parametrize("integration_mode", ["direct", "inherited_checkout"])
def test_distribution_source_cannot_become_an_installer_consumer(
    setup, integration_mode
):
    roots, registry_path, release_path = setup
    registry = fleet.load(registry_path)
    registry["checkouts"][0].update(
        classification="governance_distribution_source",
        integration_mode=integration_mode,
    )
    save_registry(registry_path, registry)
    planned = fleet.plan(registry_path, release_path)
    source = planned["checkouts"][0]
    assert source["status"] == "blocked"
    assert source["operations"] == []
    assert source["errors"] == ["Protected repository classification cannot be mutated"]
    assert not (roots[0] / "gov").exists()
    assert not (roots[0] / "product.ran").exists()
    assert not (roots[0] / "baseline.ran").exists()


@pytest.mark.parametrize("run_checks", [False, True])
def test_read_only_distribution_source_never_claims_or_runs_producer_acceptance(
    setup, run_checks
):
    roots, registry_path, release_path = setup
    registry = fleet.load(registry_path)
    registry["checkouts"] = registry["checkouts"][:1]
    registry["checkouts"][0].update(
        classification="governance_distribution_source",
        integration_mode="external",
        managed_files=[],
        external_policy={
            "mode": "read-only",
            "owner": "Fixture producer owner",
            "reason": "Preserve producer source",
            "release_manifest_sha256": fleet.digest(release_path.read_bytes()),
        },
        producer_verification={"argv": ["./scripts/verify.sh"]},
    )
    save_registry(registry_path, registry)
    planned = fleet.plan(registry_path, release_path)["checkouts"][0]
    assert planned["status"] == "external" and planned["operations"] == []
    audited = fleet.audit(registry_path, release_path, run_checks=run_checks)
    observed = audited["checkouts"][0]
    assert observed["status"] == "governed_read_only"
    assert observed["product_acceptance"] == "not_run"
    assert observed["governance_baseline"] == "not_observed"
    assert observed["structural_check"] == "not_run"
    assert not (roots[0] / "gov").exists()
    assert not (roots[0] / "product.ran").exists()
    assert not (roots[0] / "baseline.ran").exists()


def test_mixed_results_preserve_valid_passing_acceptance(setup):
    roots, registry, release = setup
    install(setup)
    evidence, path = collect(setup)
    assert not evidence["ok"] and evidence["failures"] == ["second"]
    assert evidence["trust"] == "collector-reported" and evidence["schema_version"] == 2
    audited = fleet.audit(registry, release, evidence_path=path, run_checks=True)
    assert not audited["ok"]
    first, second = audited["checkouts"]
    assert (
        first["status"] == "accepted"
        and first["product_acceptance"] == "collector_reported_passed"
    )
    assert (
        first["structural_check"] == "passed"
        and first["governance_baseline"] == "passed"
    )
    assert (
        second["status"] == "incomplete" and second["governance_baseline"] == "passed"
    )
    assert all((root / "product.ran").exists() for root in roots)


@pytest.mark.parametrize(
    "change",
    [
        "kind",
        "schema",
        "registry",
        "release",
        "collector",
        "policy",
        "trust",
        "incomplete",
        "run-id",
        "timestamp",
        "reverse-time",
        "duplicate-id",
        "unknown-id",
        "omitted-record",
        "aggregate",
        "failed-without-error",
        "pass-with-error",
        "boolean-exit",
        "timeout",
        "baseline-timeout",
        "wrong-mode",
        "wrong-run",
        "missing-output",
    ],
)
def test_envelope_tampering_never_establishes_acceptance(setup, change):
    _roots, registry, release = setup
    install(setup)
    evidence, path = collect(setup)
    data = copy.deepcopy(evidence)
    record = data["checkouts"][0]
    mutations = {
        "kind": lambda: data.update(kind="unrelated-document"),
        "schema": lambda: data.update(schema_version=True),
        "registry": lambda: data.update(registry_sha256="0" * 64),
        "release": lambda: data.update(release_manifest_sha256="0" * 64),
        "collector": lambda: data.update(collector_id="invented"),
        "policy": lambda: data.update(execution_policy="legacy"),
        "trust": lambda: data.update(trust="authenticated"),
        "incomplete": lambda: data.update(completed=False),
        "run-id": lambda: data.update(run_id="anything"),
        "timestamp": lambda: record.update(started_at="anything"),
        "reverse-time": lambda: record.update(completed_at="2000-01-01T00:00:00+00:00"),
        "duplicate-id": lambda: data["selection"].__setitem__(1, data["selection"][0]),
        "unknown-id": lambda: data["selection"].__setitem__(0, "unknown"),
        "omitted-record": lambda: data["checkouts"].pop(),
        "aggregate": lambda: data.update(ok=True),
        "failed-without-error": lambda: data["checkouts"][1].update(errors=[]),
        "pass-with-error": lambda: record.update(errors=["failed after all"]),
        "boolean-exit": lambda: record.update(exit_code=False),
        "timeout": lambda: record.update(timed_out=True),
        "baseline-timeout": lambda: record.update(governance_timed_out=True),
        "wrong-mode": lambda: record.update(mode="baseline"),
        "wrong-run": lambda: record.update(run_id="0" * 32),
        "missing-output": lambda: record.pop("stdout_sha256"),
    }
    mutations[change]()
    path.write_bytes(fleet.canonical(data))
    audit = fleet.audit(registry, release, evidence_path=path)
    assert not audit["ok"] and "invalid_verification_evidence" in audit["failures"]
    assert all(row["product_acceptance"] == "not_run" for row in audit["checkouts"])


def test_legacy_evidence_remains_historic(setup):
    _roots, registry, release = setup
    install(setup)
    data, path = collect(setup)
    data["schema_version"] = 1
    path.write_bytes(fleet.canonical(data))
    result = fleet.audit(registry, release, evidence_path=path)
    assert not result["ok"] and "invalid_verification_evidence" in result["failures"]


def test_composition_preserves_sources_failures_and_scope(setup):
    _roots, registry, release = setup
    install(setup)
    one, _ = collect(setup, ids=["first"])
    two, _ = collect(setup, ids=["second"])
    merged = fleet.merge_evidence(registry, release, [one, two])
    assert merged["checkouts"] == one["checkouts"] + two["checkouts"]
    assert merged["failures"] == ["second"] and not merged["ok"]
    assert merged["source_runs"] == [one, two]
    path = registry.parent / "merged.json"
    path.write_bytes(fleet.canonical(merged))
    assert (
        fleet.audit(registry, release, evidence_path=path)["checkouts"][0]["status"]
        == "accepted"
    )
    with pytest.raises(fleet.FleetError):
        fleet.merge_evidence(registry, release, [one, one])
    with pytest.raises(fleet.FleetError):
        fleet.merge_evidence(registry, release, [merged])


def test_baseline_only_declared_runner_never_starts_product(setup):
    roots, registry, release = setup
    install(setup)
    result, path = collect(setup, baseline=True)
    assert result["ok"] and result["mode"] == "baseline"
    assert all(record["status"] == "baseline_passed" for record in result["checkouts"])
    assert all(
        (root / "baseline.ran").exists() and not (root / "product.ran").exists()
        for root in roots
    )
    audit = fleet.audit(registry, release, evidence_path=path, run_checks=True)
    assert not audit["ok"]
    assert all(
        record["governance_baseline"] == "passed"
        and record["product_acceptance"] == "not_run"
        for record in audit["checkouts"]
    )


def test_structural_audit_does_not_execute_baseline_or_product(setup):
    roots, registry, release = setup
    install(setup)
    result = fleet.audit(registry, release, run_checks=True)
    assert all(
        row["structural_check"] == "passed"
        and row["governance_baseline"] == "not_observed"
        for row in result["checkouts"]
    )
    assert all(
        not (root / "baseline.ran").exists() and not (root / "product.ran").exists()
        for root in roots
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["env", "BASH_ENV=/not-read", "bash", "scripts/verify.sh"],
        ["env", "ENV=/not-read", "./scripts/verify.sh"],
        ["env", "ZDOTDIR=/not-read", "./scripts/verify.sh"],
        ["env", "PATH=/not-read", "bash", "scripts/verify.sh"],
        ["env", "PYTHONPATH=/not-read", "./scripts/verify.sh"],
        ["env", "LD_PRELOAD=/not-read", "./scripts/verify.sh"],
        ["/foreign/bash", "scripts/verify.sh"],
        ["/foreign/env", "./scripts/verify.sh"],
        ["bash", "-n", "scripts/verify.sh"],
        ["bash", "-c", "true"],
    ],
)
def test_shell_environment_and_executable_escapes_reject_before_dispatch(setup, argv):
    roots, registry, release = setup
    data = json.loads(registry.read_text())
    data["checkouts"][0]["verification"]["argv"] = argv
    save_registry(registry, data)
    planned = fleet.plan(registry, release)
    assert planned["checkouts"][0]["status"] == "blocked"
    assert (
        not (roots[0] / "product.ran").exists()
        and not (roots[0] / "baseline.ran").exists()
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["make", "-f", "Other.mk", "-f", "Makefile", "test"],
        ["make", "-f", "Makefile", "-f", "Other.mk", "test"],
        ["make", "-f", "Makefile", "-f", "Makefile", "test"],
        ["make", "-n", "test"],
        ["env", "MAKEFLAGS=-n", "make", "test"],
        ["/foreign/make", "test"],
    ],
)
def test_foreign_or_repeated_makefiles_reject(setup, argv):
    roots, registry, release = setup
    write(roots[0] / "Makefile", "test:\n\t@printf checked > product.ran\n")
    data = json.loads(registry.read_text())
    data["checkouts"][0]["verification"] = {
        "argv": argv,
        "runner": {
            "kind": "make",
            "path": "Makefile",
            "targets": ["test"],
            "governance_runner": "scripts/baseline.sh",
        },
    }
    save_registry(registry, data)
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "blocked"
    assert not (roots[0] / "product.ran").exists()


def test_ambient_startup_and_path_cannot_bypass_actual_runner(setup, monkeypatch):
    roots, registry, _release = setup
    poisoned = registry.parent / "poisoned"
    write(
        poisoned / "bash",
        "#!/bin/sh\nprintf bypass > " + str(registry.parent / "bypass.ran") + "\n",
        0o755,
    )
    write(
        poisoned / "startup",
        "printf bypass > " + str(registry.parent / "bypass.ran") + "\nexit 0\n",
    )
    install(setup)
    monkeypatch.setenv("PATH", str(poisoned))
    monkeypatch.setenv("BASH_ENV", str(poisoned / "startup"))
    monkeypatch.setenv("SHELLOPTS", "noexec")
    monkeypatch.setenv("MAKEFLAGS", "-n")
    result, _ = collect(setup, ids=["first"])
    assert result["ok"] and (roots[0] / "product.ran").exists()
    assert not (registry.parent / "bypass.ran").exists()
    env = fleet_execution.safe_environment()
    assert "BASH_ENV" not in env and "SHELLOPTS" not in env and "MAKEFLAGS" not in env
    assert env["PATH"] != "" + str(poisoned)


def test_declared_application_environment_is_retained_without_values_in_proof(
    setup, monkeypatch
):
    roots, registry, _release = setup
    write(
        roots[0] / "scripts/verify.sh",
        '#!/bin/sh\n[ "$APP_FEATURE" = required ] || exit 3\nprintf checked > product.ran\n',
        0o755,
    )
    data = json.loads(registry.read_text())
    data["checkouts"][0]["verification"]["execution_environment"] = {
        "version": fleet_support.EXECUTION_POLICY,
        "inherit": ["HOME", "APP_FEATURE", "GOVERNANCE_PYTHON"],
    }
    save_registry(registry, data)
    install(setup)
    monkeypatch.setenv("APP_FEATURE", "required")
    monkeypatch.setenv("GOVERNANCE_PYTHON", sys.executable)
    result, _ = collect(setup, ids=["first"])
    assert result["ok"]
    proof = json.dumps(result)
    assert "required" not in proof and sys.executable not in proof
    assert result["checkouts"][0]["execution"]["inherited_names"] == [
        "APP_FEATURE",
        "GOVERNANCE_PYTHON",
        "HOME",
    ]


def test_changed_product_inputs_stale_previous_report(setup):
    roots, registry, release = setup
    install(setup)
    evidence, path = collect(setup, ids=["first"])
    assert evidence["ok"]
    write(roots[0] / "README.md", "Owner updated the product authority.\n")
    audited = fleet.audit(registry, release, evidence_path=path)
    assert audited["checkouts"][0]["status"] == "incomplete"
    assert audited["checkouts"][0]["product_acceptance"] == "not_run"


@pytest.mark.parametrize(
    "original",
    [
        fleet_support.END + fleet_support.BEGIN,
        fleet_support.END + "\n" + fleet_support.BEGIN + "\n",
        "prefix" + fleet_support.BEGIN + "\nbody\n" + fleet_support.END + "\n",
        fleet_support.BEGIN + " suffix\nbody\n" + fleet_support.END + "\n",
        fleet_support.BEGIN
        + "\n"
        + fleet_support.END
        + "\n"
        + fleet_support.BEGIN
        + "\n"
        + fleet_support.END
        + "\n",
    ],
)
def test_malformed_managed_marker_grammar_rejects(original):
    with pytest.raises(fleet.FleetError):
        fleet_support.managed_block(
            original, fleet_support.BEGIN, fleet_support.END, "new"
        )


def test_declared_instruction_alias_preserves_link_through_reapply_and_undo(setup):
    roots, registry, release = setup
    root = roots[0]
    (root / "CLAUDE.md").symlink_to("./AGENTS.md")
    data = json.loads(registry.read_text())
    data["checkouts"][0]["instruction_aliases"] = {"CLAUDE.md": "./AGENTS.md"}
    save_registry(registry, data)
    original = (root / "AGENTS.md").read_bytes()
    receipt = install(setup)
    assert (root / "CLAUDE.md").is_symlink() and os.readlink(
        root / "CLAUDE.md"
    ) == "./AGENTS.md"
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "unchanged"
    assert fleet.audit(registry, release, run_checks=True)["checkouts"][0]["installed"]
    undone = fleet.undo(receipt)
    assert undone["checkouts"][0]["status"] == "undone"
    assert (root / "AGENTS.md").read_bytes() == original and os.readlink(
        root / "CLAUDE.md"
    ) == "./AGENTS.md"


def test_changed_alias_rejects_apply_before_writes(setup):
    roots, registry, release = setup
    root = roots[0]
    (root / "CLAUDE.md").symlink_to("./AGENTS.md")
    data = json.loads(registry.read_text())
    data["checkouts"][0]["instruction_aliases"] = {"CLAUDE.md": "./AGENTS.md"}
    save_registry(registry, data)
    plan = registry.parent / "plan.json"
    plan.write_bytes(fleet.canonical(fleet.plan(registry, release)))
    (root / "CLAUDE.md").unlink()
    (root / "CLAUDE.md").symlink_to("AGENTS.md")
    result = fleet.apply(plan, registry.parent / "receipt.json")
    assert (
        result["checkouts"][0]["status"] == "failed"
        and result["checkouts"][0]["writes"] == []
    )
    assert not (root / "gov").exists()


def test_changed_alias_rejects_undo_before_restoration(setup):
    roots, registry, _release = setup
    root = roots[0]
    (root / "CLAUDE.md").symlink_to("AGENTS.md")
    data = json.loads(registry.read_text())
    data["checkouts"][0]["instruction_aliases"] = {"CLAUDE.md": "AGENTS.md"}
    save_registry(registry, data)
    receipt = install(setup)
    after = (root / "AGENTS.md").read_bytes()
    (root / "CLAUDE.md").unlink()
    (root / "CLAUDE.md").symlink_to("./AGENTS.md")
    result = fleet.undo(receipt)
    assert result["checkouts"][0]["status"] == "undo_refused"
    assert (root / "AGENTS.md").read_bytes() == after and (root / "gov").exists()


@pytest.mark.parametrize(
    "target", ["../outside.md", "/external/AGENTS.md", "missing.md", "CLAUDE.md"]
)
def test_unsafe_aliases_reject(setup, target):
    roots, registry, release = setup
    root = roots[0]
    (root / "CLAUDE.md").symlink_to(target)
    data = json.loads(registry.read_text())
    data["checkouts"][0]["instruction_aliases"] = {"CLAUDE.md": target}
    save_registry(registry, data)
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "blocked"


def test_owner_doc_edit_is_preserved_and_plan_guarded(setup):
    roots, registry, release = setup
    root = roots[0]
    declaration = fleet.plan(registry, release)
    planned = registry.parent / "plan.json"
    planned.write_bytes(fleet.canonical(declaration))
    write(
        root / "AGENTS.md",
        (root / "AGENTS.md").read_text() + "Owner tightened a rule.\n",
    )
    result = fleet.apply(planned, registry.parent / "receipt.json")
    assert (
        result["checkouts"][0]["status"] == "failed"
        and result["checkouts"][0]["writes"] == []
    )
    assert "Owner tightened" in (root / "AGENTS.md").read_text()


def test_adopted_owner_doc_edits_are_not_rewritten_by_audit(setup):
    roots, registry, release = setup
    root = roots[0]
    install(setup)
    write(
        root / "AGENTS.md",
        (root / "AGENTS.md").read_text() + "Owner tightened a rule.\n",
    )
    before = (root / "AGENTS.md").read_bytes()
    result = fleet.audit(registry, release, run_checks=True)
    assert (
        result["checkouts"][0]["installed"]
        and (root / "AGENTS.md").read_bytes() == before
    )
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "unchanged"


def test_identity_disables_configured_external_and_textconv_helpers(setup, monkeypatch):
    roots, registry, _release = setup
    root = roots[0]
    marker = registry.parent / "helper.ran"
    helper = registry.parent / "diff-helper"
    write(helper, "#!/bin/sh\nprintf called > " + str(marker) + "\n", 0o755)
    write(root / ".gitattributes", "README.md diff=fixture\n")
    git(root, "config", "diff.external", str(helper))
    git(root, "config", "diff.fixture.textconv", str(helper))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(helper))
    write(root / "README.md", "First owner change.\n")
    first = fleet.git_identity(root)
    write(root / "README.md", "Second owner change.\n")
    second = fleet.git_identity(root)
    assert first["state_sha256"] != second["state_sha256"] and not marker.exists()


def test_untracked_nested_git_directory_fails_closed(setup):
    roots, _registry, _release = setup
    root = roots[0]
    child = repository(root / "nested source")
    with pytest.raises(fleet.FleetError, match="child checkout scope"):
        fleet.git_identity(root)
    write(child / "README.md", "Nested owner change.\n")
    with pytest.raises(fleet.FleetError, match="child checkout scope"):
        fleet.git_identity(root)


def test_release_verification_component_is_validated_but_never_installed(setup):
    roots, _registry, release_path = setup
    release = json.loads(release_path.read_text())
    release["schema_version"] = 2
    target = release_path.parent / "verification/tests/test_contract.py"
    write(target, "# Synthetic retained verifier\n")
    entries = [
        {
            "path": "tests/test_contract.py",
            "source": "verification/tests/test_contract.py",
            "sha256": fleet.digest(target.read_bytes()),
            "mode": 0o644,
        }
    ]
    release["verification"] = {
        "schema_version": 1,
        "identity_sha256": fleet.digest(fleet.canonical(entries)),
        "files": entries,
    }
    release_path.write_bytes(fleet.canonical(release))
    install(setup)
    assert all(not (root / "tests/test_contract.py").exists() for root in roots)
    write(target, "# altered verifier\n")
    with pytest.raises(fleet.FleetError):
        fleet.release_files(release_path)


def test_release_undeclared_component_file_and_mode_drift_reject(setup):
    _roots, _registry, release = setup
    (release.parent / "gov").chmod(0o644)
    with pytest.raises(fleet.FleetError):
        fleet.release_files(release)
    (release.parent / "gov").chmod(0o755)
    write(release.parent / "undeclared", "unowned")
    with pytest.raises(fleet.FleetError, match="Undeclared"):
        fleet.release_files(release)


def test_bounded_execution_times_out_process_group(tmp_path):
    runner = tmp_path / "hang.sh"
    marker = tmp_path / "child.ran"
    write(
        runner,
        '#!/bin/sh\n(sleep 1; printf escaped > "' + str(marker) + '") &\nsleep 5\n',
        0o755,
    )
    result = fleet_execution.run_digest(
        ["/bin/sh", str(runner)], tmp_path, fleet_execution.safe_environment(), 0.1
    )
    assert result["timed_out"] and result["exit_code"] != 0
    time.sleep(1.1)
    assert not marker.exists()


def test_arbitrary_verifier_output_is_retained_only_as_digest(setup, capsys):
    roots, _registry, _release = setup
    synthetic = "synthetic-sensitive-output-for-negative-proof"
    write(
        roots[0] / "scripts/verify.sh",
        '#!/bin/sh\nprintf "' + synthetic + '"\nprintf "' + synthetic + '" >&2\n',
        0o755,
    )
    install(setup)
    result, _ = collect(setup, ids=["first"])
    assert result["ok"] and synthetic not in json.dumps(result)
    output = capsys.readouterr()
    assert synthetic not in output.out + output.err


def native_binding(setup, text):
    roots, registry, _release = setup
    write(roots[0] / "scripts/verify.sh", text, 0o755)
    data = json.loads(registry.read_text())
    data["checkouts"][0]["verification"]["runner"].update(
        integration="native", native_sha256=fleet.digest(text.encode())
    )
    save_registry(registry, data)


def test_native_reviewed_graph_is_preserved_without_prepend(setup):
    roots, registry, release = setup
    body = (
        "#!/bin/sh\n"
        + fleet_support.VERIFY_BEGIN
        + "\n# Native named governance graph; standalone baseline remains separate.\n"
        + fleet_support.VERIFY_END
        + '\n./gov check || exit "$?"\nprintf checked > product.ran\n'
    )
    native_binding(setup, body)
    install(setup)
    assert (roots[0] / "scripts/verify.sh").read_text() == body
    assert "GOVERNANCE_REPO_ROOT" not in body
    result, _ = collect(setup, ids=["first"])
    assert result["ok"]
    write(
        roots[0] / "scripts/verify.sh",
        body + "# Owner changed the native graph.\n",
        0o755,
    )
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "blocked"


@pytest.mark.parametrize(
    "markers",
    [
        fleet_support.VERIFY_END + fleet_support.VERIFY_BEGIN,
        "prefix" + fleet_support.VERIFY_BEGIN + "\n" + fleet_support.VERIFY_END + "\n",
        fleet_support.VERIFY_BEGIN
        + "\n"
        + fleet_support.VERIFY_END
        + "\n"
        + fleet_support.VERIFY_BEGIN
        + "\n"
        + fleet_support.VERIFY_END
        + "\n",
    ],
)
def test_native_hash_cannot_bless_malformed_markers(setup, markers):
    _roots, registry, release = setup
    native_binding(setup, "#!/bin/sh\n" + markers + "\nprintf checked\n")
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "blocked"


@pytest.mark.parametrize(
    "field", ["snapshot", "execution-contract", "unsafe-make-record"]
)
def test_malformed_snapshot_and_execution_contract_are_rejected(setup, field):
    _roots, registry, release = setup
    install(setup)
    data, path = collect(setup, ids=["first"])
    record = data["checkouts"][0]
    if field == "snapshot":
        record["pre_verification"] = record["post_verification"] = None
    elif field == "execution-contract":
        record["execution"]["tool"]["mode"] = True
    else:
        record["make_environment"] = {"MAKEFLAGS": "-n"}
    path.write_bytes(fleet.canonical(data))
    result = fleet.audit(registry, release, evidence_path=path)
    assert "invalid_verification_evidence" in result["failures"]


def test_separate_baseline_attestation_is_visible_without_overwriting_product_proof(
    setup,
):
    _roots, registry, release = setup
    install(setup)
    products, product_path = collect(setup)
    original = product_path.read_bytes()
    baselines, baseline_path = collect(setup, baseline=True)
    assert (
        product_path.read_bytes() == original and baselines["ok"] and not products["ok"]
    )
    result = fleet.audit(
        registry,
        release,
        evidence_path=product_path,
        baseline_evidence_path=baseline_path,
    )
    assert result["checkouts"][0]["status"] == "accepted"
    assert all(row["governance_baseline"] == "passed" for row in result["checkouts"])
    result = fleet.audit(registry, release, baseline_evidence_path=product_path)
    assert "invalid_verification_evidence" in result["failures"]


def test_optional_diagnostic_prefix_is_bounded_and_default_records_remain_hash_only(
    tmp_path,
):
    runner = tmp_path / "output.sh"
    write(runner, "#!/bin/sh\nprintf abcdefghijkl\nprintf 987654321 >&2\n", 0o755)
    arguments = (
        ["/bin/sh", str(runner)],
        tmp_path,
        fleet_execution.safe_environment(),
        5,
    )
    ordinary = fleet_execution.run_digest(*arguments)
    assert "stdout_preview" not in ordinary and "stderr_preview" not in ordinary
    diagnostic = fleet_execution.run_digest(*arguments, diagnostic_limit=4)
    assert (
        diagnostic["stdout_preview"] == b"abcd"
        and diagnostic["stderr_preview"] == b"9876"
    )
    assert diagnostic["stdout_bytes"] == 12 and diagnostic["stderr_bytes"] == 9
    assert diagnostic["stdout_sha256"] == fleet.digest(b"abcdefghijkl")


@pytest.mark.parametrize("limit", [-1, True, 128 * 1024 + 1, 1.5])
def test_invalid_diagnostic_limits_reject_before_start(tmp_path, limit):
    with pytest.raises(ValueError):
        fleet_execution.run_digest(
            ["/never-invoked"], tmp_path, {}, 5, diagnostic_limit=limit
        )
