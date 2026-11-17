"""Equivalent renderer contracts execute once and protect every declared output."""

from __future__ import annotations

import pytest
from support import read_project, write_project

from tools.workspace_governance.runtime_floor import numeric_python_floor
from tools.workspace_governance.validators import core


def surfaces(checkout, script="raise SystemExit(0)\n"):
    renderer = checkout / "scripts/render.py"
    renderer.write_text(script)
    for name in ("one.txt", "two.txt"):
        (checkout / name).write_text(name)
    project = read_project(checkout)
    project["generated_surfaces"] = [
        {
            "surface_id": name,
            "kind": "generated_output",
            "source": "scripts/render.py",
            "target": name,
            "check_command": ["{python}", "scripts/render.py", "--check"],
            "timeout_seconds": 3,
        }
        for name in ("one.txt", "two.txt")
    ]
    write_project(checkout, project)
    return project


def runs(monkeypatch):
    calls = []
    real = core.subprocess.run

    def observe(argv, **kwargs):
        if len(argv) > 1 and argv[1] == "scripts/render.py":
            calls.append((argv, kwargs))
        return real(argv, **kwargs)

    monkeypatch.setattr(core.subprocess, "run", observe)
    return calls


def test_equal_contract_once_with_all_targets(native_consumer, monkeypatch):
    project = surfaces(native_consumer)
    calls = runs(monkeypatch)
    assert (
        core._generated_surface_issues(native_consumer, project, generated=True) == []
    )
    assert len(calls) == 1


@pytest.mark.parametrize("difference", ["timeout", "argv", "environment"])
def test_distinct_contracts_do_not_share_execution(
    native_consumer, monkeypatch, difference
):
    project = surfaces(native_consumer)
    if difference == "timeout":
        project["generated_surfaces"][1]["timeout_seconds"] = 4
    elif difference == "argv":
        project["generated_surfaces"][1]["check_command"].append("--another")
    else:
        count = iter(("first", "second"))
        monkeypatch.setattr(
            core,
            "binding_environment",
            lambda *args, **kwargs: {"FIXTURE_BINDING": next(count)},
        )
    write_project(native_consumer, project)
    calls = runs(monkeypatch)
    assert (
        core._generated_surface_issues(native_consumer, project, generated=True) == []
    )
    assert len(calls) == 2


@pytest.mark.parametrize(
    "script",
    [
        "raise SystemExit(3)\n",
        "from pathlib import Path\nPath('two.txt').write_text('changed')\n",
    ],
)
def test_group_failure_diagnoses_every_surface(native_consumer, monkeypatch, script):
    project = surfaces(native_consumer, script)
    calls = runs(monkeypatch)
    issues = core._generated_surface_issues(native_consumer, project, generated=True)
    assert {issue.fact for issue in issues} == {"one.txt", "two.txt"}
    assert len(calls) == 1
    assert all(issue.code == "CONFLICTING_VALUE" for issue in issues)


def test_every_declaration_preflighted_before_execution(native_consumer, monkeypatch):
    project = surfaces(native_consumer)
    (native_consumer / "two.txt").unlink()
    calls = runs(monkeypatch)
    issues = core._generated_surface_issues(native_consumer, project, generated=True)
    assert issues and calls == []


@pytest.mark.parametrize(
    "version,requirement,expected",
    [
        ("3.14.0rc1", ">=3.11", True),
        ("3.14.0rc1", ">=3.14.0", True),
        ("3.14.0a2", ">=3.15", False),
        ("3.11", ">=3.11.0", True),
        ("3.10.99", ">=3.11", False),
    ],
)
def test_numeric_tools_floor_accepts_prerelease_release_numbers(
    version, requirement, expected
):
    assert numeric_python_floor(version, requirement) is expected


def test_numeric_tools_floor_refuses_compound_application_specifiers():
    with pytest.raises(ValueError):
        numeric_python_floor("3.14.0rc1", ">=3.11,<3.15")
