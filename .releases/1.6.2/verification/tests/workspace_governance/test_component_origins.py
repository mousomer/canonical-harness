"""The portable gate must exercise selected bytes, not ambient installations."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from support import PACK, ROOT, scrubbed_environment


def test_selected_runtime_and_catalog_origins():
    from tools.workspace_governance import telemetry
    from tools.workspace_governance.cli import gov
    from tools.workspace_governance.resolver import core as resolver
    from tools.workspace_governance.validators import core as validator

    for module in (telemetry, gov, resolver, validator):
        assert Path(module.__file__).resolve().is_relative_to(PACK)
    for relative in (
        "schemas/project.schema.json",
        "field-usage.json",
        "synthetic-fixtures/contradictions.json",
    ):
        source = PACK / relative
        assert source.is_file() and not source.is_symlink()
        assert source.resolve().is_relative_to(PACK)
        assert isinstance(json.loads(source.read_text()), dict)


def test_suite_ignores_poisoned_ambient_editable_import_and_overlay(tmp_path):
    poison = tmp_path / "ambient-editable"
    (poison / "tools/workspace_governance").mkdir(parents=True)
    (poison / "tools/__init__.py").write_text(
        'raise RuntimeError("ambient editable tools selected")\n'
    )
    (poison / "tools/workspace_governance/__init__.py").write_text(
        'raise RuntimeError("foreign runtime selected")\n'
    )
    config = tmp_path / "poison-config"
    (config / "workspace-governance").mkdir(parents=True)
    (config / "workspace-governance/fixture-workspace.local.json").write_text(
        "{ invalid ambient overlay\n"
    )
    env = scrubbed_environment(
        PYTHONPATH=str(poison),
        XDG_CONFIG_HOME=str(config),
        XDG_STATE_HOME=str(tmp_path / "poison-state"),
        POLICY_KIT_RUNTIME_ROOT=str(PACK),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            str(Path(__file__).resolve()),
            "-k",
            "selected_runtime_and_catalog_origins",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "poison-state").exists()
    assert (
        config / "workspace-governance/fixture-workspace.local.json"
    ).read_text() == "{ invalid ambient overlay\n"


def test_preimported_foreign_runtime_is_rejected(tmp_path):
    foreign = tmp_path / "foreign"
    shutil.copytree(
        PACK,
        foreign / "tools/workspace_governance",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    conftest = Path(__file__).with_name("conftest.py")
    script = (
        "import importlib.util,sys; "
        "sys.path.insert(0, " + repr(str(foreign)) + "); "
        "from tools.workspace_governance.resolver import core; "
        'spec=importlib.util.spec_from_file_location("verification_contract", '
        + repr(str(conftest))
        + "); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "module.pytest_sessionstart(None)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=scrubbed_environment(POLICY_KIT_RUNTIME_ROOT=str(PACK)),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "Runtime module came from outside the selected component" in result.stderr
