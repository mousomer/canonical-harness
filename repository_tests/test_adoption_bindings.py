import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.adoption.prepare import prepare_bindings
from tools.workspace_governance.fleet_support import canonical, digest


def test_coordinated_consumer_bindings(tmp_path, monkeypatch):
    from tools.adoption import prepare

    root = tmp_path / "consumer"
    release = tmp_path / "release.json"
    target = "tools/workspace_governance/skills/context/SKILL.md"
    adapter = {
        "name": "context",
        "target": target,
        "path": "skills/context.md",
        "sha256": "a" * 64,
        "release_manifest_sha256": "b" * 64,
    }

    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    write(tmp_path / "payload" / target, b"new skill")
    write(
        tmp_path / "payload/tools/workspace_governance/MANIFEST.json",
        canonical(
            {
                "name": "pack",
                "version": "2",
                "revision": "next",
                "lock_algorithm": "hash",
            }
        ),
    )
    write(
        root / ".governance/workspace.json",
        canonical(
            {
                "projects": [
                    {"id": "example", "repository": ".", "manifest": "project.json"}
                ],
                "governance_pack": {"lock": "lock.json"},
                "defaults": {"project": "example"},
            }
        ),
    )
    write(root / ".governance/fleet.json", canonical({"skill_adapters": [adapter]}))
    write(
        root / "project.json",
        canonical({"skill_adapters": [adapter], "owner_note": adapter["sha256"]}),
    )
    write(
        root / "lock.json",
        canonical({"pack_path": "tools/workspace_governance", "pack_name": "pack"}),
    )
    write(
        root / adapter["path"],
        ("Canonical source SHA-256: `" + adapter["sha256"] + "`").encode(),
    )
    row = {
        "path": str(root),
        "integration_mode": "direct",
        "project_id": "registry-alias",
        "managed_files": [],
        "replace_sha256": {},
    }
    monkeypatch.setattr(prepare, "validate_release", lambda path: {"version": "2"})
    monkeypatch.setattr(
        prepare, "pack_hash", lambda path: ("new-pack", ["MANIFEST.json"])
    )
    result = prepare_bindings(row, release)
    changes = {e["path"]: e["content"] for e in result["managed_files"]}
    project = json.loads(changes["project.json"])
    assert project["skill_adapters"] == result["skill_adapters"]
    assert project["skill_adapters"][0]["release_manifest_sha256"] == digest(
        canonical({"version": "2"})
    )
    assert project["owner_note"] == adapter["sha256"]
    assert json.loads(changes["lock.json"])["content_sha256"] == "new-pack"
    assert result["replace_sha256"]["project.json"] == digest(
        (root / "project.json").read_bytes()
    )
    write(root / "project.json", canonical({"skill_adapters": []}))
    with pytest.raises(ValueError, match="Project and fleet"):
        prepare_bindings(row, release)
    assert prepare_bindings({"integration_mode": "external"}, release) == {
        "integration_mode": "external"
    }
