"""Run the separately packaged policy-kit gate on an explicit selection."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import subprocess
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
        selected = shutil.which(executable, path=approved_path)
        if executable not in {"git", "bash", "make"} or selected is None:
            raise ValueError("declared verification executable is unavailable")
        if executable == "make":
            try:
                result = subprocess.run(
                    [selected, "--version"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                    env={"PATH": approved_path, "LANG": "C", "LC_ALL": "C"},
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValueError("verification requires GNU Make >=3.81") from exc
            version = re.search(r"^GNU Make ([0-9]+(?:\.[0-9]+)+)", result.stdout)
            if (
                result.returncode != 0
                or version is None
                or Version(version.group(1)) < Version("3.81")
            ):
                raise ValueError("verification requires GNU Make >=3.81")


PUBLIC_CONTROL_SCRIPTS = (
    "scripts/distribution_bootstrap.py",
    "scripts/verify_policy_kit.sh",
    "scripts/check_policy_kit_selftest.sh",
    "scripts/sync_workspace_policy.sh",
    "scripts/audit_workspace_policy.sh",
)

VERIFICATION_PROGRAM = """import importlib.util,pathlib,runpy,sys,sysconfig,types
roots={pathlib.Path(sysconfig.get_path(name)).resolve() for name in ('purelib','platlib')}
for name in ('pytest','packaging','ruff'):
    spec=importlib.util.find_spec(name)
    if spec is None or spec.origin is None or not any(pathlib.Path(spec.origin).resolve().is_relative_to(root) for root in roots):
        raise SystemExit('VERIFICATION_DEPENDENCY_ORIGIN_INVALID:'+name)
module,runtime,control,tests,temp,*arguments=sys.argv[1:]
if module=='ruff':
    sys.argv=['ruff',*arguments]
    runpy.run_module('ruff',run_name='__main__')
    raise SystemExit(0)
if module!='pytest':
    raise SystemExit('VERIFICATION_MODULE_INVALID')
import pytest
namespace=types.ModuleType('tools');namespace.__package__='tools'
namespace.__path__=[str(pathlib.Path(runtime).parents[1]/'tools'),str(pathlib.Path(control).parents[1]/'tools')]
sys.modules['tools']=namespace
sys.path[:0]=[str(pathlib.Path(runtime).parents[1]),str(pathlib.Path(control).parents[1])]
raise SystemExit(pytest.main([tests,'-q','-p','no:cacheprovider','--basetemp',temp]))
"""


def _style_diagnostics(output: bytes, runtime: Path) -> list[dict]:
    diagnostics = []
    for line in output.splitlines():
        try:
            row = json.loads(line)
            code = row["code"]
            if not isinstance(code, str) or not re.fullmatch(
                r"[A-Z][0-9]{3,4}|invalid-syntax|unformatted", code
            ):
                continue
            name = Path(row["filename"]).resolve().relative_to(runtime).as_posix()
            location = row["location"]
            if type(location["row"]) is not int or type(location["column"]) is not int:
                continue
            diagnostics.append(
                {
                    "code": code,
                    "path": name,
                    "line": location["row"],
                    "column": location["column"],
                }
            )
        except (ValueError, KeyError, TypeError):
            continue
        if len(diagnostics) == 20:
            break
    return diagnostics


def _phase_result(name: str, result: dict, runtime: Path) -> dict:
    output = result.pop("stdout_preview")
    errors = result.pop("stderr_preview")
    diagnostics = [] if name == "pytest" else _style_diagnostics(output, runtime)
    origin = re.search(
        rb"VERIFICATION_DEPENDENCY_ORIGIN_INVALID:(pytest|packaging|ruff)\b", errors
    )
    if origin:
        diagnostics.append(
            {"code": "dependency-origin-invalid", "dependency": origin[1].decode()}
        )
    status = 124 if result["timed_out"] else result["exit_code"]
    if status != 0 and not diagnostics:
        diagnostics.append(
            {"code": "timeout" if result["timed_out"] else "phase-failed"}
        )
    phase = {
        "name": name,
        **result,
        "exit_code": status,
        "diagnostics": diagnostics,
        "diagnostics_truncated": len(diagnostics) >= 20
        or result["stdout_bytes"] > len(output)
        or result["stderr_bytes"] > len(errors),
    }
    if name == "pytest":
        phase["test_counts"] = {
            outcome.decode(): int(count)
            for count, outcome in re.findall(
                rb"(\d+) (passed|failed|error|errors|skipped)\b",
                output + b"\n" + errors,
            )
        }
    return phase


def _run_phases(
    runtime: Path, control: Path, tests: Path, environment: dict
) -> list[dict]:
    from tools.workspace_governance.fleet_execution import run_digest

    phases = []
    with tempfile.TemporaryDirectory(prefix="policy-kit-gate-") as temporary:
        style = [
            "--config",
            str(runtime / ".ruff.toml"),
            "--no-cache",
            "--output-format",
            "json-lines",
            str(runtime),
        ]
        commands = (
            ("lint", "ruff", ["check", "--no-fix", *style], 120),
            ("format", "ruff", ["format", "--check", *style], 120),
            ("pytest", "pytest", [], 1800),
        )
        for name, module, arguments, timeout in commands:
            result = run_digest(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    VERIFICATION_PROGRAM,
                    module,
                    str(runtime),
                    str(control),
                    str(tests),
                    temporary + "/pytest",
                    *arguments,
                ],
                Path(temporary),
                environment,
                timeout,
                diagnostic_limit=128 * 1024,
            )
            phases.append(_phase_result(name, result, runtime))
    return phases


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
    from tools.workspace_governance.fleet_execution import safe_environment

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
    phases = _run_phases(runtime, control, tests, environment)
    _, _, _, observed_identity = selected_components(kit, candidate)
    if observed_identity != identity:
        raise ValueError("selected components changed during verification")
    tests_result = phases[-1]
    status = next(
        (phase["exit_code"] for phase in phases if phase["exit_code"] != 0), 0
    )
    return {
        "kind": "policy-kit-verification",
        **identity,
        **{
            key: tests_result[key]
            for key in (
                "stdout_bytes",
                "stderr_bytes",
                "stdout_sha256",
                "stderr_sha256",
            )
        },
        "output_scope": "pytest",
        "test_counts": tests_result["test_counts"],
        "phases": phases,
        "timed_out": any(phase["timed_out"] for phase in phases),
        "duration_seconds": round(
            sum(phase["duration_seconds"] for phase in phases), 6
        ),
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
