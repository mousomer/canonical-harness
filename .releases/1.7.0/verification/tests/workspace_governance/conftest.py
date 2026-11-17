"""Select candidate code before import; isolate all private process state."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[2]
RUNTIME = Path(
    os.environ.get("POLICY_KIT_RUNTIME_ROOT", str(KIT / "tools/workspace_governance"))
).resolve()
# The runtime layout is always tools/workspace_governance, including the immutable payload.
SELECTED_ROOT = RUNTIME.parents[1]
sys.path.insert(0, str(SELECTED_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# A later regular `tools` package can supersede a namespace even when our root
# is first on sys.path. Anchor the selected namespace before runtime imports.
# Preloaded runtime modules remain intact so origin validation rejects them.
if "tools" not in sys.modules:
    namespace = types.ModuleType("tools")
    namespace.__package__ = "tools"
    namespace.__path__ = [str(SELECTED_ROOT / "tools")]
    sys.modules["tools"] = namespace


def pytest_sessionstart(session):
    from tools.workspace_governance import telemetry
    from tools.workspace_governance.cli import gov
    from tools.workspace_governance.resolver import core as resolver
    from tools.workspace_governance.validators import core as validators

    for module in (telemetry, gov, resolver, validators):
        if not Path(module.__file__).resolve().is_relative_to(RUNTIME):
            raise pytest.UsageError(
                "Runtime module came from outside the selected component"
            )
    for relative in (
        "schemas/project.schema.json",
        "field-usage.json",
        "synthetic-fixtures/contradictions.json",
    ):
        path = RUNTIME / relative
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(RUNTIME)
            or not path.is_file()
        ):
            raise pytest.UsageError(
                "Verification catalog escaped the selected component"
            )


@pytest.fixture(autouse=True)
def isolated_user_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path.parent / (tmp_path.name + "-config")
    state = tmp_path.parent / (tmp_path.name + "-state")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    from support import INTERPRETER_SELECTORS

    for key in INTERPRETER_SELECTORS:
        monkeypatch.delenv(key, raising=False)
    yield config


@pytest.fixture
def checkout(tmp_path: Path):
    from support import build_checkout

    return build_checkout(tmp_path)


@pytest.fixture
def native_consumer(tmp_path: Path):
    from support import build_checkout

    return build_checkout(tmp_path, profile="tools", project_id="fixture-native")
