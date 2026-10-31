"""The immutable gate enforces consumer runtime style, independently of pytest."""

from pathlib import Path

import pytest
from tools.policy_kit_distribution import verification
from tools.workspace_governance.fleet_execution import safe_environment


@pytest.mark.parametrize("defect", ["none", "format", "complexity", "syntax"])
def test_real_style_phases_reject_runtime_defects_and_still_execute_tests(
    tmp_path, defect
):
    runtime = tmp_path / "runtime"
    tests = tmp_path / "tests"
    runtime.mkdir()
    tests.mkdir()
    (runtime / ".ruff.toml").write_text(
        'line-length = 88\ntarget-version = "py311"\n[lint]\nextend-select = ["C901"]\n'
    )
    source = "VALUE = 1\n"
    if defect == "format":
        source = 'VALUE={"private_source_value":1}\n'
    elif defect == "complexity":
        source = (
            "def complex(value):\n"
            + "".join(f"    if value == {i}:\n        return {i}\n" for i in range(11))
            + "    return -1\n"
        )
    elif defect == "syntax":
        source = "def broken(\n"
    (runtime / "subject.py").write_text(source)
    (tests / "test_product.py").write_text(
        "def test_behavior():\n    assert 2 + 2 == 4\n"
    )
    environment = safe_environment()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # -I must ignore an ambient package that would replace the real verifier.
    (tmp_path / "ruff.py").write_text("raise RuntimeError('foreign verifier')\n")
    environment["PYTHONPATH"] = str(tmp_path)
    phases = verification._run_phases(runtime, tmp_path / "control", tests, environment)
    assert [phase["name"] for phase in phases] == ["lint", "format", "pytest"]
    assert phases[-1]["exit_code"] == 0
    assert phases[-1]["test_counts"] == {"passed": 1}
    if defect == "none":
        assert all(phase["exit_code"] == 0 for phase in phases)
    else:
        assert any(phase["exit_code"] != 0 for phase in phases[:2])
        assert any(phase["diagnostics"] for phase in phases[:2])
        assert "private_source_value" not in str(phases)
        assert all(
            diagnostic.get("path", "subject.py") == "subject.py"
            for phase in phases[:2]
            for diagnostic in phase["diagnostics"]
        )


def test_phase_timeout_is_a_failure_with_bounded_diagnostics():
    result = {
        "stdout_preview": b"",
        "stderr_preview": b"",
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "stdout_sha256": "a",
        "stderr_sha256": "b",
        "duration_seconds": 1,
        "exit_code": -15,
        "timed_out": True,
    }
    phase = verification._phase_result("lint", result, Path("/runtime"))
    assert phase["exit_code"] == 124
    assert phase["diagnostics"] == [{"code": "timeout"}]
