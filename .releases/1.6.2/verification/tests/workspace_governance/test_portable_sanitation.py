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
    assert report["findings"] == [
        {"pattern_id": "github-token", "path": "source.py", "line": 2}
    ]
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
    assert report["skipped"] == [{"path": "external-link", "reason": "symlink"}]


def test_binary_and_large_data_limits_are_explicit(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    (root / "asset").write_bytes(b"\0binary")
    (root / "large").write_bytes(b"x" * (MAX_BYTES + 1))
    report = scan(root)
    assert report["finding_count"] == 0
    assert {item["reason"] for item in report["skipped"]} == {"binary", "size-bound"}
    assert "Git history" in report["limits"]
