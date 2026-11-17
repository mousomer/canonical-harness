"""Public binding portability with the complete execution policy kept private."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest
from tools.workspace_governance import fleet, fleet_support


@pytest.fixture
def consumer(tmp_path: Path):
    root = tmp_path / "synthetic consumer"
    root.mkdir()
    (root / "scripts").mkdir()
    (root / "AGENTS.md").write_text(
        "# Existing owner doctrine\nKeep required verification.\n"
    )
    script = root / "scripts/verify.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    row = {
        "id": "synthetic-consumer",
        "path": str(root),
        "repository_id": "synthetic-repository",
        "integration_mode": "direct",
        "profile": "tools",
        "authorities": ["AGENTS.md", "AGENTS.md"],
        "verification": {
            "argv": ["./scripts/verify.sh"],
            "runner": {"kind": "shell", "path": "scripts/verify.sh"},
            "source": "synthetic owner authority",
            "timeout_seconds": 10,
        },
    }
    release = {
        "schema_version": 1,
        "name": "synthetic-pack",
        "version": "synthetic",
        "revision": "synthetic",
        "files": [],
    }
    return root, row, release


def rendered_metadata(consumer):
    root, row, release = consumer
    generated = fleet_support.rendered(row, root, release, "a" * 64)
    return generated, json.loads(generated[".governance/fleet.json"][0])


def private_policy():
    return {
        "version": fleet_support.EXECUTION_POLICY,
        "inherit": ["HOME", "SYNTHETIC_PRIVATE_SELECTOR"],
        "path": [
            "/Users/synthetic-private-machine/certified/runtime/bin",
            "/usr/bin",
            "/bin",
        ],
        "approved_tools": {"bash": "/Users/synthetic-private-machine/certified/bash"},
    }


def test_private_policy_paths_and_names_are_not_serialized_publicly(consumer):
    _root, row, _release = consumer
    row["verification"]["execution_environment"] = private_policy()
    original = copy.deepcopy(row)
    generated, metadata = rendered_metadata(consumer)
    assert metadata["verification"] == {
        key: value
        for key, value in row["verification"].items()
        if key != "execution_environment"
    }
    assert metadata["execution_environment_binding"] == {
        "version": fleet_support.EXECUTION_POLICY,
        "source": "private-registry",
        "sha256": fleet_support.digest(fleet_support.canonical(private_policy())),
    }
    public_bytes = b"\n".join(data for data, _mode in generated.values())
    assert b"/Users/synthetic-private-machine/" not in public_bytes
    assert b"SYNTHETIC_PRIVATE_SELECTOR" not in public_bytes
    assert row == original


@pytest.mark.parametrize("field", ["path", "inherit", "approved_tools"])
def test_every_private_policy_field_is_bound_and_drift_changes_the_public_fingerprint(
    consumer, field
):
    _root, row, _release = consumer
    row["verification"]["execution_environment"] = private_policy()
    _generated, before = rendered_metadata(consumer)
    if field == "path":
        row["verification"]["execution_environment"][field][0] = (
            "/Users/second-private-machine/certified/runtime/bin"
        )
    elif field == "inherit":
        row["verification"]["execution_environment"][field].append(
            "SYNTHETIC_SECOND_SELECTOR"
        )
    else:
        row["verification"]["execution_environment"][field]["bash"] = (
            "/Users/second-private-machine/certified/bash"
        )
    _generated, after = rendered_metadata(consumer)
    assert before["verification"] == after["verification"]
    assert (
        before["execution_environment_binding"]["sha256"]
        != after["execution_environment_binding"]["sha256"]
    )
    assert after["execution_environment_binding"]["sha256"] == fleet_support.digest(
        fleet_support.canonical(row["verification"]["execution_environment"])
    )
    assert b"/Users/second-private-machine/" not in fleet_support.canonical(after)


def test_rendering_preserves_actual_declared_tool_path_environment_and_execution(
    consumer, monkeypatch
):
    root, row, _release = consumer
    tools = root.parent / "private certified tools"
    tools.mkdir()
    tool = tools / "synthetic-certified-tool"
    tool.write_text("#!/bin/sh\nprintf 'certified-policy-tool\\n'\n")
    tool.chmod(0o755)
    (root / "scripts/verify.sh").write_text(
        '#!/bin/sh\n[ "$SYNTHETIC_PRIVATE_SELECTOR" = enabled ] || exit 41\nsynthetic-certified-tool\n'
    )
    monkeypatch.setenv("SYNTHETIC_PRIVATE_SELECTOR", "enabled")
    monkeypatch.setenv("PATH", "/synthetic/ambient-path-is-insufficient")
    row["verification"]["execution_environment"] = {
        "version": fleet_support.EXECUTION_POLICY,
        "inherit": ["HOME", "SYNTHETIC_PRIVATE_SELECTOR"],
        "path": [str(tools), "/usr/bin", "/bin"],
    }
    original = copy.deepcopy(row["verification"])
    before = fleet.execution_contract(
        row["verification"], root, row["verification"]["argv"]
    )
    result_before = fleet.run_digest(before[0], root, before[1], 10)
    _generated, metadata = rendered_metadata(consumer)
    after = fleet.execution_contract(
        row["verification"], root, row["verification"]["argv"]
    )
    result_after = fleet.run_digest(after[0], root, after[1], 10)
    assert row["verification"] == original
    assert before == after
    assert after[1]["PATH"] == ":".join(original["execution_environment"]["path"])
    assert after[1]["SYNTHETIC_PRIVATE_SELECTOR"] == "enabled"
    assert after[2]["inherited_names"] == ["HOME", "SYNTHETIC_PRIVATE_SELECTOR"]
    assert result_before["exit_code"] == result_after["exit_code"] == 0
    assert result_before["timed_out"] is result_after["timed_out"] is False
    assert (
        result_before["stdout_sha256"]
        == result_after["stdout_sha256"]
        == fleet_support.digest(b"certified-policy-tool\n")
    )
    assert "execution_environment" not in metadata["verification"]
    assert str(tools) not in json.dumps(metadata)


def test_no_policy_metadata_remains_byte_compatible(consumer):
    _root, row, release = consumer
    generated, metadata = rendered_metadata(consumer)
    expected = {
        "schema_version": fleet_support.SCHEMA,
        "release": {key: release[key] for key in ("name", "version", "revision")},
        "release_manifest_sha256": "a" * 64,
        "profile": "tools",
        "integration_mode": "direct",
        "verification": row["verification"],
        "authorities": ["AGENTS.md"],
        "instruction_aliases": {},
        "skill_adapters": [],
    }
    assert "execution_environment_binding" not in metadata
    assert generated[".governance/fleet.json"] == (
        fleet_support.canonical(expected),
        0o644,
    )


def test_explicit_empty_policy_is_bound_without_changing_default_execution(consumer):
    root, row, _release = consumer
    before = fleet.execution_contract(
        row["verification"], root, row["verification"]["argv"]
    )
    row["verification"]["execution_environment"] = {}
    _generated, metadata = rendered_metadata(consumer)
    after = fleet.execution_contract(
        row["verification"], root, row["verification"]["argv"]
    )
    assert before == after
    assert metadata["execution_environment_binding"]["sha256"] == fleet_support.digest(
        fleet_support.canonical({})
    )


def test_changed_private_policy_invalidates_an_installed_binding(consumer, monkeypatch):
    root, row, release = consumer
    monkeypatch.setenv("HOME", str(root.parent))
    row["verification"]["execution_environment"] = private_policy()
    gov = root / "gov"
    gov.write_bytes(b"#!/bin/sh\nexit 0\n")
    gov.chmod(0o755)
    payload = {"gov": gov.read_bytes()}
    release["files"] = [{"path": "gov", "mode": 0o755}]
    generated, _metadata = rendered_metadata(consumer)
    for relative, (data, mode) in generated.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        destination.chmod(mode)
    initialized = subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(root),
            "init",
            "--initial-branch=main",
        ],
        capture_output=True,
        check=False,
    )
    assert initialized.returncode == 0
    _git, binding = fleet.installed_metadata(row, root, release, "a" * 64, payload)
    assert binding == fleet_support.digest(
        (root / ".governance/fleet.json").read_bytes()
    )
    row["verification"]["execution_environment"]["path"][0] = (
        "/Users/changed-private-machine/certified/runtime/bin"
    )
    with pytest.raises(
        fleet_support.FleetError,
        match=r"Managed binding drifted: \.governance/fleet\.json",
    ):
        fleet.installed_metadata(row, root, release, "a" * 64, payload)
