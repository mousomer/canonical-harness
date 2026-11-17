from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest
from tools.workspace_governance import fleet


def write(path: Path, data: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    path.chmod(mode)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def repository(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "--initial-branch=main")
    git(path, "config", "user.name", "Fixture")
    git(path, "config", "user.email", "fixture@example.invalid")
    write(path / "AGENTS.md", "# Existing authority\nPreserve stronger checks.\n")
    write(
        path / "scripts/verify.sh",
        "#!/bin/sh\nprintf 'product ran\\n' > product-ran\n",
        0o755,
    )
    write(path / "README.md", "Product authority.\n")
    write(
        path / ".gitignore",
        "# Only generated verifier fixtures; product inputs remain visible.\n/product-ran\n/native-ran\n/make-product-ran\n/make-baseline-ran\n/ignored-build/\n",
    )
    git(path, "add", ".")
    git(path, "commit", "-m", "Fixture baseline")
    return path


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = repository(tmp_path / "consumer with spaces")
    release_root = tmp_path / "release"
    write(
        release_root / "gov",
        '#!/bin/sh\n[ "$1" = check ] || exit 2\n[ ! -f fail-gov ]\n',
        0o755,
    )
    release = {
        "schema_version": 1,
        "name": "workspace-governance",
        "version": "test-1",
        "revision": "immutable-fixture-1",
        "release_root": ".",
        "files": [
            {
                "source": "gov",
                "path": "gov",
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
                "id": "consumer",
                "path": str(root),
                "repository_id": "fixture-repository",
                "integration_mode": "direct",
                "classification": "workspace_consumer_candidate",
                "profile": "native",
                "authorities": ["README.md"],
                "verification": {
                    "argv": ["./scripts/verify.sh"],
                    "runner": {"path": "scripts/verify.sh", "kind": "shell"},
                    "source": "fixture README",
                },
            }
        ],
    }
    registry_path = tmp_path / "registry.json"
    registry_path.write_bytes(fleet.canonical(registry))
    return root, registry_path, release_path


def apply_fixture(setup: tuple[Path, Path, Path]) -> tuple[Path, dict]:
    _root, registry, release = setup
    plan_path = registry.parent / "plan.json"
    plan_path.write_bytes(fleet.canonical(fleet.plan(registry, release)))
    receipt = registry.parent / "receipt.json"
    result = fleet.apply(plan_path, receipt)
    assert result["checkouts"][0]["status"] == "installed"
    return receipt, result


def evidence_for(root: Path, registry: Path, release: Path) -> Path:
    row = json.loads(registry.read_text())["checkouts"][0]
    path = registry.parent / "evidence.json"
    result = fleet.collect_acceptance(
        registry, release, checkout_ids=[row["id"]], output_path=path
    )
    if row["verification"].get("coverage") != "governance-only":
        assert result["ok"], result["failures"]
    return path


def test_preserves_instructions_and_wires_existing_runner(
    setup: tuple[Path, Path, Path],
) -> None:
    root, registry, release = setup
    original = (root / "scripts/verify.sh").read_text()
    apply_fixture(setup)
    assert "Preserve stronger checks." in (root / "AGENTS.md").read_text()
    assert (root / "AGENTS.md").read_text().count(fleet.BEGIN) == 1
    assert original.split("\n", 1)[1] in (root / "scripts/verify.sh").read_text()
    evidence = evidence_for(root, registry, release)
    accepted = fleet.audit(registry, release, run_checks=True, evidence_path=evidence)
    assert accepted["ok"]
    assert accepted["checkouts"][0]["product_acceptance"] == "collector_reported_passed"
    write(root / "fail-gov", "fixture failure")
    (root / "product-ran").unlink()
    failed = subprocess.run(
        ["./scripts/verify.sh"], cwd=root, capture_output=True, check=False
    )
    assert failed.returncode != 0
    assert not (root / "product-ran").exists()


def test_idempotent_application_and_guarded_undo(
    setup: tuple[Path, Path, Path],
) -> None:
    root, registry, release = setup
    original_agents = (root / "AGENTS.md").read_bytes()
    original_runner = (root / "scripts/verify.sh").read_bytes()
    receipt, _ = apply_fixture(setup)
    second = fleet.plan(registry, release)
    assert second["checkouts"][0]["status"] == "unchanged"
    second_path = registry.parent / "second-plan.json"
    second_path.write_bytes(fleet.canonical(second))
    assert (
        fleet.apply(second_path, registry.parent / "second-receipt.json")["checkouts"][
            0
        ]["status"]
        == "unchanged"
    )
    undone = fleet.undo(receipt)
    assert undone["checkouts"][0]["status"] == "undone"
    assert (root / "AGENTS.md").read_bytes() == original_agents
    assert (root / "scripts/verify.sh").read_bytes() == original_runner
    assert not (root / "gov").exists()
    assert fleet.undo(receipt)["checkouts"][0]["status"] == "undone"


def test_dirty_preimage_and_existing_release_files_are_preserved(
    setup: tuple[Path, Path, Path],
) -> None:
    root, registry, release = setup
    write(root / "AGENTS.md", "Uncommitted stronger obligations.\n")
    declaration = fleet.plan(registry, release)
    assert declaration["checkouts"][0]["status"] == "ready"
    write(root / "AGENTS.md", "Later user edit.\n")
    plan_path = registry.parent / "plan.json"
    plan_path.write_bytes(fleet.canonical(declaration))
    result = fleet.apply(plan_path, registry.parent / "receipt.json")
    assert result["checkouts"][0]["status"] == "failed"
    assert result["checkouts"][0]["writes"] == []
    assert not (root / "gov").exists()
    assert (root / "AGENTS.md").read_text() == "Later user edit.\n"
    write(root / "gov", "Existing meaningful launcher\n", 0o755)
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "blocked"
    value = json.loads(registry.read_text())
    value["checkouts"][0]["replace_sha256"] = {
        "gov": fleet.digest((root / "gov").read_bytes())
    }
    registry.write_bytes(fleet.canonical(value))
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "ready"


def test_rejects_release_and_plan_tampering(setup: tuple[Path, Path, Path]) -> None:
    _, registry, release = setup
    declaration = fleet.plan(registry, release)
    path = registry.parent / "plan.json"
    path.write_bytes(fleet.canonical(declaration))
    write(release.parent / "gov", "Different immutable bytes\n", 0o755)
    with pytest.raises(fleet.FleetError, match="byte mismatch"):
        fleet.apply(path, registry.parent / "receipt.json")


def test_rejects_symlink_before_and_after_plan(setup: tuple[Path, Path, Path]) -> None:
    root, registry, release = setup
    declaration = fleet.plan(registry, release)
    outside = root.parent / "outside"
    outside.mkdir()
    (root / ".governance").symlink_to(outside, target_is_directory=True)
    path = registry.parent / "plan.json"
    path.write_bytes(fleet.canonical(declaration))
    result = fleet.apply(path, registry.parent / "receipt.json")
    assert result["checkouts"][0]["status"] == "failed"
    assert not list(outside.iterdir())
    assert not (root / "gov").exists()
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "blocked"


@pytest.mark.parametrize(
    "value",
    [
        "../outside",
        "/absolute",
        "nested/../../outside",
        ".git/config",
        "nested/.git/config",
        "a\\b",
        "a//b",
        ".",
    ],
)
def test_rejects_unsafe_managed_paths(value: str) -> None:
    with pytest.raises(fleet.FleetError):
        fleet.relative(value)


def test_rolls_back_only_failed_checkout(
    setup: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, registry, release = setup
    original = (root / "AGENTS.md").read_bytes()
    declaration = fleet.plan(registry, release)
    plan_path = registry.parent / "plan.json"
    plan_path.write_bytes(fleet.canonical(declaration))
    original_atomic = fleet.atomic

    def failing_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
        if path == root / "CLAUDE.md":
            raise OSError("Fixture write failure")
        original_atomic(path, data, mode)

    monkeypatch.setattr(fleet, "atomic", failing_atomic)
    result = fleet.apply(plan_path, registry.parent / "receipt.json")
    assert result["checkouts"][0]["status"] == "failed"
    assert not (root / "gov").exists()
    assert (root / "AGENTS.md").read_bytes() == original
    assert all(
        item["status"] == "rolled_back"
        for item in result["checkouts"][0]["writes"][:-1]
    )


def test_undo_refuses_intervening_user_edit(setup: tuple[Path, Path, Path]) -> None:
    root, _, _ = setup
    receipt, _ = apply_fixture(setup)
    write(root / "AGENTS.md", "User changed authority after install.\n")
    result = fleet.undo(receipt)
    assert result["checkouts"][0]["status"] == "undo_refused"
    assert (root / "AGENTS.md").read_text() == "User changed authority after install.\n"
    assert not (root / "gov").exists()


def test_protected_repositories_and_scan_gaps_cannot_be_hidden(
    setup: tuple[Path, Path, Path],
) -> None:
    root, registry, release = setup
    value = json.loads(registry.read_text())
    value["checkouts"][0]["classification"] = "test_fixture_repository"
    registry.write_bytes(fleet.canonical(value))
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "blocked"
    value["checkouts"][0]["integration_mode"] = "external"
    value["checkouts"][0]["external_policy"] = {
        "mode": "read-only",
        "owner": "workspace pack",
        "reason": "Preserve fixture evidence",
        "release_manifest_sha256": fleet.release_files(release)[1],
    }
    value["scope"] = {
        "whole_computer_coverage": False,
        "scan_errors": [{"path": "unreadable"}],
    }
    registry.write_bytes(fleet.canonical(value))
    result = fleet.audit(registry, release)
    assert not result["ok"]
    assert result["checkouts"][0]["status"] == "governed_read_only"
    assert result["checkouts"][0]["installed"] is False
    assert "scan_scope_incomplete" in result["failures"]
    assert not (root / "gov").exists()


def test_audit_separates_installation_from_acceptance_and_rejects_stale_evidence(
    setup: tuple[Path, Path, Path],
) -> None:
    root, registry, release = setup
    apply_fixture(setup)
    result = fleet.audit(registry, release, run_checks=True)
    assert result["checkouts"][0]["installed"]
    assert not result["ok"]
    evidence = evidence_for(root, registry, release)
    write(root / "README.md", "Different product behaviour authority.\n")
    assert not fleet.audit(registry, release, run_checks=True, evidence_path=evidence)[
        "ok"
    ]
    assert (
        fleet.main(
            [
                "audit",
                "--registry",
                str(registry),
                "--release",
                str(release),
                "--run-checks",
            ]
        )
        == 1
    )


def test_make_target_hook_is_required_and_repeatable(
    setup: tuple[Path, Path, Path],
) -> None:
    root, registry, release = setup
    write(
        root / "Makefile",
        "test:\n\t@printf 'native ran\\n' > native-ran\n\ntest-sanitize:\n\t@true\n",
    )
    value = json.loads(registry.read_text())
    value["checkouts"][0]["verification"] = {
        "argv": ["make", "test"],
        "runner": {
            "path": "Makefile",
            "kind": "make",
            "targets": ["test", "test-sanitize"],
        },
    }
    registry.write_bytes(fleet.canonical(value))
    apply_fixture(setup)
    write(root / "fail-gov", "Fixture failure")
    result = subprocess.run(
        ["make", "test"], cwd=root, capture_output=True, check=False
    )
    assert result.returncode != 0
    assert not (root / "native-ran").exists()
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "unchanged"


def test_linked_worktree_and_nested_repository_have_distinct_checkout_plans(
    setup: tuple[Path, Path, Path],
) -> None:
    root, registry, release = setup
    linked = root.parent / "linked worktree"
    git(root, "worktree", "add", "-b", "linked-fixture", str(linked))
    nested = repository(root / "nested independent")
    value = json.loads(registry.read_text())
    linked_row = copy.deepcopy(value["checkouts"][0])
    linked_row.update(
        id="linked", path=str(linked), integration_mode="inherited_checkout"
    )
    nested_row = copy.deepcopy(value["checkouts"][0])
    nested_row.update(id="nested", path=str(nested), repository_id="nested-independent")
    value["checkouts"].extend([linked_row, nested_row])
    registry.write_bytes(fleet.canonical(value))
    result = fleet.plan(registry, release)
    assert [item["status"] for item in result["checkouts"]] == [
        "blocked",
        "ready",
        "ready",
    ]
    assert "child checkout scope" in result["checkouts"][0]["errors"][0].lower()
    assert (
        result["checkouts"][1]["git"]["common_dir_sha256"]
        != result["checkouts"][2]["git"]["common_dir_sha256"]
    )


def test_atomic_write_never_follows_a_symlinked_parent(tmp_path: Path) -> None:
    from tools.workspace_governance.fleet_support import atomic

    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        atomic(alias / "escaped.txt", b"must not escape")
    assert not (outside / "escaped.txt").exists()


def test_cli_does_not_print_preserved_bytes(
    setup: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    root, registry, release = setup
    write(
        root / "AGENTS.md", "Private consumer content that belongs only in backups.\n"
    )
    path = registry.parent / "plan.json"
    assert (
        fleet.main(
            [
                "plan",
                "--registry",
                str(registry),
                "--release",
                str(release),
                "--output",
                str(path),
            ]
        )
        == 0
    )
    assert "content_base64" not in capsys.readouterr().out
    assert (
        fleet.main(
            [
                "apply",
                "--plan",
                str(path),
                "--receipt",
                str(registry.parent / "receipt.json"),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "backup_base64" not in output
    assert "Private consumer content" not in output


def test_apply_rejects_plan_location_omissions(setup: tuple[Path, Path, Path]) -> None:
    _, registry, release = setup
    declaration = fleet.plan(registry, release)
    declaration["checkouts"] = []
    path = registry.parent / "incomplete-plan.json"
    path.write_bytes(fleet.canonical(declaration))
    with pytest.raises(fleet.FleetError, match="every registry checkout"):
        fleet.apply(path, registry.parent / "receipt.json")


def test_apply_rejects_plan_mode_tampering(setup: tuple[Path, Path, Path]) -> None:
    root, registry, release = setup
    declaration = fleet.plan(registry, release)
    declaration["checkouts"][0]["operations"][0]["after"]["mode"] = 0o777
    path = registry.parent / "tampered-plan.json"
    path.write_bytes(fleet.canonical(declaration))
    result = fleet.apply(path, registry.parent / "receipt.json")
    assert result["checkouts"][0]["status"] == "failed"
    assert not (root / "gov").exists()


def test_required_governance_runner_blocks_product_on_failed_baseline(setup):
    root, registry_path, release_path = setup
    registry = fleet.load(registry_path)
    row = registry["checkouts"][0]
    row["verification"]["runner"]["governance_runner"] = "scripts/verify-governance.sh"
    row["managed_files"] = [
        {
            "path": "scripts/verify-governance.sh",
            "content": "#!/bin/sh\nexit 19\n",
            "mode": 0o755,
        }
    ]
    registry_path.write_bytes(fleet.canonical(registry))
    planned = fleet.plan(registry_path, release_path)
    plan_path = root.parent / "plan.json"
    plan_path.write_bytes(fleet.canonical(planned))
    fleet.apply(plan_path, root.parent / "receipt.json")
    result = subprocess.run(
        [str(root / "scripts/verify.sh")], cwd=root, capture_output=True, check=False
    )
    assert result.returncode == 19
    assert not (root / "product-ran").exists()


def test_untracked_source_bytes_invalidate_product_acceptance(setup):
    root, registry, release = setup
    write(root / "new-source.py", "answer = 1\n")
    apply_fixture(setup)
    evidence = evidence_for(root, registry, release)
    before_status = git(root, "status", "--porcelain")
    previous = fleet.git_identity(root)
    write(root / "new-source.py", "answer = 2\n")
    assert git(root, "status", "--porcelain") == before_status
    assert fleet.git_identity(root)["state_sha256"] != previous["state_sha256"]
    assert not fleet.audit(registry, release, run_checks=True, evidence_path=evidence)[
        "ok"
    ]


def test_untracked_types_modes_and_link_targets_affect_identity(setup):
    root, _, _ = setup
    write(root / "local-source", "same bytes\n")
    first = fleet.git_identity(root)["state_sha256"]
    (root / "local-source").chmod(0o755)
    second = fleet.git_identity(root)["state_sha256"]
    assert first != second
    (root / "local-source").unlink()
    (root / "local-source").symlink_to("first-unread-target")
    third = fleet.git_identity(root)["state_sha256"]
    assert second != third
    (root / "local-source").unlink()
    (root / "local-source").symlink_to("another-unread-target")
    assert third != fleet.git_identity(root)["state_sha256"]


def test_large_untracked_content_is_streamed_without_path_read_bytes(
    setup, monkeypatch
):
    root, _, _ = setup
    large = root / "large-source.bin"
    with large.open("wb") as stream:
        for _ in range(10):
            stream.write(b"source bytes\n" * 100_000)
    original = Path.read_bytes

    def refusing_read_bytes(path):
        if path == large:
            raise AssertionError("Large source must be streamed")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", refusing_read_bytes)
    assert fleet.git_identity(root)["untracked_entries"] >= 1


@pytest.mark.parametrize(
    "argv",
    [
        ["./scripts/verify.sh"],
        ["sh", "scripts/verify.sh"],
        ["bash", "-eu", "./scripts/verify.sh"],
        ["env", "CODEX_MODE=1", "./scripts/verify.sh"],
    ],
)
def test_product_argv_is_bound_to_real_shell_runner(setup, argv):
    _, registry, release = setup
    value = fleet.load(registry)
    value["checkouts"][0]["verification"]["argv"] = argv
    registry.write_bytes(fleet.canonical(value))
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "ready"


@pytest.mark.parametrize(
    "argv", [["true"], ["bash", "-c", "./scripts/verify.sh"], ["./other-verifier"]]
)
def test_shell_runner_cannot_accept_bypassing_command(setup, argv):
    _, registry, release = setup
    value = fleet.load(registry)
    value["checkouts"][0]["verification"]["argv"] = argv
    registry.write_bytes(fleet.canonical(value))
    planned = fleet.plan(registry, release)["checkouts"][0]
    assert planned["status"] == "blocked"
    assert "does not invoke the hooked runner" in planned["errors"][0]


@pytest.mark.parametrize(
    "argv,ready",
    [
        (["make", "test"], True),
        (["make", "-j2", "test", "test-sanitize"], True),
        (["env", "CC=cc", "make", "test-sanitize"], True),
        (["make", "clean"], False),
        (["make", "-n", "test"], False),
        (["make", "-f", "Other.mk", "test"], False),
        (["env", "MAKEFLAGS=-n", "make", "test"], False),
    ],
)
def test_make_argv_must_run_hooked_targets_without_bypass(setup, argv, ready):
    root, registry, release = setup
    write(
        root / "Makefile",
        "test:\n\t@true\n\ntest-sanitize:\n\t@true\n\nclean:\n\t@true\n",
    )
    value = fleet.load(registry)
    value["checkouts"][0]["verification"] = {
        "argv": argv,
        "runner": {
            "path": "Makefile",
            "kind": "make",
            "targets": ["test", "test-sanitize"],
        },
    }
    registry.write_bytes(fleet.canonical(value))
    assert (fleet.plan(registry, release)["checkouts"][0]["status"] == "ready") is ready


def test_apply_rechecks_registry_replacement_permission_after_plan_tampering(setup):
    root, registry, release = setup
    declaration = fleet.plan(registry, release)
    write(root / "gov", "Meaningful local launcher added after planning.\n", 0o755)
    operation = next(
        op for op in declaration["checkouts"][0]["operations"] if op["path"] == "gov"
    )
    operation["before"] = fleet.state(root / "gov")
    path = registry.parent / "tampered-preimage-plan.json"
    path.write_bytes(fleet.canonical(declaration))
    result = fleet.apply(path, registry.parent / "receipt.json")
    assert result["checkouts"][0]["status"] == "failed"
    assert (
        "Registry does not authorize replacing: gov" in result["checkouts"][0]["errors"]
    )
    assert (
        root / "gov"
    ).read_text() == "Meaningful local launcher added after planning.\n"
    assert result["checkouts"][0]["writes"] == []


def test_created_governance_runner_never_claims_product_acceptance(setup):
    root, registry, release = setup
    value = fleet.load(registry)
    value["checkouts"][0]["verification"] = {
        "argv": ["./scripts/new-verifier.sh"],
        "coverage": "governance-only",
        "runner": {
            "path": "scripts/new-verifier.sh",
            "kind": "shell",
            "seed_content": "#!/bin/sh\ntrue\n",
        },
    }
    value["checkouts"][0]["acceptance_required"] = False
    registry.write_bytes(fleet.canonical(value))
    apply_fixture(setup)
    assert (root / "scripts/new-verifier.sh").is_file()
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "unchanged"
    evidence = evidence_for(root, registry, release)
    result = fleet.audit(registry, release, run_checks=True, evidence_path=evidence)
    assert result["checkouts"][0]["installed"]
    assert result["checkouts"][0]["structural_check"] == "passed"
    assert result["checkouts"][0]["governance_baseline"] == "passed"
    assert result["checkouts"][0]["product_acceptance"] == "not_run"
    assert not result["ok"]
    assert "coverage is governance-only" in result["checkouts"][0]["errors"][0]


def test_runner_creation_requires_honest_coverage_and_single_owner(setup):
    _, registry, release = setup
    value = fleet.load(registry)
    verification = value["checkouts"][0]["verification"]
    verification["argv"] = ["./scripts/new-verifier.sh"]
    verification["runner"] = {
        "path": "scripts/new-verifier.sh",
        "kind": "shell",
        "seed_content": "#!/bin/sh\ntrue\n",
    }
    registry.write_bytes(fleet.canonical(value))
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "blocked"
    verification["coverage"] = "governance-only"
    value["checkouts"][0]["managed_files"] = [
        {
            "path": "scripts/new-verifier.sh",
            "content": "#!/bin/sh\ntrue\n",
            "mode": 0o755,
        }
    ]
    registry.write_bytes(fleet.canonical(value))
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "blocked"


def test_unborn_repository_has_explicit_unknown_head_and_usable_plan(setup):
    root, registry, release = setup
    unborn = root.parent / "unborn checkout"
    unborn.mkdir()
    git(unborn, "init", "--initial-branch=main")
    write(unborn / "scripts/verify.sh", "#!/bin/sh\ntrue\n", 0o755)
    value = fleet.load(registry)
    value["checkouts"][0]["path"] = str(unborn)
    registry.write_bytes(fleet.canonical(value))
    identity = fleet.git_identity(unborn)
    assert identity["head"] is None
    assert identity["head_state"] == "unborn"
    planned = fleet.plan(registry, release)
    assert planned["checkouts"][0]["status"] == "ready"


def test_root_shell_runner_requires_explicit_invocation_path(tmp_path):
    from tools.workspace_governance.fleet_support import validate_verification_argv

    verification = {
        "argv": ["verify.sh"],
        "runner": {"path": "verify.sh", "kind": "shell"},
    }
    with pytest.raises(fleet.FleetError, match="does not invoke"):
        validate_verification_argv(verification, tmp_path)
    write(tmp_path / "verify.sh", "#!/bin/sh\nexit 0\n", 0o755)
    verification["argv"] = ["./verify.sh"]
    validate_verification_argv(verification, tmp_path)
    verification["argv"] = ["bash", "verify.sh"]
    validate_verification_argv(verification, tmp_path)


def make_consumer(setup):
    root, registry, release = setup
    write(root / "Makefile", "test:\n\t@printf 'product ran\\n' > make-product-ran\n")
    value = fleet.load(registry)
    value["checkouts"][0]["verification"] = {
        "argv": ["make", "test"],
        "runner": {
            "path": "Makefile",
            "kind": "make",
            "targets": ["test"],
            "governance_runner": "scripts/verify-governance.sh",
        },
    }
    value["checkouts"][0]["managed_files"] = [
        {
            "path": "scripts/verify-governance.sh",
            "content": "#!/bin/sh\nprintf 'baseline ran\\n' >> make-baseline-ran\n./gov check\n",
            "mode": 0o755,
        }
    ]
    registry.write_bytes(fleet.canonical(value))
    apply_fixture(setup)
    return root, registry, release


def test_collector_removes_ambient_make_overrides_and_executes_real_checks(
    setup, monkeypatch
):
    root, registry, release = make_consumer(setup)
    for key, value in {
        "MAKEFLAGS": "-n",
        "MFLAGS": "-n",
        "MAKEFILES": "injected.mk",
        "GNUMAKEFLAGS": "-n",
    }.items():
        monkeypatch.setenv(key, value)
    evidence = evidence_for(root, registry, release)
    assert (root / "make-product-ran").read_text() == "product ran\n"
    assert (root / "make-baseline-ran").read_text().count("baseline ran") >= 2
    record = json.loads(evidence.read_text())["checkouts"][0]
    assert record["make_environment"] == {}
    assert record["collector_id"] == "workspace-governance-fleet-verify-v2"
    assert record["exit_code"] == 0
    assert record["governance_exit_code"] == 0
    assert fleet.audit(registry, release, run_checks=True, evidence_path=evidence)["ok"]


@pytest.mark.parametrize(
    "change", ["missing_environment", "unsafe_environment", "missing_collector"]
)
def test_make_historical_evidence_without_safe_collector_is_rejected(setup, change):
    root, registry, release = make_consumer(setup)
    evidence = evidence_for(root, registry, release)
    value = json.loads(evidence.read_text())
    record = value["checkouts"][0]
    if change == "missing_environment":
        record["execution"].pop("inherited_names")
    elif change == "unsafe_environment":
        record["execution"]["inherited_names"] = ["MAKEFLAGS"]
    else:
        record.pop("collector_id")
    evidence.write_bytes(fleet.canonical(value))
    assert not fleet.audit(registry, release, run_checks=True, evidence_path=evidence)[
        "ok"
    ]


def test_collector_records_failed_execution_without_product_acceptance(setup):
    root, registry, release = setup
    write(
        root / "scripts/verify.sh",
        "#!/bin/sh\nprintf 'sensitive-output-fixture\\n'\nexit 17\n",
        0o755,
    )
    apply_fixture(setup)
    result = fleet.collect_acceptance(registry, release)
    record = result["checkouts"][0]
    assert not result["ok"]
    assert record["exit_code"] == 17
    assert record["stdout_bytes"] > 0
    assert len(record["stdout_sha256"]) == 64
    assert "sensitive-output-fixture" not in json.dumps(result)
    evidence = registry.parent / "failed-evidence.json"
    evidence.write_bytes(fleet.canonical(result))
    assert not fleet.audit(registry, release, run_checks=True, evidence_path=evidence)[
        "ok"
    ]


def test_collector_timeout_has_bounded_output_and_is_not_accepted(setup):
    root, registry, release = setup
    write(
        root / "scripts/verify.sh",
        "#!/bin/sh\nprintf 'timeout-output-fixture\\n'\nsleep 5\n",
        0o755,
    )
    value = fleet.load(registry)
    value["checkouts"][0]["verification"]["timeout_seconds"] = 0.2
    registry.write_bytes(fleet.canonical(value))
    apply_fixture(setup)
    result = fleet.collect_acceptance(registry, release)
    assert not result["ok"]
    assert result["checkouts"][0]["timed_out"]
    assert result["checkouts"][0]["duration_seconds"] < 3
    assert "timeout-output-fixture" not in json.dumps(result)


def test_collector_skips_governance_only_product_command(setup):
    root, registry, release = setup
    value = fleet.load(registry)
    value["checkouts"][0]["verification"] = {
        "argv": ["./scripts/new-verifier.sh"],
        "coverage": "governance-only",
        "runner": {
            "path": "scripts/new-verifier.sh",
            "kind": "shell",
            "seed_content": "#!/bin/sh\nprintf wrong > product-should-not-run\n",
        },
    }
    registry.write_bytes(fleet.canonical(value))
    apply_fixture(setup)
    result = fleet.collect_acceptance(registry, release)
    assert not result["ok"]
    assert result["checkouts"][0]["governance_exit_code"] == 0
    assert result["checkouts"][0]["exit_code"] is None
    assert not (root / "product-should-not-run").exists()


def test_collector_cli_selects_only_requested_checkout(setup, capsys):
    root, registry, release = setup
    sibling = repository(root.parent / "second consumer")
    value = fleet.load(registry)
    second = copy.deepcopy(value["checkouts"][0])
    second.update(id="second", path=str(sibling), repository_id="second-repository")
    value["checkouts"].append(second)
    registry.write_bytes(fleet.canonical(value))
    apply_fixture(setup)
    evidence = registry.parent / "selected-evidence.json"
    assert (
        fleet.main(
            [
                "verify",
                "--registry",
                str(registry),
                "--release",
                str(release),
                "--output",
                str(evidence),
                "--checkout",
                "consumer",
            ]
        )
        == 0
    )
    result = json.loads(evidence.read_text())
    assert [row["id"] for row in result["checkouts"]] == ["consumer"]
    assert (root / "product-ran").exists()
    assert not (sibling / "product-ran").exists()
    assert "product ran" not in capsys.readouterr().out


def test_collector_rejects_in_repo_evidence_before_execution(setup):
    root, registry, release = setup
    apply_fixture(setup)
    with pytest.raises(fleet.FleetError, match="outside consumer"):
        fleet.collect_acceptance(registry, release, output_path=root / "evidence.json")
    assert not (root / "product-ran").exists()


def test_default_makefile_selection_cannot_bypass_declared_hook(setup):
    root, registry, release = setup
    write(root / "Makefile", "test:\n\t@true\n")
    write(root / "GNUmakefile", "test:\n\t@true\n")
    value = fleet.load(registry)
    value["checkouts"][0]["verification"] = {
        "argv": ["make", "test"],
        "runner": {"path": "Makefile", "kind": "make", "targets": ["test"]},
    }
    registry.write_bytes(fleet.canonical(value))
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "blocked"
    value["checkouts"][0]["verification"]["argv"] = ["make", "-f", "Makefile", "test"]
    registry.write_bytes(fleet.canonical(value))
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "ready"


@pytest.mark.parametrize("tracked", [True, False])
def test_collector_rejects_product_input_mutation_during_successful_verification(
    setup, tracked
):
    root, registry, release = setup
    write(root / "product.py", "VERSION = 1\n")
    if tracked:
        git(root, "add", "product.py")
        git(root, "commit", "-m", "Declare product input")
    write(
        root / "scripts/verify.sh",
        "#!/bin/sh\ngrep -q 'VERSION = 1' product.py || exit 1\nprintf 'VERSION = 2\\n' > product.py\n",
        0o755,
    )
    apply_fixture(setup)
    evidence = registry.parent / "mutated-input-evidence.json"
    result = fleet.collect_acceptance(registry, release, output_path=evidence)
    record = result["checkouts"][0]
    assert record["exit_code"] == 0
    assert record["input_state_stable"] is False
    assert (
        record["pre_verification"]["state_sha256"]
        != record["post_verification"]["state_sha256"]
    )
    assert record["status"] == "failed"
    assert not result["ok"]
    assert (root / "product.py").read_text() == "VERSION = 2\n"
    assert not fleet.audit(registry, release, run_checks=True, evidence_path=evidence)[
        "ok"
    ]


def test_collector_accepts_ignored_build_output_without_input_change(setup):
    root, registry, release = setup
    write(root / "product.py", "VERSION = 1\n")
    write(
        root / "scripts/verify.sh",
        "#!/bin/sh\ngrep -q 'VERSION = 1' product.py || exit 1\nmkdir -p ignored-build\nprintf 'generated artifact\\n' > ignored-build/result.bin\n",
        0o755,
    )
    apply_fixture(setup)
    evidence = evidence_for(root, registry, release)
    record = json.loads(evidence.read_text())["checkouts"][0]
    assert record["input_state_stable"] is True
    assert record["pre_verification"] == record["post_verification"]
    assert (root / "ignored-build/result.bin").is_file()
    assert fleet.audit(registry, release, run_checks=True, evidence_path=evidence)["ok"]


def test_collector_changed_binding_retains_both_identities_and_is_unaccepted(setup):
    root, registry, release = setup
    write(
        root / "scripts/verify.sh",
        "#!/bin/sh\nprintf '{}\\n' > .governance/fleet.json\n",
        0o755,
    )
    apply_fixture(setup)
    result = fleet.collect_acceptance(registry, release)
    record = result["checkouts"][0]
    assert record["exit_code"] == 0
    assert not record["input_state_stable"]
    assert (
        record["pre_verification"]["binding_sha256"]
        != record["post_verification"]["binding_sha256"]
    )
    assert not result["ok"]


def test_historical_post_tree_evidence_without_stability_proof_is_rejected(setup):
    root, registry, release = setup
    apply_fixture(setup)
    evidence = evidence_for(root, registry, release)
    value = json.loads(evidence.read_text())
    for key in ("pre_verification", "post_verification", "input_state_stable"):
        value["checkouts"][0].pop(key)
    evidence.write_bytes(fleet.canonical(value))
    assert not fleet.audit(registry, release, run_checks=True, evidence_path=evidence)[
        "ok"
    ]


def absence_fixture(setup, tmp_path):
    _root, registry, release = setup
    value = json.loads(registry.read_text())
    row = value["checkouts"][0]
    row.update(integration_mode="external", path=str(tmp_path / "retired-store"))
    row["external_policy"] = {
        "mode": "read-only",
        "owner": "application-owner",
        "reason": "retired temporary store",
        "release_manifest_sha256": fleet.digest(
            fleet.canonical(json.loads(release.read_text()))
        ),
        "observed_root_validity": "resolved",
        "observed_git_head": "a" * 40,
        "lifecycle": {
            "state": "retired_absent",
            "recorded_at": "2026-01-01T00:00:00Z",
            "evidence_ref": "observations/store-retirement.json",
        },
    }
    registry.write_bytes(fleet.canonical(value))
    return value, row, registry, release


def test_explicit_retired_store_is_freshly_absent_without_installation_or_product_proof(
    setup, tmp_path
):
    _value, _row, registry, release = absence_fixture(setup, tmp_path)
    planned = fleet.plan(registry, release)["checkouts"][0]
    assert planned["status"] == "external" and planned["operations"] == []
    result = fleet.audit(registry, release)
    assert result["ok"]
    record = result["checkouts"][0]
    assert record["status"] == "governed_absent"
    assert record["observation"]["location_exists"] is False
    assert record["observation"]["observed_at"]
    assert not record["installed"]
    assert record["product_acceptance"] == "not_run"


@pytest.mark.parametrize("kind", ["file", "directory", "broken-symlink"])
def test_retired_store_reappearance_fails_closed(setup, tmp_path, kind):
    _value, row, registry, release = absence_fixture(setup, tmp_path)
    root = Path(row["path"])
    if kind == "file":
        root.write_text("new owner input")
    elif kind == "directory":
        root.mkdir()
    else:
        root.symlink_to(tmp_path / "missing-target")
    result = fleet.audit(registry, release)
    assert not result["ok"]
    assert "reappeared" in result["checkouts"][0]["errors"][0]


@pytest.mark.parametrize(
    "change",
    [
        "unknown-state",
        "missing-time",
        "naive-time",
        "future-time",
        "private-path",
        "traversal",
        "unknown-field",
        "unresolved",
        "missing-head",
        "no-lifecycle",
    ],
)
def test_missing_or_malformed_absence_authority_never_passes(setup, tmp_path, change):
    value, row, registry, release = absence_fixture(setup, tmp_path)
    policy = row["external_policy"]
    lifecycle = policy["lifecycle"]
    if change == "unknown-state":
        lifecycle["state"] = "accepted"
    elif change == "missing-time":
        lifecycle.pop("recorded_at")
    elif change == "naive-time":
        lifecycle["recorded_at"] = "2026-01-01T00:00:00"
    elif change == "future-time":
        lifecycle["recorded_at"] = "9999-01-01T00:00:00Z"
    elif change == "private-path":
        lifecycle["evidence_ref"] = "/private/owner-record.json"
    elif change == "traversal":
        lifecycle["evidence_ref"] = "../owner-record.json"
    elif change == "unknown-field":
        lifecycle["accepted"] = True
    elif change == "unresolved":
        policy["observed_root_validity"] = "unresolved_git_marker"
    elif change == "missing-head":
        policy["observed_git_head"] = None
    else:
        policy.pop("lifecycle")
    registry.write_bytes(fleet.canonical(value))
    assert not fleet.audit(registry, release)["ok"]


def test_absence_observation_preserves_scan_failure_and_permission_failure(
    setup, tmp_path, monkeypatch
):
    value, row, registry, release = absence_fixture(setup, tmp_path)
    value["scope"]["scan_errors"] = ["unreadable root"]
    registry.write_bytes(fleet.canonical(value))
    result = fleet.audit(registry, release)
    assert not result["ok"] and "unresolved_scan_evidence" in result["failures"]
    original = Path.lstat

    def refused(path):
        if str(path) == row["path"]:
            raise PermissionError("synthetic unreadable store")
        return original(path)

    monkeypatch.setattr(Path, "lstat", refused)
    assert fleet.audit(registry, release)["checkouts"][0]["status"] == "incomplete"


def test_absence_audit_reaches_the_public_distribution_summary(setup, tmp_path, capsys):
    from tools.policy_kit_distribution import cli

    _value, _row, registry, release = absence_fixture(setup, tmp_path)
    observed = fleet.audit(registry, release)
    cli._summary(observed, command="audit")
    summary = json.loads(capsys.readouterr().out)
    assert summary["statuses"] == {"governed_absent": 1}
    assert summary["failures"] == 0
    assert summary["product_execution"] == "not_requested"


def test_reapplication_updates_existing_hook_bindings_and_keeps_owner_hooks(
    setup: tuple[Path, Path, Path],
) -> None:
    from tools.workspace_governance import hook_adapters

    root, registry, release = setup
    apply_fixture(setup)
    settings = root / hook_adapters.CLAUDE_PATH
    data = json.loads(settings.read_text())
    data["hooks"]["PreToolUse"][-1]["hooks"][0].pop("onFailure")  # older binding
    data["hooks"].setdefault("Stop", []).append(
        {"hooks": [{"type": "command", "command": "owner-notify"}]}
    )
    settings.write_text(json.dumps(data))
    plan_path = registry.parent / "plan-2.json"
    plan_path.write_bytes(fleet.canonical(fleet.plan(registry, release)))
    result = fleet.apply(plan_path, registry.parent / "receipt-2.json")
    assert result["checkouts"][0]["status"] == "installed", result
    merged = json.loads(settings.read_text())
    assert merged["hooks"]["Stop"][-1]["hooks"][0]["command"] == "owner-notify"
    # The synthetic release ships no pack payload, so only bindings are checked.
    problems = [p for p in hook_adapters.check(root) if hook_adapters.GUARD not in p]
    assert problems == []
    again = registry.parent / "plan-3.json"
    again.write_bytes(fleet.canonical(fleet.plan(registry, release)))
    assert json.loads(again.read_text())["checkouts"][0]["status"] == "unchanged"
