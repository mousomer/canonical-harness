"""Synthetic consumers; no application checkout or host overlay is a fixture."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACK = Path(
    os.environ.get("POLICY_KIT_RUNTIME_ROOT", str(ROOT / "tools/workspace_governance"))
).resolve()
PROJECT_REL = ".governance/project.json"
WORKSPACE_ID = "fixture-workspace"
MODES = ("LOCAL_FIX", "FEATURE", "STRUCTURAL_CHANGE")
INTERPRETER_SELECTORS = (
    "FIXTURE_PYTHON",
    "FIXTURE_EXECUTION_MODE",
    "FIXTURE_TOOL",
    "GOVERNANCE_PYTHON",
    "WORKSPACE_VENV",
    "PYTHON_BIN",
    "PYTHONPATH",
    "PYTHONHOME",
    "TET4D_PYTHON",
    "TET4D_ENVIRONMENT_MODE",
    "TET4D_RESOLVED_PYTHON",
    "BASH_ENV",
    "ENV",
    "MAKEFLAGS",
    "MFLAGS",
    "GNUMAKEFLAGS",
    "MAKEFILES",
)


def scrubbed_environment(**updates: str) -> dict[str, str]:
    return {
        **{k: v for k, v in os.environ.items() if k not in INTERPRETER_SELECTORS},
        **updates,
    }


def write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def read_project(root: Path) -> dict:
    return json.loads((root / PROJECT_REL).read_text())


def write_project(root: Path, payload: dict) -> None:
    write(root / PROJECT_REL, payload)


def baseline_script(source: str) -> Path:
    """Read only the selected payload baseline or pinned compatibility archive."""
    from tools.workspace_governance.release import BASELINE_FILES

    destinations = dict(BASELINE_FILES)
    if source not in destinations:
        raise ValueError("baseline fixture source is undeclared")
    selected_root = PACK.parents[1]
    release_manifest = selected_root.parent / "release.json"
    if selected_root.name == "payload" and release_manifest.is_file():
        manifest = json.loads(release_manifest.read_bytes())
        relative = destinations[source]
        rows = [row for row in manifest["files"] if row["path"] == relative]
        root = selected_root
    else:
        root = selected_root / "compatibility/1.1.3"
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        active = json.loads(
            (selected_root / ".governance/distribution.json").read_bytes()
        )
        identity = sha256(
            (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
        ).hexdigest()
        if identity != active["legacy_manifest_sha256"]:
            raise ValueError("compatibility fixture identity differs")
        relative = source
        rows = [row for row in manifest["files"] if row["path"] == source]
    if len(rows) != 1:
        raise ValueError("baseline fixture lacks one declared source")
    target = root / relative
    for component in (target, *target.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError("baseline fixture contains a symlink")
    if (
        root.is_symlink()
        or not target.resolve().is_relative_to(root.resolve())
        or not target.is_file()
        or sha256(target.read_bytes()).hexdigest() != rows[0]["sha256"]
        or stat.S_IMODE(target.stat().st_mode) != rows[0]["mode"]
    ):
        raise ValueError("baseline fixture bytes or mode differ")
    return target


def build_checkout(
    root: Path,
    *,
    profile: str = "python",
    workspace_id: str = WORKSPACE_ID,
    project_id: str = "fixture-app",
) -> Path:
    from tools.workspace_governance.cli.gov import _sync
    from tools.workspace_governance.resolver.core import GovernanceResolver

    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        PACK,
        root / "tools/workspace_governance",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".ruff_cache"),
    )
    for directory in (".governance", "docs", "scripts"):
        (root / directory).mkdir(exist_ok=True)
    (root / "AGENTS.md").write_text(
        "Use declared governance and preserve human authority.\n"
    )
    (root / "CLAUDE.md").write_text("Follow AGENTS.md.\n")
    from tools.workspace_governance import hook_adapters

    for name, data in hook_adapters.render(root).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    (root / "docs/governance.md").write_text("Require declared evidence for changes.\n")
    (root / "docs/design.md").write_text("The product owner chooses behavior.\n")
    (root / "docs/api").mkdir()
    (root / "docs/api/index.md").write_text("Synthetic API contract.\n")
    (root / "docs/plan.md").write_text("A synthetic plan.\n")
    (root / "CURRENT_STATE.md").write_text("Synthetic checkpoint.\n")
    (root / "scripts/check.py").write_text("raise SystemExit(0)\n")
    (root / "docs/generated.md").write_text("current\n")
    interpreter = root / ".governance/python"
    interpreter.symlink_to(sys.executable)
    gov = root / "gov"
    gov.write_text(
        '#!/bin/sh\nexec "${GOVERNANCE_PYTHON:-'
        + sys.executable
        + '}" "$(dirname "$0")/tools/workspace_governance/cli/bootstrap.py" "$@"\n'
    )
    gov.chmod(0o755)
    authorities = [
        {
            "authority_id": "engineering",
            "authority_type": "human",
            "scope": "engineering",
            "source": "docs/governance.md",
            "exclusive": True,
            "canonical_governance": True,
            "source_type": "file",
            "legacy_key": "engineering",
        },
        {
            "authority_id": "product-design",
            "authority_type": "human",
            "scope": "product design",
            "source": "docs/design.md",
            "exclusive": True,
            "canonical_governance": False,
            "source_type": "file",
            "legacy_key": "design",
        },
        {
            "authority_id": "compatibility",
            "authority_type": "compatibility",
            "scope": "compatibility",
            "source": ".governance/legacy.json",
            "exclusive": True,
            "canonical_governance": False,
            "source_type": "file",
            "legacy_key": "legacy",
        },
        {
            "authority_id": "engineering-alias",
            "authority_type": "alias",
            "scope": "engineering alias",
            "source": "docs/governance.md",
            "exclusive": False,
            "canonical_governance": False,
            "source_type": "alias",
            "alias_of": "engineering",
            "legacy_key": "engineering",
        },
    ]
    authorities.append(
        {
            "authority_id": "api-contracts",
            "authority_type": "human",
            "scope": "api contract directory",
            "source": "docs/api",
            "exclusive": True,
            "canonical_governance": False,
            "source_type": "directory",
            "legacy_key": "api",
        }
    )
    routes = {
        "development": {
            "authority_refs": ["engineering", "product-design", "api-contracts"],
            "dispatch_paths": ["AGENTS.md"],
            "typical_verification_requirements": ["run product checks"],
            "environment_tools": ["fixture-tool"],
        },
        "governance": {
            "authority_refs": ["engineering", "compatibility"],
            "dispatch_paths": ["AGENTS.md"],
            "typical_verification_requirements": ["run governance checks"],
            "environment_tools": [],
        },
    }
    project = {
        "schema_version": 1,
        "project": {"id": project_id, "name": "Synthetic application"},
        "authorities": authorities,
        "required_authorities": ["engineering"],
        "routes": routes,
        "verification": {
            "canonical": {
                "authority_ref": "compatibility",
                "json_pointer": "/verification/canonical",
            },
            "full": {
                "authority_ref": "compatibility",
                "json_pointer": "/verification/full",
            },
            "targeted": "python scripts/check.py",
            "rule": "Select evidence from the actual change.",
        },
        "execution": {
            "default_mode": "FEATURE",
            "profiles": {
                mode: {
                    "exploration": ["inspect owner"],
                    "routes": ["development", "governance"],
                    "final_evidence_rule": "Report actual evidence.",
                }
                for mode in MODES
            },
            "representative_scenarios": [
                {
                    "id": "governance-change",
                    "match_all": ["governance", "change"],
                    "mode": "STRUCTURAL_CHANGE",
                    "priority": 20,
                    "routes": ["governance"],
                },
                {
                    "id": "development-change",
                    "match_all": ["development"],
                    "mode": "FEATURE",
                    "priority": 10,
                    "routes": ["development"],
                },
            ],
        },
        "artifact_roles": {"docs/plan.md": "planning_document"},
        "role_bootstrap": {
            "instruction_roots": ["AGENTS.md", "CLAUDE.md"],
            "bookkeeping": ["CURRENT_STATE.md"],
        },
        "generated_surfaces": [
            {
                "surface_id": "legacy-routes",
                "kind": "compatibility_facade",
                "source": PROJECT_REL + "#/routes",
                "target": ".governance/legacy.json#/codex_routing/routes",
                "transform": "authority-legacy-key-route-v1",
            },
            {
                "surface_id": "generated-reference",
                "kind": "generated_output",
                "source": "scripts/check.py",
                "target": "docs/generated.md",
                "check_command": ["{python}", "scripts/check.py", "--check"],
                "timeout_seconds": 12,
            },
        ],
        "environment": {
            "profile": profile,
            "preferred": ".governance/python",
            "interpreter_override": "FIXTURE_PYTHON",
            "route_tools": {
                "fixture-tool": {
                    "override": "FIXTURE_TOOL",
                    "candidates": ["python3"],
                    "version_args": ["--version"],
                }
            },
        },
        "sanitation": {
            "secret_scanner": "scripts/check.py",
            "repository_check": "scripts/check.py",
            "bounded_diagnostics": 10,
        },
        "canonical_owner_set": {
            "authority_ref": "compatibility",
            "json_pointer": "/owners",
        },
    }
    if profile == "python":
        (root / "src/fixture_runtime").mkdir(parents=True)
        (root / "src/fixture_runtime/__init__.py").write_text(
            "CHECKOUT = " + repr(root.name) + "\n"
        )
        (root / "pyproject.toml").write_text(
            '[project]\nname="fixture-runtime"\nversion="0.0.0"\nrequires-python=">=3.11"\ndependencies=["packaging>=23"]\n'
        )
        authorities.append(
            {
                "authority_id": "application-metadata",
                "authority_type": "project_metadata",
                "scope": "application metadata",
                "source": "pyproject.toml",
                "exclusive": True,
                "canonical_governance": False,
                "source_type": "file",
            }
        )
        project["environment"].update(
            {
                "python_requires": {
                    "authority_ref": "application-metadata",
                    "source_field": "project.requires-python",
                },
                "dependency_authority": {
                    "authority_ref": "application-metadata",
                    "source": "pyproject.toml",
                },
                "execution_mode": {
                    "default": "source",
                    "override": "FIXTURE_EXECUTION_MODE",
                },
                "package_import": "fixture_runtime",
                "editable_source": "src/fixture_runtime",
                "critical_packages": ["fixture_runtime"],
            }
        )
    from tools.workspace_governance.validators.core import _route_facade

    write(
        root / ".governance/legacy.json",
        {
            "verification": {
                "canonical": "python scripts/check.py",
                "full": "python scripts/check.py --full",
                "alternate": "python scripts/check.py --alternate",
            },
            "owners": {"engineering": "docs/governance.md"},
            "alternate_owners": {"engineering": "docs/governance.md"},
            "authority_model": {"owners": {"engineering": "docs/governance.md"}},
            "codex_routing": {"routes": _route_facade(project)},
        },
    )
    write_project(root, project)
    write(
        root / ".governance/workspace.json",
        {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "governance_pack": {"required_schema": 1, "lock": ".governance/lock.json"},
            "projects": [
                {"id": project_id, "repository": ".", "manifest": PROJECT_REL}
            ],
            "relationships": [],
            "defaults": {"project": project_id},
        },
    )
    write(root / ".governance/lock.json", {"pack_path": "tools/workspace_governance"})
    _sync(root, GovernanceResolver.for_root(root))
    return root
