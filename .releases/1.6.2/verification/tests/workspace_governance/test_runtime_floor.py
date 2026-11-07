"""Numeric bootstrap floors are distinct from application PEP440 certification."""

import pytest
from tools.workspace_governance.dependency_compatibility import classify, compare
from tools.workspace_governance.runtime_floor import numeric_python_floor


@pytest.mark.parametrize(
    ("version", "requirement", "expected"),
    [
        ("3.14.0rc1", ">=3.11", True),
        ("3.14.0a2", ">=3.14", True),
        ("3.14.0b1", ">=3.14.1", False),
        ("3.14.0.dev2", ">=3.14", True),
        ("3.14.0+local", ">=3.14.0", True),
        ("3.10.99", ">=3.11", False),
        ("3.11", ">=3.11.0", True),
    ],
)
def test_numeric_floor_prereleases_and_zero_padding(version, requirement, expected):
    assert numeric_python_floor(version, requirement) is expected


@pytest.mark.parametrize(
    ("version", "requirement"),
    [
        ("unknown", ">=3.11"),
        ("3.14.0", ">=3.11,<4"),
        ("3.14.0", "~=3.11"),
        ("3.14.0", ">=3.11rc1"),
    ],
)
def test_numeric_floor_rejects_requirements_outside_its_contract(version, requirement):
    with pytest.raises(ValueError):
        numeric_python_floor(version, requirement)


@pytest.mark.parametrize(
    ("first", "second", "status"),
    [
        ("packaging>=23,<27", "packaging>=24,<26", "compatible"),
        ("packaging>=23,<24", "packaging>=24", "contradictory"),
        ("packaging==24", "packaging>=23", "unknown"),
        ("other>=1", "packaging>=23", "compatible"),
        ("not requirement ???", "packaging>=23", "unknown"),
    ],
)
def test_conservative_dependency_intersection_is_honest(first, second, status):
    assert compare(first, second).status == status


def test_unknown_dependency_proof_is_retained():
    results = classify(["packaging==24"], ["packaging>=23"])
    assert len(results) == 1 and results[0].status == "unknown"
