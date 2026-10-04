"""Run the separately packaged policy-kit gate on an explicit selection."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import sys
import tempfile
from pathlib import Path

from .contract import active_configuration, anchor_components, canonical, digest


def dependency_check(contract: dict) -> None:
    import shutil

    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    if (
        not isinstance(contract, dict)
        or contract.get("schema_version") != 1
        or not isinstance(contract.get("python_requires"), str)
        or not isinstance(contract.get("requirements"), list)
        or not isinstance(contract.get("executables"), list)
    ):
        raise ValueError("invalid verification dependency contract")
    if Version(".".join(str(x) for x in sys.version_info[:3])) not in SpecifierSet(
        contract["python_requires"]
    ):
        raise ValueError("verification Python requirement is unavailable")
    for text in contract["requirements"]:
        requirement = Requirement(text)
        installed = Version(importlib.metadata.version(requirement.name))
        if installed not in requirement.specifier:
            raise ValueError(
                "declared verification dependency is unavailable: " + requirement.name
            )
    approved_path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    for executable in contract["executables"]:
        if (
            executable not in {"git", "bash"}
            or shutil.which(executable, path=approved_path) is None
        ):
            raise ValueError("declared verification executable is unavailable")


PUBLIC_CONTROL_SCRIPTS = (
    "scripts/distribution_bootstrap.py",
    "scripts/verify_policy_kit.sh",
    "scripts/check_policy_kit_selftest.sh",
    "scripts/sync_workspace_policy.sh",
    "scripts/audit_workspace_policy.sh",
)


def candidate_component_identity(root: Path, name: str) -> str:
    from tools.workspace_governance.release import collect_component

    directory = "tests" if name == "verification" else "tools/policy_kit_distribution"
    _, declaration = collect_component(root / directory, name)
    rows = []
    for row in declaration["files"]:
        path = directory + "/" + row["path"]
        rows.append({**row, "path": path, "source": name + "/" + path})
    if name == "distribution":
        from .contract import confined

        for path in PUBLIC_CONTROL_SCRIPTS:
            source = confined(root, path)
            mode = source.stat().st_mode & 0o777
            if mode not in {0o644, 0o755}:
                raise ValueError("unsupported control script mode")
            rows.append(
                {
                    "path": path,
                    "source": name + "/" + path,
                    "sha256": digest(source.read_bytes()),
                    "mode": mode,
                }
            )
    rows.sort(key=lambda row: row["path"])
    return digest(canonical(rows))


def selected_components(
    kit: Path, candidate: Path | None
) -> tuple[Path, Path, Path, dict]:
    if candidate is not None:
        root = candidate.resolve()
        runtime = root / "tools/workspace_governance"
        control = root / "tools/policy_kit_distribution"
        tests = root / "tests"
        anchor_components(runtime, control)
        from tools.workspace_governance.release import (
            collect_payload,
        )

        payload, manifest = collect_payload(runtime, root)
        identity = {
            "selection": "explicit-candidate",
            "runtime_identity": digest(
                canonical(
                    {name: digest(data) for name, data in sorted(payload.items())}
                )
            ),
            "runtime_version": manifest["version"],
            "verification_identity": candidate_component_identity(root, "verification"),
            "distribution_identity": candidate_component_identity(root, "distribution"),
        }
        return runtime, control, tests, identity
    _, _, path, release = active_configuration(kit)
    return (
        path.parent / "payload/tools/workspace_governance",
        path.parent / "distribution/tools/policy_kit_distribution",
        path.parent / "verification/tests",
        {
            "selection": "active-immutable",
            "release_identity": digest(canonical(release)),
            "runtime_identity": release["provenance"]["payload_identity"],
            "verification_identity": release["verification"]["identity_sha256"],
            "distribution_identity": release["distribution"]["identity_sha256"],
        },
    )


def run_gate(kit: Path, candidate: Path | None = None) -> dict:
    runtime, control, tests, identity = selected_components(kit, candidate)
    anchor_components(runtime, control)
    from tools.workspace_governance.fleet_execution import run_digest, safe_environment

    dependency_check(
        json.loads((tests / "verification-dependencies.json").read_bytes())
    )
    environment = safe_environment()
    environment.update(
        POLICY_KIT_RUNTIME_ROOT=str(runtime),
        POLICY_KIT_DISTRIBUTION_ROOT=str(control),
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    # -I ignores ambient import/startup paths. The child anchors only selected packages.
    program = """import sys,types,pathlib
runtime,control,tests,temp=sys.argv[1:]
namespace=types.ModuleType('tools');namespace.__package__='tools'
namespace.__path__=[str(pathlib.Path(runtime).parents[1]/'tools'),str(pathlib.Path(control).parents[1]/'tools')]
sys.modules['tools']=namespace
sys.path[:0]=[str(pathlib.Path(runtime).parents[1]),str(pathlib.Path(control).parents[1])]
import pytest
raise SystemExit(pytest.main([tests,'-q','-p','no:cacheprovider','--basetemp',temp]))
"""
    with tempfile.TemporaryDirectory(prefix="policy-kit-gate-") as temporary:
        result = run_digest(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                program,
                str(runtime),
                str(control),
                str(tests),
                temporary + "/pytest",
            ],
            Path(temporary),
            environment,
            1800,
            diagnostic_limit=128 * 1024,
        )
    _, _, _, observed_identity = selected_components(kit, candidate)
    if observed_identity != identity:
        raise ValueError("selected components changed during verification")
    preview = result.pop("stdout_preview") + b"\n" + result.pop("stderr_preview")
    counts = {}
    for count, outcome in re.findall(
        rb"(\d+) (passed|failed|error|errors|skipped)\b", preview
    ):
        counts[outcome.decode()] = int(count)
    status = 124 if result["timed_out"] else result["exit_code"]
    return {
        "kind": "policy-kit-verification",
        **identity,
        **result,
        "test_counts": counts,
        "exit_code": status,
        "ok": status == 0,
        "product_execution": "not_requested",
    }


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid verification arguments")


def main(kit: Path, argv: list[str] | None = None) -> int:
    parser = Parser(description=__doc__)
    parser.add_argument("--candidate", type=Path)
    args = parser.parse_args(argv)
    result = run_gate(kit, args.candidate)
    print(json.dumps(result, sort_keys=True))
    return result["exit_code"]
