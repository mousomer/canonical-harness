"""Public stdlib dispatch selects immutable code before any packaged import."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from tools.policy_kit_distribution import contract


def bootstrap_source():
    root = Path(contract.__file__).resolve().parents[2]
    source = root / "scripts/distribution_bootstrap.py"
    assert source.is_file() and not source.is_symlink()
    return source


def synthetic_distribution(tmp_path):
    kit = tmp_path / "kit"
    script = kit / "scripts/distribution_bootstrap.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(bootstrap_source(), script)
    private = kit / "_machine/distribution.local.json"
    private.parent.mkdir(mode=0o700)
    private.write_text(json.dumps({"governance_python": sys.executable}))
    private.chmod(0o600)
    root = kit / "releases/synthetic"
    root.mkdir(parents=True)
    payloads = {
        "payload": {
            "tools/workspace_governance/__init__.py": b"",
        },
        "verification": {
            "tests/verification-dependencies.json": b'{"schema_version":1}\n',
        },
        "distribution": {
            "tools/policy_kit_distribution/__init__.py": b"",
            "tools/policy_kit_distribution/verification.py": (
                b"import json\n"
                b"def main(kit, argv=None):\n"
                b"    print(json.dumps({'selected_module': __file__}))\n"
                b"    return 0\n"
            ),
        },
    }
    components = {}
    for component, files in payloads.items():
        rows = []
        for relative, data in sorted(files.items()):
            source = component + "/" + relative
            target = root / source
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o644)
            rows.append(
                {
                    "source": source,
                    "path": relative,
                    "sha256": contract.digest(data),
                    "mode": 0o644,
                }
            )
        components[component] = {
            "schema_version": 1,
            "files": rows,
            "identity_sha256": contract.digest(contract.canonical(rows)),
        }
    release = {
        "schema_version": 2,
        "name": "workspace/policy-kit",
        "version": "synthetic",
        "revision": "synthetic",
        "release_root": ".",
        "files": components["payload"]["files"],
        "verification": components["verification"],
        "distribution": components["distribution"],
        "provenance": {"payload_identity": components["payload"]["identity_sha256"]},
    }
    manifest = root / "release.json"
    manifest.write_bytes(contract.canonical(release))
    active = kit / ".governance/distribution.json"
    active.parent.mkdir()
    active.write_bytes(
        contract.canonical(
            {
                "release_manifest": manifest.relative_to(kit).as_posix(),
                "release_manifest_sha256": contract.digest(contract.canonical(release)),
            }
        )
    )
    return kit, script, manifest, release


def run_bootstrap(script, *arguments, environment=None):
    return subprocess.run(
        [sys.executable, "-I", "-B", str(script), *arguments],
        env=dict(os.environ) if environment is None else environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )


def test_default_bootstrap_selects_immutable_control_with_poisoned_editable_paths(
    tmp_path,
):
    kit, script, manifest, _ = synthetic_distribution(tmp_path)
    poison = tmp_path / "poison"
    for root in (kit, poison):
        directory = root / "tools/policy_kit_distribution"
        directory.mkdir(parents=True)
        (directory.parent / "__init__.py").write_text(
            'raise RuntimeError("unselected tools package was executed")\n'
        )
        (directory / "__init__.py").write_text("")
        (directory / "verification.py").write_text(
            'raise RuntimeError("unselected mutable control was executed")\n'
        )
    environment = {**os.environ, "PYTHONPATH": str(poison)}
    result = run_bootstrap(script, "verify-kit", environment=environment)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "selected_module": str(
            manifest.parent
            / "distribution/tools/policy_kit_distribution/verification.py"
        )
    }
    assert "unselected" not in result.stdout + result.stderr


@pytest.mark.parametrize("component", ["payload", "verification", "distribution"])
def test_bootstrap_rejects_tampered_selected_component_before_import(
    tmp_path, component
):
    _kit, script, manifest, release = synthetic_distribution(tmp_path)
    assert run_bootstrap(script, "verify-kit").returncode == 0
    row = (
        release["files"][0]
        if component == "payload"
        else release[component]["files"][0]
    )
    (manifest.parent / row["source"]).write_text(
        'raise RuntimeError("tampered selected bytes executed")\n'
    )
    result = run_bootstrap(script, "verify-kit")
    assert result.returncode == 2
    assert not result.stdout
    assert json.loads(result.stderr) == {
        "kind": "distribution-error",
        "error_class": "ValueError",
    }
    assert "tampered selected bytes" not in result.stderr


def test_noncanonical_interpreter_cannot_create_a_reexec_loop(tmp_path):
    kit, script, _manifest, _release = synthetic_distribution(tmp_path)
    interpreter = Path(sys.executable)
    spelling = (
        str(interpreter.parent)
        + "/../"
        + interpreter.parent.name
        + "/"
        + interpreter.name
    )
    assert Path(spelling).is_file() and spelling != sys.executable
    (kit / "_machine/distribution.local.json").write_text(
        json.dumps({"governance_python": spelling})
    )
    result = run_bootstrap(script, "verify-kit")
    # Either explicit refusal or normalization is safe; repeated exec is not.
    assert result.returncode in {0, 2}
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.parametrize("local", [[], None, {"governance_python": 123}])
def test_malformed_private_config_refuses_without_implementation_import(
    tmp_path, local
):
    kit, script, _manifest, _release = synthetic_distribution(tmp_path)
    (kit / "_machine/distribution.local.json").write_text(json.dumps(local))
    result = run_bootstrap(script, "verify-kit")
    assert result.returncode == 2
    assert not result.stdout
    assert json.loads(result.stderr)["kind"] == "distribution-error"
    assert "Traceback" not in result.stderr
