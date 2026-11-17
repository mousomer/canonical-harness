from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools.workspace_governance.sanitation import MAX_BYTES, scan


def checkout(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_detects_untracked_secrets_without_echoing_values(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    token = "gh" + "p_" + "a" * 36
    (root / "source.py").write_text("# benign\ncredential='" + token + "'\n")
    report = scan(root)
    assert report["findings"] == [{"pattern_id": "github-token", "line": 2}]
    assert token not in json.dumps(report)


def test_ignored_bytes_and_external_symlink_are_not_scanned(tmp_path: Path) -> None:
    root = checkout(tmp_path / "repo")
    (root / ".gitignore").write_text("ignored\n")
    token = "AK" + "IA" + "B" * 16
    (root / "ignored").write_text(token)
    external = tmp_path / "external"
    external.write_text(token)
    (root / "external-link").symlink_to(external)
    report = scan(root)
    assert report["finding_count"] == 0
    assert report["skipped"] == [{"reason": "symlink"}]


def test_binary_and_large_data_limits_are_explicit(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    (root / "asset").write_bytes(b"\0binary")
    (root / "large").write_bytes(b"x" * (MAX_BYTES + 1))
    report = scan(root)
    assert report["finding_count"] == 0
    assert {item["reason"] for item in report["skipped"]} == {"binary", "size-bound"}
    assert "Git history" in report["limits"]


def test_cli_hides_names_and_counts_all_exclusions(tmp_path: Path) -> None:
    import sys
    from tools.workspace_governance import sanitation

    root = checkout(tmp_path)
    for index in range(55):
        (root / f"private-canary-{index}").write_bytes(b"\0")
    result = subprocess.run(
        [sys.executable, sanitation.__file__, "--root", str(root)],
        capture_output=True,
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["coverage_counts"] == {"binary": 55}
    assert len(report["skipped"]) == sanitation.MAX_FINDINGS
    assert b"private-canary" not in result.stdout + result.stderr


def test_checker_rejects_success_with_invalid_evidence(tmp_path: Path) -> None:
    import sys
    from tools.workspace_governance.diagnostics import execute

    result = scan(checkout(tmp_path))
    for override, expected in [
        ({}, 0),
        ({"schema_version": 99}, 2),
        ({"execution": "failed"}, 2),
        ({"finding_count": 1}, 2),
        ({"coverage_counts": {"unknown": 1}}, 2),
    ]:
        payload = json.dumps(result | override)
        _, status = execute(
            "pack_sanitation",
            [sys.executable, "-c", "print(" + repr(payload) + ")"],
            tmp_path,
        )
        assert status == expected


def test_restricted_scanner_and_capability_controls(tmp_path):
    import shutil
    import sys
    import pytest
    from tools.workspace_governance import sanitation

    sandbox = shutil.which("sandbox-exec")
    if not sandbox:
        pytest.skip("macOS scanner capability probe unavailable")
    root = checkout(tmp_path / "repo")
    protected = root / "input"
    protected.write_text("clean")
    profile = (
        "(version 1)(allow default)(deny network*)"
        "(deny file-write* (subpath " + json.dumps(str(root.resolve())) + "))"
        '(deny process-exec (literal "/usr/bin/curl"))'
    )
    prefix = [sandbox, "-p", profile]
    probe = subprocess.run([*prefix, "/usr/bin/true"], capture_output=True)
    if probe.returncode and b"Operation not permitted" in probe.stderr:
        pytest.skip("host prevents activation of nested macOS sandbox")
    assert probe.returncode == 0
    scanner = subprocess.run(
        [*prefix, sys.executable, "-B", sanitation.__file__, "--root", str(root)],
        capture_output=True,
    )
    assert scanner.returncode == 0
    assert json.loads(scanner.stdout)["inspected_files"] == 1
    attempts = [
        "open(" + repr(str(protected)) + ", 'a').close()",
        "import socket; socket.socket().bind(('127.0.0.1', 0))",
        "import os; os.execv('/usr/bin/curl', ['curl', '--version'])",
    ]
    for operation in attempts:
        result = subprocess.run(
            [*prefix, sys.executable, "-B", "-c", operation], capture_output=True
        )
        assert (
            result.returncode != 0
            and b"Permission" in result.stderr
            or (result.returncode != 0 and b"Operation not permitted" in result.stderr)
        )


def test_missing_result_guard_breaks_behavioural_assertion(tmp_path, monkeypatch):
    import pytest
    from tools.workspace_governance import diagnostics

    test_checker_rejects_success_with_invalid_evidence(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(diagnostics, "_accepted_text_scan", lambda stdout: True)
        with pytest.raises(AssertionError, match="assert 0 == 2"):
            test_checker_rejects_success_with_invalid_evidence(tmp_path)
