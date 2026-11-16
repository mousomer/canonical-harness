"""Prepare coherent project and pack bindings before guarded fleet planning."""

import json
from copy import deepcopy
from pathlib import Path

from tools.policy_kit_distribution.contract import validate_release
from tools.workspace_governance.fleet_support import canonical, confined, digest
from tools.workspace_governance.validators.core import pack_hash


def prepare_bindings(row, release_path):
    """Return a registry row without mutating the checkout or the supplied row."""
    row = deepcopy(row)
    if row["integration_mode"] != "direct":
        return row
    root = Path(row["path"])
    release = validate_release(release_path.resolve())
    identity = digest(canonical(release))
    workspace = json.loads(confined(root, ".governance/workspace.json").read_bytes())
    projects = [
        project
        for project in workspace["projects"]
        if project["id"]
        == workspace.get("defaults", {}).get("project", row["project_id"])
        and project["repository"] == "."
    ]
    if len(projects) != 1:
        raise ValueError("Expected a unique local project manifest")
    project_path = projects[0]["manifest"]
    lock_path = workspace["governance_pack"]["lock"]
    if len({project_path, lock_path, ".governance/fleet.json"}) != 3:
        raise ValueError("Overlapping consumer binding paths")
    project_source = confined(root, project_path).read_bytes()
    project = json.loads(project_source)
    fleet = json.loads(confined(root, ".governance/fleet.json").read_bytes())
    prior = fleet["skill_adapters"]
    if project.get("skill_adapters") != prior:
        raise ValueError("Project and fleet skill declarations differ")
    pack = release_path.parent / "payload/tools/workspace_governance"
    adapters = deepcopy(prior)
    entries = {entry["path"]: entry for entry in row["managed_files"]}
    if len(entries) != len(row["managed_files"]):
        raise ValueError("Duplicate managed path")
    for adapter in adapters:
        name = adapter["name"]
        target = "tools/workspace_governance/skills/" + name + "/SKILL.md"
        if target != adapter["target"]:
            raise ValueError("Adapter target differs from declared skill")
        original = adapter["sha256"]
        body = confined(root, adapter["path"]).read_text()
        marker = "Canonical source SHA-256: `" + original + "`"
        if body.count(marker) != 1:
            raise ValueError("Adapter body differs from its declared digest")
        adapter["sha256"] = digest(
            confined(release_path.parent / "payload", target).read_bytes()
        )
        adapter["release_manifest_sha256"] = identity
        _bind(
            row,
            entries,
            root,
            adapter["path"],
            body.replace(marker, marker.replace(original, adapter["sha256"])),
        )
    project["skill_adapters"] = adapters
    row["skill_adapters"] = adapters
    lock = json.loads(confined(root, lock_path).read_bytes())
    manifest = json.loads((pack / "MANIFEST.json").read_bytes())
    if (
        lock["pack_path"] != "tools/workspace_governance"
        or lock["pack_name"] != manifest["name"]
    ):
        raise ValueError("Unexpected consumer pack binding")
    content, files = pack_hash(pack)
    lock.update(
        content_sha256=content,
        files=files,
        version=manifest["version"],
        revision=manifest["revision"],
        lock_algorithm=manifest["lock_algorithm"],
    )
    # Preserve the project's serialization wherever the pin-only edits suffice.
    text = project_source.decode()
    for before, after in zip(prior, adapters, strict=True):
        for key in ("sha256", "release_manifest_sha256"):
            text = text.replace(before[key], after[key])
    if json.loads(text) != project:
        text = canonical(project).decode()
    _bind(row, entries, root, project_path, text)
    _bind(row, entries, root, lock_path, canonical(lock).decode())
    row["managed_files"] = list(entries.values())
    return row


def _bind(row, entries, root, name, text):
    path = confined(root, name)
    row["replace_sha256"][name] = digest(path.read_bytes())
    entries[name] = {"path": name, "content": text, "mode": path.stat().st_mode & 0o777}


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    registry = json.loads(args.registry.read_bytes())
    release_path = args.release.resolve()
    if registry["release_manifest_sha256"] != digest(
        canonical(validate_release(release_path))
    ):
        parser.error("Registry and release identity differ")
    registry["checkouts"] = [
        prepare_bindings(row, release_path) for row in registry["checkouts"]
    ]
    import os

    with os.fdopen(
        os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
    ) as output:
        output.write(canonical(registry))


if __name__ == "__main__":
    main()
