from __future__ import annotations

import json
from pathlib import Path

import pytest
from tools.workspace_governance.release import BASELINE_FILES, export_release


def sources(tmp_path: Path) -> tuple[Path, Path]:
    pack = tmp_path / "pack"
    kit = tmp_path / "kit"
    pack.mkdir()
    kit.mkdir()
    manifest = {
        "name": "workspace-governance",
        "version": "0.5.0",
        "revision": "fixture",
        "artifact_roles": [
            {"path": "MANIFEST.json", "role": "executable_machinery"},
            {"path": "VERSION", "role": "executable_machinery"},
        ],
    }
    (pack / "MANIFEST.json").write_text(json.dumps(manifest))
    (pack / "VERSION").write_text("0.5.0\n")
    for source, _ in BASELINE_FILES:
        p = kit / source
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"policy_version": "1.1.3"})
            if source == "POLICY_VERSION.json"
            else "baseline\n"
        )
    return pack, kit


def test_export_is_reconstructible_and_reapplication_is_unchanged(
    tmp_path: Path,
) -> None:
    pack, kit = sources(tmp_path)
    first = export_release(pack, kit, tmp_path / "release")
    repeat = export_release(pack, kit, tmp_path / "release")
    separate = export_release(pack, kit, tmp_path / "second")
    assert first["identity"] == repeat["identity"] == separate["identity"]
    assert repeat["changed"] is False
    assert (
        tmp_path / "release/payload/tools/workspace_governance/VERSION"
    ).read_bytes() == (pack / "VERSION").read_bytes()


def test_rejects_rewriting_an_accepted_release(tmp_path: Path) -> None:
    pack, kit = sources(tmp_path)
    export_release(pack, kit, tmp_path / "release")
    (pack / "VERSION").write_text("0.5.1\n")
    with pytest.raises(ValueError, match="different identity"):
        export_release(pack, kit, tmp_path / "release")


def test_rejects_drifted_retained_release_bytes(tmp_path: Path) -> None:
    pack, kit = sources(tmp_path)
    export_release(pack, kit, tmp_path / "release")
    (tmp_path / "release/payload/tools/workspace_governance/VERSION").write_text(
        "tampered"
    )
    with pytest.raises(ValueError, match="drifted"):
        export_release(pack, kit, tmp_path / "release")


def test_rejects_undeclared_export_file(tmp_path: Path) -> None:
    pack, kit = sources(tmp_path)
    (pack / "unowned.py").write_text("pass")
    with pytest.raises(ValueError, match="artifact set"):
        export_release(pack, kit, tmp_path / "release")


def test_rejects_source_symlink_outside_pack(tmp_path: Path) -> None:
    pack, kit = sources(tmp_path)
    (pack / "VERSION").unlink()
    external = tmp_path / "external"
    external.write_text("external")
    (pack / "VERSION").symlink_to(external)
    with pytest.raises(ValueError, match="escapes"):
        export_release(pack, kit, tmp_path / "release")


def test_export_preserves_legacy_archive_after_active_distribution_upgrade(tmp_path):
    import shutil

    pack, kit = sources(tmp_path)
    first = export_release(pack, kit, tmp_path / "first")
    archived = tmp_path / "legacy"
    shutil.copytree(kit, archived)
    shutil.copytree(archived, kit / "compatibility/1.1.3")
    (kit / "POLICY_VERSION.json").write_text(json.dumps({"policy_version": "1.2.0"}))
    repeat = export_release(pack, kit, tmp_path / "second")
    assert repeat["identity"] == first["identity"]
