"""Portable manifest semantics and ownership contracts."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from support import (
    PACK,
    PROJECT_REL,
    read_project,
    scrubbed_environment,
    write,
    write_project,
)
from tools.workspace_governance.cli.gov import _sync
from tools.workspace_governance.resolver.core import GovernanceError, GovernanceResolver
from tools.workspace_governance.resolver.roles import (
    RoleDeclarationError,
    compatibility,
)
from tools.workspace_governance.validators import core


def test_synthetic_manifests_and_uniform_provenance(checkout: Path) -> None:
    resolver = GovernanceResolver.for_root(checkout)
    assert resolver.check() == []
    for mode in ("LOCAL_FIX", "FEATURE", "STRUCTURAL_CHANGE"):
        entries = resolver.resolve(mode=mode)["entries"]
        assert all(
            set(item) == {"value", "owner", "source"} for item in entries.values()
        )
        assert entries["execution.mode"]["value"] == mode
        assert entries["verification.full"] == {
            "value": "python scripts/check.py --full",
            "owner": "compatibility",
            "source": ".governance/legacy.json#/verification/full",
        }


@pytest.mark.parametrize(
    ("path", "role", "code"),
    [
        ("../outside.md", "planning_document", "CONFLICTING_VALUE"),
        ("docs/**/*.md", "planning_document", "CONFLICTING_VALUE"),
        ("/docs/plan.md", "bookkeeping", "CONFLICTING_VALUE"),
        ("missing.md", "planning_document", "BROKEN_REFERENCE"),
        (
            "tools/workspace_governance/policies/ownership.md",
            "executable_machinery",
            "CONFLICTING_VALUE",
        ),
        (PROJECT_REL, "executable_machinery", "CONFLICTING_VALUE"),
        (".governance/lock.json", "bookkeeping", "CONFLICTING_VALUE"),
        ("AGENTS.md", "bookkeeping", "CONFLICTING_VALUE"),
        ("AGENTS.md", "governance_treatment", None),
    ],
)
def test_project_cannot_escape_or_relabel_fixed_roles(checkout, path, role, code):
    project = read_project(checkout)
    project["artifact_roles"] = {path: role}
    write_project(checkout, project)
    issues = [
        i
        for i in GovernanceResolver.for_root(checkout).check()
        if i.fact == "artifact_roles." + path
    ]
    assert [i.code for i in issues] == ([] if code is None else [code])


@pytest.mark.parametrize("path", ["AGENTS.md", PROJECT_REL, "missing.md"])
def test_bookkeeping_cannot_capture_instruction_or_manifest_roots(checkout, path):
    project = read_project(checkout)
    project["role_bootstrap"]["bookkeeping"].append(path)
    write_project(checkout, project)
    assert GovernanceResolver.for_root(checkout).check()


def test_role_compatibility_retains_conditions_and_unclassified():
    assert compatibility("Planning", "governance_treatment") == ("contradiction", None)
    assert compatibility("Coding", "generated") == (
        "conditional",
        "allowed_through_owning_generator",
    )
    assert compatibility("Manifest", "product_authority") == (
        "conditional",
        "contradiction_unless_justified",
    )
    assert compatibility("Coding", "unclassified") == ("unclassified", None)
    with pytest.raises(RoleDeclarationError):
        compatibility("Verification", "generated")


@pytest.mark.parametrize(
    "mutation",
    [
        "scope",
        "exclusive_source",
        "unreachable_authority",
        "missing_route",
        "missing_tool",
        "missing_dispatch",
        "unreachable_route",
        "reserved_local",
        "reserved_workspace",
    ],
)
def test_authority_routes_membership_and_reserved_contracts(checkout, mutation):
    resolver = GovernanceResolver.for_root(checkout)
    workspace, project, _ = resolver.load()
    if mutation == "scope":
        project["authorities"][1]["scope"] = project["authorities"][0]["scope"]
    elif mutation == "exclusive_source":
        project["authorities"][1]["source"] = project["authorities"][0]["source"]
    elif mutation == "unreachable_authority":
        authority = copy.deepcopy(project["authorities"][1])
        authority.update(
            authority_id="unreachable", exclusive=False, scope="unreachable"
        )
        project["authorities"].append(authority)
    elif mutation == "missing_route":
        project["execution"]["profiles"]["FEATURE"]["routes"].append("missing")
    elif mutation == "missing_tool":
        project["routes"]["development"]["environment_tools"].append("missing")
    elif mutation == "missing_dispatch":
        project["routes"]["development"]["dispatch_paths"].append("missing/AGENTS.md")
    elif mutation == "unreachable_route":
        project["routes"]["unreachable"] = copy.deepcopy(
            project["routes"]["development"]
        )
    elif mutation == "reserved_local":
        write(
            checkout / ".governance/workspace.local.json",
            {"schema_version": 1, "checkout_locations": {"other": "."}},
        )
    else:
        workspace["relationships"] = [{"kind": "not activated"}]
    write(resolver.workspace_path, workspace)
    write_project(checkout, project)
    issues = GovernanceResolver.for_root(checkout).check()
    assert issues and {i.code for i in issues} <= core.DIAGNOSTIC_CLASSES


def test_overlapping_scenarios_resolve_by_priority_and_ties_reject(checkout):
    project = read_project(checkout)
    project["execution"]["representative_scenarios"][1]["match_all"] = ["governance"]
    write_project(checkout, project)
    resolver = GovernanceResolver.for_root(checkout)
    first = resolver.resolve(task="governance change")["entries"]
    assert first["matched_scenario"]["value"] == "governance-change"
    assert set(first["routes"]["value"]) == {"governance"}
    project["execution"]["representative_scenarios"].reverse()
    write_project(checkout, project)
    assert resolver.resolve(task="governance change") == {
        "schema_version": 1,
        "entries": first,
    }
    project["execution"]["representative_scenarios"][0]["priority"] = 20
    write_project(checkout, project)
    with pytest.raises(GovernanceError) as error:
        resolver.resolve(task="governance change")
    assert [i.code for i in error.value.diagnostics] == ["AMBIGUOUS_AUTHORITY"]


def test_dependency_authority_and_structured_verification_have_positive_controls(
    checkout,
):
    project = read_project(checkout)
    assert core._python_specifier(checkout, project) == (">=3.11", "pyproject.toml")
    path = checkout / ".governance/legacy.json"
    legacy = json.loads(path.read_text())
    legacy["verification"]["full"] = "python scripts/check.py --changed"
    write(path, legacy)
    assert (
        GovernanceResolver.for_root(checkout).resolve()["entries"]["verification.full"][
            "value"
        ]
        == "python scripts/check.py --changed"
    )
    project["verification"]["full"]["json_pointer"] = "/missing"
    write_project(checkout, project)
    with pytest.raises(GovernanceError) as error:
        GovernanceResolver.for_root(checkout).resolve()
    assert [i.code for i in error.value.diagnostics] == ["BROKEN_REFERENCE"]


def test_pack_metadata_has_directional_and_complete_capabilities():
    manifest = json.loads((PACK / "MANIFEST.json").read_text())
    _, files = core.pack_hash(PACK)
    assert core._pack_metadata_issues(manifest, files) == []
    for key in (
        "obligation_comparators",
        "artifact_roles",
        "write_compatibility",
        "bootstrap_roots",
    ):
        changed = copy.deepcopy(manifest)
        if key == "obligation_comparators":
            changed[key]["required_all_of"] = "set_subset"
        elif key == "artifact_roles":
            changed[key].pop()
        elif key == "write_compatibility":
            changed[key]["Coding"]["governance_treatment"] = "allowed"
        else:
            changed[key].remove("validators/core.py")
        assert any(
            i.fact == "MANIFEST." + key
            for i in core._pack_metadata_issues(changed, files)
        )


def test_consumed_optional_field_needs_a_behavioral_fixture():
    from test_consumed_fields import _leaves

    schema = {
        "properties": {
            "new_optional": {"type": "string", "x-governance-use": "consumed"}
        }
    }
    with pytest.raises(AssertionError, match="no behavioral fixture"):
        list(_leaves(schema, [({}, ())], "synthetic"))


def test_unexpected_exception_is_never_consumption_evidence(checkout, monkeypatch):
    from test_consumed_fields import _observe

    def crash(*_args, **_kwargs):
        raise KeyError("synthetic implementation crash")

    monkeypatch.setattr(GovernanceResolver, "resolve", crash)
    with pytest.raises(KeyError, match="implementation crash"):
        _observe(checkout, [], "project.project.name")


def test_work_type_and_artifact_role_vocabulary_is_explicit(checkout):
    project = read_project(checkout)
    project["work_types"] = ["Planning", "Coding", "Manifest"]
    write_project(checkout, project)
    assert GovernanceResolver.for_root(checkout).check() == []
    project["work_types"][2] = "Verification"
    write_project(checkout, project)
    assert any(
        issue.code == "CONFLICTING_VALUE" and issue.fact == "work_types[2]"
        for issue in GovernanceResolver.for_root(checkout).check()
    )
    project["work_types"][2] = "Manifest"
    project["artifact_roles"]["CURRENT_STATE.md"] = "route_membership"
    write_project(checkout, project)
    assert any(
        issue.code == "CONFLICTING_VALUE"
        and issue.fact == "artifact_roles.CURRENT_STATE.md"
        for issue in GovernanceResolver.for_root(checkout).check()
    )


@pytest.mark.parametrize("priority", [-1, True, "high", None])
def test_scenario_priority_schema_rejects_invalid_values(checkout, priority):
    project = read_project(checkout)
    project["execution"]["representative_scenarios"][0]["priority"] = priority
    write_project(checkout, project)
    assert any(
        issue.code == "CONFLICTING_VALUE"
        and issue.fact.endswith("representative_scenarios[0].priority")
        for issue in GovernanceResolver.for_root(checkout).check()
    )


def test_pack_lock_identity_is_manifest_owned(checkout):
    resolver = GovernanceResolver.for_root(checkout)
    workspace, _, _ = resolver.load()
    pack = checkout / "tools/workspace_governance"
    manifest = json.loads((pack / "MANIFEST.json").read_text())
    manifest["version"] = "98.7.6"
    write(pack / "MANIFEST.json", manifest)
    (pack / "VERSION").write_text("98.7.6\n")
    _sync(checkout, resolver)
    lock_path = checkout / workspace["governance_pack"]["lock"]
    lock = json.loads(lock_path.read_text())
    assert lock["version"] == "98.7.6"
    assert core.validate_pack(checkout, workspace) == []
    lock["version"] = "0.0.0"
    write(lock_path, lock)
    assert any(
        issue.code == "PACK_DRIFT" and issue.fact == "governance_pack.version"
        for issue in core.validate_pack(checkout, workspace)
    )


def test_sync_refuses_to_invent_a_missing_pack_path(tmp_path):
    write(tmp_path / "lock.json", {"pack_path": "missing"})
    resolver = SimpleNamespace(
        load=lambda: ({"governance_pack": {"lock": "lock.json"}}, {}, None)
    )
    with pytest.raises(ValueError, match="locked pack path does not exist"):
        _sync(tmp_path, resolver)


def test_cli_json_positions_and_missing_explain_usage(checkout):
    environment = scrubbed_environment(GOVERNANCE_PYTHON=sys.executable)
    for arguments in (["--json", "resolve"], ["resolve", "--json"]):
        result = subprocess.run(
            [str(checkout / "gov"), *arguments],
            cwd=checkout,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["schema_version"] == 1
    missing = subprocess.run(
        [str(checkout / "gov"), "explain"],
        cwd=checkout,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert missing.returncode == 2
    assert missing.stderr.startswith("usage:")
    assert "BROKEN_REFERENCE" not in missing.stdout + missing.stderr


def test_interpreter_override_precedes_overlay_then_repository(checkout):
    project = read_project(checkout)
    project["environment"]["preferred"] = ".venv/bin/python"
    preferred = checkout / ".venv/bin/python"
    preferred.parent.mkdir(parents=True)
    preferred.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    preferred.chmod(0o755)
    selected, reason, issues = core.resolve_interpreter(
        checkout, project, None, {"PYTHON_BIN": "/invalid/legacy"}
    )
    assert (selected, reason, issues) == (
        preferred.absolute(),
        "repository-local environment",
        [],
    )
    selected, reason, issues = core.resolve_interpreter(
        checkout, project, {"interpreter": sys.executable}, {}
    )
    assert (selected, reason, issues) == (
        Path(sys.executable).absolute(),
        "approved local overlay",
        [],
    )
    selected, _, issues = core.resolve_interpreter(
        checkout,
        project,
        {"interpreter": sys.executable},
        {"FIXTURE_PYTHON": "/invalid/explicit"},
    )
    assert selected is None
    assert [issue.code for issue in issues] == ["ENVIRONMENT_MISMATCH"]
