from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from support import PACK, build_checkout, scrubbed_environment, write
from tools.workspace_governance.cli.gov import main
from tools.workspace_governance.resolver.core import GovernanceResolver
from tools.workspace_governance.validators import core

CATALOG = json.loads((PACK / "synthetic-fixtures/contradictions.json").read_text())
USAGE = json.loads((PACK / "field-usage.json").read_text())


def environment(**updates: str) -> dict[str, str]:
    return scrubbed_environment(**updates)


def run(
    root: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=root,
        env=environment() if env is None else env,
        text=True,
        capture_output=True,
        check=False,
    )


def wrapper(root: Path, *, no_site: bool = False) -> Path:
    path = root / "selected-python"
    path.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" ' + ("-S " if no_site else "") + '"$@"\n'
    )
    path.chmod(0o755)
    return path


def test_application_floor_is_certified_separately_by_doctor(checkout: Path) -> None:
    (checkout / "pyproject.toml").write_text('[project]\nrequires-python = ">=99.0"\n')
    result = run(
        checkout,
        "./gov",
        "doctor",
        "--json",
        env=environment(
            GOVERNANCE_PYTHON=sys.executable, FIXTURE_PYTHON=sys.executable
        ),
    )
    assert result.returncode == 1
    assert "python.version" in result.stdout and ">=99.0" in result.stdout
    assert "bootstrap.interpreter" not in result.stdout + result.stderr


def test_application_bootstrap_retains_compound_specifier_lower_bound(
    checkout: Path,
) -> None:
    (checkout / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.11,<4.0"\n'
    )
    result = run(
        checkout,
        sys.executable,
        "-c",
        "from tools.workspace_governance.cli.bootstrap import main; raise SystemExit(main(check_only=True))",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "error",
    [
        ModuleNotFoundError("packaging"),
        ImportError("dependency"),
        KeyError("requires-python"),
        StopIteration(),
    ],
)
def test_metadata_failure_classes_are_controlled(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    def fail(*args: object) -> None:
        raise error

    monkeypatch.setattr(core, "_python_specifier", fail)
    _, project, _ = GovernanceResolver.for_root(checkout).load()
    data, issues = core.doctor(
        checkout,
        project,
        None,
        {"FIXTURE_PYTHON": sys.executable, "FIXTURE_EXECUTION_MODE": "source"},
    )
    assert data["status"] == "ENVIRONMENT_INVALID"
    assert {i.code for i in issues} == {"ENVIRONMENT_MISMATCH"}
    assert main(["--root", str(checkout), "doctor", "--json"]) == 1
    assert "Traceback" not in capsys.readouterr().out


@pytest.mark.parametrize("operation", ["unflag", "remove"])
def test_each_canonical_owner_is_protected(checkout: Path, operation: str) -> None:
    resolver = GovernanceResolver.for_root(checkout)
    workspace, project, _ = resolver.load()
    owners = [a for a in project["authorities"] if a["canonical_governance"]]
    assert owners
    for owner in owners:
        changed = copy.deepcopy(project)
        entry = next(
            a
            for a in changed["authorities"]
            if a["authority_id"] == owner["authority_id"]
        )
        if operation == "unflag":
            entry["canonical_governance"] = False
        else:
            changed["authorities"].remove(entry)
        issues = core.validate_manifests(
            checkout,
            workspace,
            changed,
            None,
            pack_root=checkout / "tools/workspace_governance",
        )
        assert any(i.fact.startswith("canonical_owner:") for i in issues), owner


def apply_case(root: Path, mutation: str) -> set[str]:  # noqa: C901 - executable contradiction catalog
    resolver = GovernanceResolver.for_root(root)
    workspace, project, local = resolver.load()
    if mutation == "duplicate-exclusive-authority":
        project["authorities"].append(copy.deepcopy(project["authorities"][0]))
    elif mutation == "cross-layer-semantic-field":
        workspace["verification"] = "not workspace-owned"
    elif mutation == "broken-authority-reference":
        project["routes"]["governance"]["authority_refs"].append("missing")
    elif mutation == "compatibility-facade-drift":
        project["routes"]["governance"]["authority_refs"].append("product-design")
    elif mutation == "modified-pack":
        (root / "tools/workspace_governance/VERSION").write_text("modified\n")
    elif mutation == "tracked-machine-path":
        project["project"]["name"] = str(Path("/") / "Users" / "example" / "private")
    elif mutation == "ambiguous-project-routing":
        workspace["projects"].append(copy.deepcopy(workspace["projects"][0]))
    elif mutation == "relabelled-bootstrap-root":
        project["artifact_roles"] = {"AGENTS.md": "bookkeeping"}
    elif mutation == "missing-role-declaration":
        project["artifact_roles"] = {"docs/missing.md": "planning_document"}
    elif mutation in {
        "missing-interpreter",
        "wrong-python-version",
        "wrong-editable-checkout",
        "valid-local-override",
    }:
        env = {
            "FIXTURE_PYTHON": sys.executable,
            "FIXTURE_EXECUTION_MODE": "source",
        }
        if mutation == "missing-interpreter":
            env["FIXTURE_PYTHON"] = str(root / "missing")
        elif mutation == "wrong-python-version":
            (root / "pyproject.toml").write_text(
                '[project]\nrequires-python = ">=99"\n'
            )
        elif mutation == "wrong-editable-checkout":
            project["environment"]["editable_source"] = "wrong/source"
        else:
            local, env = (
                {"interpreter": sys.executable, "execution_mode": "source"},
                {},
            )
        _, issues = core.doctor(root, project, local, env)
        return {i.code for i in issues}
    elif mutation == "valid-local-fix-resolution":
        assert (
            resolver.resolve(mode="LOCAL_FIX")["entries"]["execution.mode"]["value"]
            == "LOCAL_FIX"
        )
        return set()
    else:
        raise AssertionError(f"Unimplemented fixture: {mutation}")
    write(resolver.workspace_path, workspace)
    write(resolver.project_path, project)
    return {i.code for i in GovernanceResolver.for_root(root).check()}


@pytest.mark.parametrize("case", CATALOG["cases"], ids=lambda c: c["id"])
def test_contradiction_catalog_executes_expected_codes(
    checkout: Path, case: dict
) -> None:
    observed = apply_case(checkout, case["mutation"])
    assert (
        observed == set() if case["expected"] == "ok" else case["expected"] in observed
    )


def test_every_advertised_diagnostic_has_an_executable_emission(tmp_path: Path) -> None:
    # Run the same catalog dispatch, not a second expected-code declaration.
    observed = set()
    for case in CATALOG["cases"]:
        root = tmp_path / case["id"]
        root.mkdir()
        build_checkout(root)
        observed.update(apply_case(root, case["mutation"]))
    manifest = json.loads((PACK / "MANIFEST.json").read_text())
    assert observed == set(manifest["diagnostics"]) == core.DIAGNOSTIC_CLASSES


def test_facade_contract_and_authority_order(checkout: Path) -> None:
    resolver = GovernanceResolver.for_root(checkout)
    _, project, _ = resolver.load()
    for route in project["routes"].values():
        route["authority_refs"].reverse()
    write(resolver.project_path, project)
    assert resolver.check() == []
    project["generated_surfaces"][0]["kind"] = "generated_document"
    write(resolver.project_path, project)
    assert "CONFLICTING_VALUE" in {i.code for i in resolver.check()}


def test_manifest_driven_platform_hash_exclusion(checkout: Path) -> None:
    pack = checkout / "tools/workspace_governance"
    before = core.pack_hash(pack)
    for rel in (".DS_Store", "schemas/.DS_Store"):
        (pack / rel).write_bytes(b"platform metadata")
    assert core.pack_hash(pack) == before
    manifest = json.loads((pack / "MANIFEST.json").read_text())
    manifest["pack_hash_excludes"].remove("**/.DS_Store")
    write(pack / "MANIFEST.json", manifest)
    assert "schemas/.DS_Store" in core.pack_hash(pack)[1]
    assert ".DS_Store" in core.pack_hash(pack)[1]


def test_source_types_and_aliases_are_behavioral(checkout: Path) -> None:
    resolver = GovernanceResolver.for_root(checkout)
    _, project, _ = resolver.load()
    alias = next(a for a in project["authorities"] if a["source_type"] == "alias")
    alias["exclusive"] = True
    assert core._authority_graph_issues(checkout, project)
    alias["exclusive"] = False
    directory = next(
        a for a in project["authorities"] if a["source_type"] == "directory"
    )
    directory["source_type"] = "file"
    assert core._authority_graph_issues(checkout, project)
    directory["source_type"] = "directory"
    item = project["authorities"][0]
    item.update(
        source_type="json_pointer",
        source=".governance/legacy.json#/authority_model",
        canonical_governance=False,
    )
    assert not any(
        i.fact == "authority:" + item["authority_id"]
        for i in core._authority_graph_issues(checkout, project)
    )
    item["source"] += "/missing"
    assert any(
        i.fact == "authority:" + item["authority_id"]
        for i in core._authority_graph_issues(checkout, project)
    )


@pytest.fixture
def behavior_trace(family: str):
    """Require execution of the named callable, not a declaration-to-declaration match."""
    expected = set()
    for name in USAGE[family]["handler"].split(" / "):
        owner = GovernanceResolver if name.startswith("GovernanceResolver.") else core
        function = getattr(owner, name.split(".")[-1])
        assert callable(function), name
        expected.add(function.__code__)
    observed = set()
    previous = sys.getprofile()

    def record(frame, event, arg):
        if event == "call" and frame.f_code in expected:
            observed.add(frame.f_code)
        if previous is not None:
            previous(frame, event, arg)

    sys.setprofile(record)
    try:
        yield
    finally:
        sys.setprofile(previous)
    assert expected <= observed, "registered behavior was not executed"


@pytest.mark.parametrize("family", sorted(USAGE))
def test_registered_behavior_paths_have_mutation_evidence(  # noqa: C901 - explicit registered behavior families
    checkout: Path, family: str, behavior_trace
) -> None:
    """Each registered path has an executed mutation, not a count or AST claim."""
    assert USAGE[family]["test"] == family
    resolver = GovernanceResolver.for_root(checkout)
    workspace, project, _ = resolver.load()
    if family == "schema":
        project["schema_version"] = 99
    elif family == "reserved":
        write(
            checkout / ".governance/workspace.local.json",
            {"schema_version": 1, "cache_locations": {"cache": "."}},
        )
    elif family == "identity":
        before = resolver.resolve()["entries"]["project.name"]
        project["project"]["name"] = "Changed identity"
        write(resolver.project_path, project)
        assert resolver.resolve()["entries"]["project.name"] != before
        return
    elif family == "membership":
        workspace["projects"][0]["manifest"] = "missing-manifest.json"
    elif family == "pack":
        workspace["governance_pack"]["required_schema"] += 1
    elif family == "authority":
        resolver.explain("authority:" + project["authorities"][0]["authority_id"])
        project["authorities"][0]["source"] = "missing-owner.md"
    elif family == "owner_set":
        project["canonical_owner_set"]["json_pointer"] = "/missing"
    elif family == "routes":
        resolver.resolve()
        project["routes"]["governance"]["authority_refs"].append("product-design")
    elif family == "verification":
        before = resolver.resolve()["entries"]["verification.full"]
        project["verification"]["full"]["json_pointer"] = project["verification"][
            "canonical"
        ]["json_pointer"]
        write(resolver.project_path, project)
        assert resolver.resolve()["entries"]["verification.full"] != before
        return
    elif family == "execution":
        before = resolver.resolve(mode="LOCAL_FIX")["entries"]["execution.exploration"]
        project["execution"]["profiles"]["LOCAL_FIX"]["exploration"].append(
            "a changed exploration bound"
        )
        write(resolver.project_path, project)
        assert (
            resolver.resolve(mode="LOCAL_FIX")["entries"]["execution.exploration"]
            != before
        )
        return
    elif family == "facade":
        project["generated_surfaces"][0]["target"] += "/missing"
    elif family == "environment":
        data, issues = core.doctor(
            checkout,
            project,
            None,
            {
                "FIXTURE_PYTHON": sys.executable,
                "FIXTURE_EXECUTION_MODE": "source",
            },
        )
        assert data["status"] == "ok" and not issues
        project["environment"]["critical_packages"].append(
            "missing_governance_test_package"
        )
        data, issues = core.doctor(
            checkout,
            project,
            None,
            {
                "FIXTURE_PYTHON": sys.executable,
                "FIXTURE_EXECUTION_MODE": "source",
            },
        )
        assert data["status"] == "ENVIRONMENT_INVALID" and issues
        return
    elif family == "sanitation":
        project["sanitation"]["secret_scanner"] = "missing-scanner.py"
    elif family == "skill_adapters":
        from test_skill_adapters import declared

        project, _, _ = declared(checkout)
        assert core._skill_adapter_issues(checkout, project) == []
        project["skill_adapters"][0]["sha256"] = "b" * 64
        assert core._skill_adapter_issues(checkout, project)
        return
    elif family == "telemetry":
        overlay = checkout / ".governance/workspace.local.json"
        write(overlay, {"schema_version": 1, "telemetry": {"enabled": True}})
        assert GovernanceResolver.for_root(checkout).telemetry_settings().enabled
        write(overlay, {"schema_version": 1, "telemetry": {"enabled": "yes"}})
        assert not GovernanceResolver.for_root(checkout).telemetry_settings().enabled
    elif family == "roles":
        assert resolver.role_index().resolve("AGENTS.md").role == (
            "governance_treatment"
        )
        project["role_bootstrap"]["instruction_roots"].remove("AGENTS.md")
        write(resolver.project_path, project)
        assert resolver.role_index().resolve("AGENTS.md").role == "unclassified"
        project["artifact_roles"] = {".governance/project.json": "executable_machinery"}
    elif family == "local":
        # The overlay carries the mode as well as the interpreter, so this
        # asserts a healthy environment rather than the project default's.
        assert not core.doctor(
            checkout,
            project,
            {"interpreter": sys.executable, "execution_mode": "source"},
            {},
        )[1]
        a = core.resolve_interpreter(
            checkout, project, {"interpreter": sys.executable}, {}
        )
        b = core.resolve_interpreter(
            checkout, project, {"interpreter": "/missing/local-python"}, {}
        )
        assert not a[2] and b[2] and a[0] != b[0]
        return
    else:
        pytest.fail(f"No executable evidence for registered behavior {family}")
    write(resolver.workspace_path, workspace)
    write(resolver.project_path, project)
    assert GovernanceResolver.for_root(checkout).check()


def test_usage_declarations_cannot_hide_unimplemented_or_unconstrained_fields(
    checkout: Path,
) -> None:
    path = checkout / "tools/workspace_governance/schemas/workspace-local.schema.json"
    schema = json.loads(path.read_text())
    schema["properties"]["checkout_locations"].pop("maxProperties")
    assert core._schema_field_usage_issues(schema, str(path))
    schema = json.loads(path.read_text())
    schema["properties"]["interpreter"]["x-governance-behavior"] = "aspirational"
    assert core._schema_field_usage_issues(schema, str(path))
    schema["properties"]["interpreter"]["x-governance-behavior"] = "schema"
    assert core._schema_field_usage_issues(schema, str(path))


def test_route_tool_fields_and_local_tool_override_are_consumed(checkout: Path) -> None:
    _, project, _ = GovernanceResolver.for_root(checkout).load()
    tool = checkout / "fake-godot"
    tool.write_text('#!/bin/sh\nprintf "test-tool:%s\\n" "$1"\n')
    tool.chmod(0o755)
    env = {
        "FIXTURE_PYTHON": sys.executable,
        "FIXTURE_EXECUTION_MODE": "source",
    }
    local = {"tool_paths": {"fixture-tool": str(tool)}}
    data, issues = core.doctor(checkout, project, local, env, route="development")
    assert not issues and data["route_tools"]["fixture-tool"] == "test-tool:--version"
    project["environment"]["route_tools"]["fixture-tool"]["version_args"] = [
        "--changed"
    ]
    data, issues = core.doctor(checkout, project, local, env, route="development")
    assert not issues and data["route_tools"]["fixture-tool"] == "test-tool:--changed"


@pytest.mark.parametrize(
    "malformed", [[], {"route": []}, {"route": {"authority_keys": [1]}}]
)
def test_malformed_facade_target_is_a_diagnostic(
    checkout: Path, malformed: object
) -> None:
    path = checkout / ".governance/legacy.json"
    pack = json.loads(path.read_text())
    pack["codex_routing"]["routes"] = malformed
    write(path, pack)
    assert "CONFLICTING_VALUE" in {
        i.code for i in GovernanceResolver.for_root(checkout).check()
    }


def test_exclusive_file_identity_normalizes_equivalent_paths(checkout: Path) -> None:
    _, project, _ = GovernanceResolver.for_root(checkout).load()
    duplicate = {
        **project["authorities"][0],
        "authority_id": "second-identity",
        "scope": "another-scope",
        "canonical_governance": False,
    }
    duplicate["source"] = "./" + duplicate["source"]
    project["authorities"].append(duplicate)
    assert any(
        i.code == "AMBIGUOUS_AUTHORITY" and i.fact == "authority.source"
        for i in core._authority_graph_issues(checkout, project)
    )
