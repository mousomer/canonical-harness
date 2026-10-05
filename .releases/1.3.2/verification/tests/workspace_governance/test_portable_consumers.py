"""Real contract boundaries for consumers with no Python application."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from support import scrubbed_environment, write
from tools.workspace_governance.resolver.core import GovernanceResolver
from tools.workspace_governance.validators import core


def test_native_consumer_checks_resolves_and_certifies_without_pyproject(
    native_consumer: Path,
) -> None:
    resolver = GovernanceResolver.for_root(native_consumer)
    assert not (native_consumer / "pyproject.toml").exists()
    assert resolver.check() == []
    resolved = resolver.resolve()["entries"]
    assert resolved["verification.full"] == {
        "value": "python scripts/check.py --full",
        "owner": "compatibility",
        "source": ".governance/legacy.json#/verification/full",
    }
    _, project, _ = resolver.load()
    result, issues = core.doctor(
        native_consumer, project, None, scrubbed_environment(), route="development"
    )
    assert issues == []
    assert result["profile"] == "tools"
    assert (
        result["python_requires_source"] == "tools/workspace_governance/MANIFEST.json"
    )
    assert core.binding_environment(native_consumer, project) == {}


def test_bootstrap_and_env_need_no_application_metadata(native_consumer: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "tools/workspace_governance/cli/bootstrap.py",
            "env",
            "--json",
        ],
        cwd=native_consumer,
        env=scrubbed_environment(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "PYTHON_BIN": str(native_consumer / ".governance/python")
    }


@pytest.mark.parametrize(
    "defect", ["mixed_command", "empty_owner", "python_missing", "tools_app_field"]
)
def test_incomplete_or_ambiguous_bindings_fail(
    native_consumer: Path, defect: str
) -> None:
    path = native_consumer / ".governance/project.json"
    project = json.loads(path.read_text())
    if defect == "mixed_command":
        project["verification"]["full"]["command"] = "python scripts/check.py"
    elif defect == "empty_owner":
        project["canonical_owner_set"] = {}
    elif defect == "python_missing":
        project["environment"].pop("profile")
    else:
        project["environment"]["package_import"] = "made_up"
    write(path, project)
    issues = GovernanceResolver.for_root(native_consumer).check()
    assert issues
    assert "CONFLICTING_VALUE" in {issue.code for issue in issues}


def test_generated_output_accepts_current_bytes_and_rejects_drift(
    native_consumer: Path,
) -> None:
    (native_consumer / "docs/generated.md").write_text("current\n")
    (native_consumer / "scripts/render.py").write_text(
        "from pathlib import Path\n"
        "raise SystemExit(0 if Path('docs/generated.md').read_text() == 'current\\n' else 1)\n"
    )
    path = native_consumer / ".governance/project.json"
    project = json.loads(path.read_text())
    project["generated_surfaces"] = [
        {
            "surface_id": "reference",
            "kind": "generated_output",
            "source": "scripts/render.py",
            "target": "docs/generated.md",
            "check_command": ["{python}", "scripts/render.py", "--check"],
        }
    ]
    write(path, project)
    resolver = GovernanceResolver.for_root(native_consumer)
    assert resolver.check(generated=True) == []
    assert resolver.role_index().resolve("docs/generated.md").role == "generated"
    (native_consumer / "docs/generated.md").write_text("drift\n")
    issues = resolver.check(generated=True)
    assert [issue.fact for issue in issues] == ["reference"]
    assert "check failed" in issues[0].reason
    assert (native_consumer / "docs/generated.md").read_text() == "drift\n"


def test_generated_output_rejects_shell_expression_and_foreign_renderer(
    native_consumer: Path,
) -> None:
    surface = {
        "surface_id": "reference",
        "kind": "generated_output",
        "source": "scripts/check.py",
        "target": "docs/governance.md",
        "check_command": ["sh", "-c", "exit 0"],
    }
    project = GovernanceResolver.for_root(native_consumer).load()[1]
    issues = core._generated_output_issue(native_consumer, project, surface)
    assert len(issues) == 1
    assert "declared renderer" in issues[0].reason
    surface["source"] = "../outside.py"
    assert (
        "repository-relative"
        in core._generated_output_issue(native_consumer, project, surface)[0].reason
    )


def _renderer(root: Path, body: str) -> GovernanceResolver:
    (root / "docs/generated.md").write_text("current\n")
    (root / "scripts/render.py").write_text(body)
    path = root / ".governance/project.json"
    project = json.loads(path.read_text())
    project["generated_surfaces"] = [
        {
            "surface_id": "reference",
            "kind": "generated_output",
            "source": "scripts/render.py",
            "target": "docs/generated.md",
            "check_command": ["{python}", "scripts/render.py", "--check"],
        }
    ]
    write(path, project)
    return GovernanceResolver.for_root(root)


@pytest.mark.parametrize("selection", ["preferred", "local"])
def test_generated_renderer_uses_the_certified_consumer_interpreter(
    native_consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    wrapper = native_consumer / ".governance/selected"
    wrapper.write_text(
        f'#!/bin/sh\nexport REVIEW_TOOLCHAIN=selected\nexec "{sys.executable}" "$@"\n'
    )
    wrapper.chmod(0o755)
    resolver = _renderer(
        native_consumer,
        "import os\nraise SystemExit(0 if os.environ.get('REVIEW_TOOLCHAIN') == 'selected' else 7)\n",
    )
    if selection == "preferred":
        path = native_consumer / ".governance/project.json"
        project = json.loads(path.read_text())
        project["environment"]["preferred"] = ".governance/selected"
        write(path, project)
    else:
        write(
            native_consumer / ".governance/workspace.local.json",
            {"schema_version": 1, "interpreter": ".governance/selected"},
        )
    # Bootstrap selection must not replace the consumer's interpreter.
    monkeypatch.setenv("GOVERNANCE_PYTHON", sys.executable)
    resolver = GovernanceResolver.for_root(native_consumer)
    _, project, local = resolver.load()
    result, issues = core.doctor(native_consumer, project, local)
    assert issues == []
    assert result["interpreter"] == str(wrapper)
    assert resolver.check(generated=True) == []


def test_generated_renderer_carries_local_python_source_binding(
    native_consumer: Path,
) -> None:
    (native_consumer / "src/demo_runtime").mkdir(parents=True)
    (native_consumer / "src/demo_runtime/__init__.py").write_text(
        "SENTINEL = 'this checkout'\n"
    )
    (native_consumer / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.11"\n'
    )
    resolver = _renderer(
        native_consumer,
        "from pathlib import Path\nimport demo_runtime\n"
        "assert demo_runtime.SENTINEL == 'this checkout'\n"
        "assert Path(demo_runtime.__file__).resolve() == Path('src/demo_runtime/__init__.py').resolve()\n",
    )
    path = native_consumer / ".governance/project.json"
    project = json.loads(path.read_text())
    project["authorities"].append(
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
            "profile": "python",
            "python_requires": {
                "authority_ref": "application-metadata",
                "source_field": "project.requires-python",
            },
            "dependency_authority": {
                "authority_ref": "application-metadata",
                "source": "pyproject.toml",
            },
            "execution_mode": {
                "default": "installed",
                "override": "DEMO_EXECUTION_MODE",
            },
            "package_import": "demo_runtime",
            "editable_source": "src/demo_runtime",
            "critical_packages": ["demo_runtime"],
        }
    )
    write(path, project)
    write(
        native_consumer / ".governance/workspace.local.json",
        {"schema_version": 1, "execution_mode": "source"},
    )
    resolver = GovernanceResolver.for_root(native_consumer)
    _, project, local = resolver.load()
    result, issues = core.doctor(native_consumer, project, local)
    assert issues == []
    assert result["execution_mode"] == "source"
    assert resolver.check(generated=True) == []


def test_generated_renderer_diagnostics_do_not_disclose_output(
    native_consumer: Path,
) -> None:
    secret = "review-private-output-do-not-print"
    resolver = _renderer(
        native_consumer,
        f"import sys\nprint({secret!r})\nprint({secret!r}, file=sys.stderr)\nraise SystemExit(7)\n",
    )
    issues = resolver.check(generated=True)
    assert len(issues) == 1
    serialized = json.dumps(issues[0].to_dict())
    assert secret not in serialized
    assert "stdout_bytes=" in serialized and "stderr_sha256=" in serialized


@pytest.mark.parametrize(
    "mutation", ["authority", "same_bytes", "outside_symlink", "tracked_file"]
)
def test_generated_renderer_detects_protected_file_and_metadata_mutation(
    native_consumer: Path,
    mutation: str,
) -> None:
    if mutation == "authority":
        body = "from pathlib import Path\nPath('docs/governance.md').write_text('weakened')\n"
    elif mutation == "same_bytes":
        body = "from pathlib import Path\nPath('docs/generated.md').write_text('current\\n')\n"
    elif mutation == "outside_symlink":
        outside = native_consumer.parent / "outside-generated.md"
        outside.write_text("current\n")
        body = (
            "from pathlib import Path\nPath('docs/generated.md').unlink()\n"
            f"Path('docs/generated.md').symlink_to({str(outside)!r})\n"
        )
    else:
        (native_consumer / "docs/other.md").write_text("original\n")
        subprocess.run(["git", "init", "-q"], cwd=native_consumer, check=True)
        subprocess.run(["git", "add", "docs/other.md"], cwd=native_consumer, check=True)
        body = "from pathlib import Path\nPath('docs/other.md').write_text('changed')\n"
    resolver = _renderer(native_consumer, body)
    issues = resolver.check(generated=True)
    assert len(issues) == 1
    assert "modified protected inputs or outputs" in issues[0].reason
    if mutation == "outside_symlink":
        assert "confined=False" in issues[0].reason


@pytest.mark.parametrize(
    "linked_path", ["docs/generated.md", "scripts/render.py", "docs"]
)
def test_generated_checks_reject_initial_links_into_ignored_cache(
    native_consumer: Path,
    linked_path: str,
) -> None:
    resolver = _renderer(
        native_consumer,
        "from pathlib import Path\nPath('docs/generated.md').write_text('changed')\n",
    )
    subprocess.run(["git", "init", "-q"], cwd=native_consumer, check=True)
    (native_consumer / ".gitignore").write_text(".cache/\n")
    cache = native_consumer / ".cache"
    cache.mkdir()
    path = native_consumer / linked_path
    stored = cache / path.name
    path.rename(stored)
    path.symlink_to(stored, target_is_directory=stored.is_dir())
    issues = resolver.check()
    assert len(issues) == 1
    assert "regular files without symlink" in issues[0].reason
    assert (native_consumer / "docs/generated.md").read_text() == "current\n"


def test_generated_checker_protects_dereferenced_authority_inputs(
    native_consumer: Path,
) -> None:
    resolver = _renderer(
        native_consumer,
        "from pathlib import Path\nPath('docs/governance.md').write_text('changed')\n",
    )
    subprocess.run(["git", "init", "-q"], cwd=native_consumer, check=True)
    (native_consumer / ".gitignore").write_text(".cache/\n")
    cache = native_consumer / ".cache"
    cache.mkdir()
    authority = native_consumer / "docs/governance.md"
    stored = cache / "governance.md"
    authority.rename(stored)
    authority.symlink_to(stored)
    issues = resolver.check(generated=True)
    assert len(issues) == 1
    assert "modified protected inputs or outputs" in issues[0].reason


def test_structural_context_needs_only_bootstrap_and_generated_check_certifies_consumer(
    native_consumer: Path,
) -> None:
    resolver = _renderer(native_consumer, "raise SystemExit(7)\n")
    (native_consumer / ".governance/python").unlink()
    # The structural contract remains resolvable with no consumer toolchain.
    assert resolver.check() == []
    assert resolver.resolve()["entries"]["project.id"]["value"] == "fixture-native"
    for command in (["check"], ["resolve"], ["check", "--generated"]):
        result = subprocess.run(
            [
                sys.executable,
                "tools/workspace_governance/cli/bootstrap.py",
                *command,
                "--json",
            ],
            cwd=native_consumer,
            env=scrubbed_environment(),
            text=True,
            capture_output=True,
            check=False,
        )
        if "--generated" in command:
            assert result.returncode == 1
            assert "python.interpreter" in result.stdout
        else:
            assert result.returncode == 0, result.stdout + result.stderr


def test_generated_drift_requires_explicit_check_but_declarations_are_always_validated(
    native_consumer: Path,
) -> None:
    resolver = _renderer(native_consumer, "raise SystemExit(7)\n")
    assert resolver.check() == []
    assert [issue.fact for issue in resolver.check(generated=True)] == ["reference"]
    path = native_consumer / ".governance/project.json"
    project = json.loads(path.read_text())
    project["generated_surfaces"][0]["check_command"] = ["sh", "-c", "exit 0"]
    write(path, project)
    assert [issue.fact for issue in resolver.check()] == ["reference"]
