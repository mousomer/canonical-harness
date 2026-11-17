"""Reject consumed-field declarations with no observable implementation effect."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from support import PACK, PROJECT_REL, build_checkout, write
from tools.workspace_governance.cli.gov import _sync
from tools.workspace_governance.resolver.core import GovernanceResolver
from tools.workspace_governance.validators import core

LOCAL = {
    "schema_version": 1,
    "interpreter": sys.executable,
    # A declared overlay key needs a representative value here, or mutating it
    # has nothing to observe.
    "execution_mode": "source",
    "tool_paths": {"fixture-tool": sys.executable},
    "telemetry": {"enabled": True},
}

# Optional consumed fields the repository manifests do not declare yet. Each is
# seeded into the checkout so that mutating it has an effect to observe;
# declaring it in the real manifest remains a separate governance decision.
OPTIONAL_FIXTURES = {
    "project": {"artifact_roles": {"docs/plan.md": "planning_document"}}
}


def _with_optional_fixtures(layer: str, data: dict) -> dict:
    seeded = copy.deepcopy({**OPTIONAL_FIXTURES.get(layer, {}), **data})
    if layer == "project":
        seeded["environment"].setdefault("profile", "python")
    return seeded


def _variants(layer: str, data: dict) -> dict[str, dict]:
    baseline = _with_optional_fixtures(layer, data)
    variants = {"baseline": baseline}
    if layer == "project":
        direct = copy.deepcopy(baseline)
        authorities = {item["authority_id"]: item for item in direct["authorities"]}

        def referenced(spec):
            assert (
                authorities[spec["authority_ref"]]["source"]
                == ".governance/legacy.json"
            )
            return core._json_pointer(LEGACY, spec["json_pointer"])

        for fact in ("canonical", "full"):
            direct["verification"][fact] = {
                "command": referenced(baseline["verification"][fact])
            }
        direct["canonical_owner_set"] = {
            "owners": referenced(baseline["canonical_owner_set"])
        }
        variants["direct"] = direct
    return variants


def _leaves(node: dict, candidates: list, label: str):
    if "properties" in node:
        for name, child in node["properties"].items():
            yield from _leaves(
                child,
                [(v[name], (*p, name)) for v, p in candidates if name in v],
                label + "." + name,
            )
    elif "properties" in node.get("items", {}):
        yield from _leaves(
            node["items"],
            [
                (item, (*p, i))
                for values, p in candidates
                for i, item in enumerate(values)
            ],
            label + "[]",
        )
    elif (
        isinstance(node.get("additionalProperties"), dict)
        and "properties" in node["additionalProperties"]
    ):
        yield from _leaves(
            node["additionalProperties"],
            [(item, (*p, k)) for values, p in candidates for k, item in values.items()],
            label + ".*",
        )
    elif node.get("x-governance-use") == "consumed":
        # New optional fields need an actual representative value, not only a tag.
        assert candidates, f"Consumed field has no behavioral fixture: {label}"
        yield label, candidates[0][1]


def _fixture_documents():
    # Collection builds only synthetic bytes and never opens a host overlay.
    with tempfile.TemporaryDirectory(prefix="governance-contract-collection-") as tmp:
        previous = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(Path(tmp) / "configuration")
        try:
            root = build_checkout(Path(tmp) / "consumer")
            return (
                json.loads((root / ".governance/workspace.json").read_text()),
                json.loads((root / PROJECT_REL).read_text()),
                json.loads((root / ".governance/legacy.json").read_text()),
            )
        finally:
            if previous is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = previous


WORKSPACE, PROJECT, LEGACY = _fixture_documents()


def _cases():
    for layer, rel, data in (
        ("workspace", "workspace", WORKSPACE),
        ("project", "project", PROJECT),
        ("local", "workspace-local", LOCAL),
    ):
        schema = json.loads((PACK / "schemas" / (rel + ".schema.json")).read_text())
        variants = _variants(layer, data)
        candidates = [(value, (name,)) for name, value in variants.items()]
        for label, path in _leaves(schema, candidates, layer):
            yield label, layer, path[0], path[1:]


def _mutate(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return value + "__changed"
    if isinstance(value, list):
        return [_mutate(value[0]), *value[1:]] if value else ["changed"]
    if isinstance(value, dict):
        key = next(iter(value))
        return {**value, key: _mutate(value[key])}
    raise AssertionError(f"Add a meaningful mutation for {value!r}")


def _observe(root: Path, calls: list, label: str) -> str:  # noqa: C901 - explicit semantic family observers
    """Observe semantic outcomes, never arbitrary exception text or raw manifests."""
    calls.clear()
    resolver = GovernanceResolver.for_root(root)
    # Missing transformed selector must be diagnosed by the semantic surface
    # contract, rather than counted through an enum/schema rejection.
    if label.endswith(".transform"):
        project = json.loads((root / PROJECT_REL).read_text())
        defects = core._surface_contract_defects(project)
        if defects:
            return json.dumps({"surface_contract_defects": defects}, sort_keys=True)
    issues = resolver.check()
    if issues:
        # All mutations preserve schema types/enums, except the transform
        # removal above whose semantic contract was explicitly observed.
        assert all(i.fact != "schema" for i in issues), (
            "Schema-only rejection is not consumption proof"
        )
        return json.dumps(
            {"diagnostics": [i.to_dict() for i in issues]}, sort_keys=True
        )
    _workspace, project, local = resolver.load()
    schema_name = (
        "workspace"
        if label.startswith("workspace.")
        else "workspace-local"
        if label.startswith("local.")
        else "project"
    )
    family = next(
        node["x-governance-behavior"]
        for name, node in _schema_leaves(
            json.loads((PACK / "schemas" / (schema_name + ".schema.json")).read_text()),
            "local" if schema_name == "workspace-local" else schema_name,
        )
        if name == label
    )
    observed = {"family": family}
    if family in {
        "identity",
        "membership",
        "pack",
        "authority",
        "routes",
        "verification",
        "execution",
    }:
        resolved = [
            resolver.resolve(mode=mode, task=task)["entries"]
            for mode in (None, "LOCAL_FIX", "FEATURE", "STRUCTURAL_CHANGE")
            for task in [None, *[" ".join(s["match_all"]) for s in BASE_SCENARIOS]]
        ]
        keys = {
            "identity": {"workspace.id", "project.id", "project.name"},
            "membership": {"project.id"},
            "pack": {"workspace.id"},
            "authority": {"authorities"},
            "routes": {"routes"},
            "verification": {
                "verification.canonical",
                "verification.full",
                "verification.targeted",
                "verification.rule",
            },
            "execution": {
                "execution.mode",
                "execution.exploration",
                "execution.final_evidence_rule",
                "routes",
                "matched_scenario",
                "matched_scenario_priority",
            },
        }[family]
        observed["resolution"] = [{key: row[key] for key in keys} for row in resolved]
        if family == "membership":
            observed["project_manifest"] = resolver.project_source()
        if family == "authority":
            observed["authority_explanations"] = [
                resolver.explain("authority:" + a["authority_id"])
                for a in project["authorities"]
            ]
    elif family in {"owner_set", "facade"}:
        if family == "owner_set":
            observed["diagnostics"] = [
                i.to_dict() for i in core._authority_graph_issues(root, project)
            ]
        else:
            original = (root / ".governance/legacy.json").read_text()
            try:
                payload = json.loads(original)
                payload["codex_routing"]["routes"] = {}
                write(root / ".governance/legacy.json", payload)
                observed["diagnostics"] = [
                    i.to_dict()
                    for i in core._generated_surface_issues(
                        root, project, generated=True
                    )
                ]
            finally:
                (root / ".governance/legacy.json").write_text(original)
    elif family == "roles":
        observed["roles"] = resolver.role_index().to_dict()
    elif family == "telemetry":
        observed["enabled"] = resolver.telemetry_settings().enabled
    elif family in {"environment", "local"}:
        for overlay in (None, local):
            for env in (
                {},
                {
                    "FIXTURE_PYTHON": sys.executable,
                    "FIXTURE_TOOL": sys.executable,
                    "FIXTURE_EXECUTION_MODE": "source",
                },
            ):
                data, diagnostics = core.doctor(
                    root, project, overlay, env, route="development"
                )
                observed.setdefault("doctor", []).append(
                    [data, [i.to_dict() for i in diagnostics]]
                )
    else:
        raise AssertionError("Add field-specific semantic observer for " + family)
    observed["execution_calls"] = calls
    return json.dumps(observed, sort_keys=True)


def _schema_leaves(node: dict, label: str):
    if "properties" in node:
        for name, child in node["properties"].items():
            yield from _schema_leaves(child, label + "." + name)
    elif "properties" in node.get("items", {}):
        yield from _schema_leaves(node["items"], label + "[]")
    elif (
        isinstance(node.get("additionalProperties"), dict)
        and "properties" in node["additionalProperties"]
    ):
        yield from _schema_leaves(node["additionalProperties"], label + ".*")
    elif node.get("x-governance-use") == "consumed":
        yield label, node


BASE_SCENARIOS = PROJECT["execution"]["representative_scenarios"]


@pytest.mark.parametrize(
    "label,layer,variant,path", list(_cases()), ids=lambda item: str(item)
)
def test_every_consumed_leaf_changes_execution(  # noqa: C901 - field-specific mutation dispatch
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    layer: str,
    variant: str,
    path: tuple,
) -> None:
    calls = []

    def probe(command, **kwargs):
        calls.append({"command": command, "timeout": kwargs.get("timeout")})
        if command[0] == "git":
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if len(command) > 1 and command[1].endswith("scripts/check.py"):
            # Surface identities are consumed on rejection. Exercise that
            # path alongside the successful interpreter/tool probes below.
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"generated drift")
        if "from packaging.specifiers" in command[-1]:
            stdout = "3.14.0"
        elif "critical_packages" in command[-1]:
            # Discriminate on a fact the probe reports, not on its exact opening
            # imports: matching the prelude made this fixture silently answer the
            # wrong branch when the probe gained a field.
            stdout = json.dumps(
                {
                    "version": "3.14.0",
                    "package": str(
                        (checkout / "src/fixture_runtime/__init__.py").resolve()
                    ),
                    "critical_packages": [],
                    "distribution": False,
                }
            )
        else:
            stdout = "tool-version"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    # Type/enum rejection is deliberately disabled for consumption proof.
    # Real semantic validators and controlled diagnostics remain enabled.
    monkeypatch.setattr(core, "_schema_issues", lambda *args, **kwargs: [])
    monkeypatch.setattr(core.subprocess, "run", probe)
    monkeypatch.setattr(
        core.shutil, "which", lambda value, **kwargs: "/resolved/" + value
    )

    preferred = checkout / ".venv/bin/python"
    preferred.parent.mkdir(parents=True)
    preferred.write_text("#!/bin/sh\nexit 0\n")
    preferred.chmod(0o755)
    local_path = checkout / ".governance/workspace.local.json"
    write(local_path, LOCAL)
    paths = {
        "workspace": checkout / ".governance/workspace.json",
        "project": checkout / PROJECT_REL,
        "local": local_path,
    }
    for fixture_layer in OPTIONAL_FIXTURES:
        seeded = json.loads(paths[fixture_layer].read_text())
        chosen = variant if fixture_layer == layer else "baseline"
        write(paths[fixture_layer], _variants(fixture_layer, seeded)[chosen])
    # A stale development lock would mask required_schema behind an earlier
    # integrity failure; establish a coherent baseline before mutation.
    _sync(checkout, GovernanceResolver.for_root(checkout))
    before = _observe(checkout, calls, label)
    payload = json.loads(paths[layer].read_text())
    parent = payload
    for token in path[:-1]:
        parent = parent[token]
    if label.endswith(".transform"):
        parent.pop(path[-1])
    elif label in {
        "project.verification.canonical.json_pointer",
        "project.verification.full.json_pointer",
    }:
        parent[path[-1]] = "/verification/alternate"
    elif label == "project.execution.representative_scenarios[].mode":
        parent[path[-1]] = "FEATURE"
    elif label.endswith(".default_mode"):
        parent[path[-1]] = "LOCAL_FIX"
    elif label.endswith(".authority_type"):
        parent[path[-1]] = "alias"
    elif label.endswith(".source_type"):
        parent[path[-1]] = "directory"
    elif label.endswith(".profile"):
        parent[path[-1]] = "tools"
    elif label == "project.artifact_roles":
        parent[path[-1]] = {"docs/plan.md": "bookkeeping"}
    else:
        parent[path[-1]] = _mutate(copy.deepcopy(parent[path[-1]]))
    write(paths[layer], payload)
    assert _observe(checkout, calls, label) != before, (
        f"Consumed declaration has no observed effect: {label}"
    )
