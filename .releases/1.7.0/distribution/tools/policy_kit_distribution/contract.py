"""Validate selected immutable components before importing their code."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def confined(root: Path, relative: str) -> Path:
    name = PurePosixPath(relative)
    if (
        not relative
        or name.is_absolute()
        or relative != name.as_posix()
        or "\\" in relative
        or any(p in {".", "..", ".git"} for p in relative.split("/"))
    ):
        raise ValueError("invalid component path")
    target = root / relative
    for current in (target, *target.parents):
        if current == root:
            break
        if current.is_symlink():
            raise ValueError("symlink in selected component")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("selected source escapes component")
    return target


def component_rows(release: object) -> list[dict]:
    if (
        not isinstance(release, dict)
        or type(release.get("schema_version")) is not int
        or release["schema_version"] != 2
        or release.get("preview_only")
        or release.get("release_root", ".") != "."
    ):
        raise ValueError("release lacks executable component contract")
    provenance = release.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("payload_identity") != digest(
        canonical(release.get("files"))
    ):
        raise ValueError("runtime payload identity differs")
    rows = []
    for name in ("payload", "verification", "distribution"):
        if name == "payload":
            files = release.get("files")
        else:
            component = release.get(name)
            if (
                not isinstance(component, dict)
                or type(component.get("schema_version")) is not int
                or component["schema_version"] != 1
            ):
                raise ValueError("invalid release component")
            files = component.get("files")
            if digest(canonical(files)) != component.get("identity_sha256"):
                raise ValueError("component identity differs")
        if not isinstance(files, list) or not files:
            raise ValueError("invalid component files")
        destinations = set()
        for row in files:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("path"), str)
                or not isinstance(row.get("source"), str)
                or not isinstance(row.get("sha256"), str)
                or len(row["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in row["sha256"])
                or type(row.get("mode")) is not int
                or row["mode"] not in {0o644, 0o755}
                or row["source"] != name + "/" + row["path"]
                or row["path"] in destinations
            ):
                raise ValueError("invalid component file declaration")
            destinations.add(row["path"])
            rows.append(row)
    return rows


def validate_release(path: Path, expected_identity: str | None = None) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("release manifest must be regular")
    release = json.loads(path.read_bytes())
    if (
        expected_identity is not None
        and digest(canonical(release)) != expected_identity
    ):
        raise ValueError("selected release identity differs")
    expected = {path.resolve()}
    for row in component_rows(release):
        target = confined(path.parent, row["source"])
        if target in expected:
            raise ValueError("duplicate release source")
        if not target.is_file() or digest(target.read_bytes()) != row["sha256"]:
            raise ValueError("selected component bytes differ")
        if stat.S_IMODE(target.stat().st_mode) != row["mode"]:
            raise ValueError("selected component mode differs")
        expected.add(target)
    for base, directories, files in os.walk(path.parent, followlinks=False):
        for name in directories:
            if (Path(base) / name).is_symlink():
                raise ValueError("symlink directory in selected release")
        for name in files:
            if Path(base) / name not in expected:
                raise ValueError("undeclared selected release source")
    return release


def active_configuration(kit: Path) -> tuple[dict, dict, Path, dict]:
    active = json.loads((kit / ".governance/distribution.json").read_bytes())
    local_path = kit / "_machine/distribution.local.json"
    if local_path.is_symlink() or stat.S_IMODE(local_path.stat().st_mode) & 0o077:
        raise ValueError("private distribution configuration must be owner-only")
    local = json.loads(local_path.read_bytes())
    path = confined(kit, active["release_manifest"])
    release = validate_release(path, active["release_manifest_sha256"])
    registry_path = Path(local["registry"])
    registry = json.loads(registry_path.read_bytes())
    if digest(canonical(registry)) != local["registry_sha256"]:
        raise ValueError("reviewed registry changed")
    return active, local, path, release


def anchor_components(runtime: Path, distribution: Path) -> None:
    """Anchor both selected tools packages without ambient namespace fallback."""
    import sys
    import types

    roots = [runtime.parents[1], distribution.parents[1]]
    owners = {
        "tools.workspace_governance": runtime,
        "tools.policy_kit_distribution": distribution,
    }
    for name, module in list(sys.modules.items()):
        for family, owner in owners.items():
            if name == family or name.startswith(family + "."):
                source = getattr(module, "__file__", None)
                if source is None or not Path(source).resolve().is_relative_to(owner):
                    raise ValueError("preimported foreign component")
    namespace = types.ModuleType("tools")
    namespace.__package__ = "tools"
    namespace.__path__ = [str(root / "tools") for root in roots]
    sys.modules["tools"] = namespace
    sys.path[:0] = [str(root) for root in roots]
