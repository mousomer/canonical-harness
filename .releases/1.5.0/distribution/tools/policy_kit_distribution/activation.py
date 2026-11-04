"""Reviewable guarded publication writes with durable preimages and rollback."""

from __future__ import annotations

import base64
import os
import stat
import tempfile
from pathlib import Path

from .state import (
    StateError,
    atomic_private,
    canonical,
    digest,
    distribution_lock,
    fsync_directory,
)


def parent_confined(path: Path) -> Path:
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
        raise StateError("activation target must be absolute")
    for parent in path.parents:
        if parent.is_symlink():
            raise StateError("symlink activation parent")
    return path


def snapshot(path: Path) -> dict:
    parent_confined(path)
    if path.is_symlink():
        if path.lstat().st_uid != os.getuid():
            raise StateError("activation link owner differs")
        return {"kind": "symlink", "target": os.readlink(path)}
    if not path.exists():
        return {"kind": "absent"}
    if not path.is_file() or path.stat().st_uid != os.getuid():
        raise StateError("activation target type or owner differs")
    data = path.read_bytes()
    return {
        "kind": "file",
        "sha256": digest(data),
        "mode": stat.S_IMODE(path.stat().st_mode),
    }


def file_operation(path: Path, data: bytes, mode: int = 0o644) -> dict:
    if mode not in {0o600, 0o644, 0o700, 0o755}:
        raise StateError("unsupported activation mode")
    before = snapshot(path)
    if before["kind"] == "symlink":
        raise StateError("file activation refuses symlink target")
    return {
        "path": str(path),
        "before": before,
        "backup_base64": base64.b64encode(path.read_bytes()).decode()
        if before["kind"] == "file"
        else None,
        "after": {"kind": "file", "sha256": digest(data), "mode": mode},
        "content_base64": base64.b64encode(data).decode(),
    }


def deletion_operation(path: Path) -> dict:
    before = snapshot(path)
    if before["kind"] != "file":
        raise StateError("retirement requires a reviewed regular source file")
    return {
        "path": str(path),
        "before": before,
        "backup_base64": base64.b64encode(path.read_bytes()).decode(),
        "after": {"kind": "absent"},
    }


def link_operation(path: Path, target: Path) -> dict:
    before = snapshot(path)
    if (
        before["kind"] not in {"absent", "symlink"}
        or not target.is_absolute()
        or not target.is_dir()
    ):
        raise StateError(
            "link activation requires existing immutable directory and discovery alias"
        )
    return {
        "path": str(path),
        "before": before,
        "after": {"kind": "symlink", "target": str(target)},
    }


def write_state(path: Path, state: dict, content: str | None = None) -> None:
    parent_confined(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if state["kind"] == "absent":
        path.unlink()
        fsync_directory(path.parent)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".activation-", dir=path.parent)
    try:
        if state["kind"] == "symlink":
            os.close(descriptor)
            os.unlink(temporary)
            os.symlink(state["target"], temporary)
        else:
            data = base64.b64decode(content, validate=True)
            if digest(data) != state["sha256"]:
                raise StateError("activation source digest differs")
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), state["mode"])
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def rollback(journal: dict, journal_path: Path) -> None:
    conflicts = []
    for operation in reversed(journal["operations"]):
        path = Path(operation["path"])
        current = snapshot(path)
        if current == operation["before"]:
            continue
        if current != operation["after"]:
            conflicts.append(operation["path"])
            continue
        write_state(path, operation["before"], operation.get("backup_base64"))
    journal.update(
        status="rollback_conflict" if conflicts else "rolled_back", conflicts=conflicts
    )
    atomic_private(journal_path, canonical(journal))


def apply(operations: list[dict], journal_path: Path, lock_path: Path) -> dict:
    paths = [item["path"] for item in operations]
    if len(paths) != len(set(paths)) or journal_path.exists():
        raise StateError("duplicate activation paths or existing receipt")
    with distribution_lock(lock_path):
        if journal_path.exists() or journal_path.is_symlink():
            raise StateError(
                "activation receipt appeared while waiting for writer lock"
            )
        if any(snapshot(Path(item["path"])) != item["before"] for item in operations):
            raise StateError("activation preimage changed")
        changed = [item for item in operations if item["before"] != item["after"]]
        journal = {
            "schema_version": 1,
            "kind": "guarded-source-activation",
            "status": "prepared",
            "operations": changed,
            "writes": 0,
            "conflicts": [],
        }
        atomic_private(journal_path, canonical(journal))
        try:
            for item in changed:
                path = Path(item["path"])
                if snapshot(path) != item["before"]:
                    raise StateError("activation preimage changed during publication")
                write_state(path, item["after"], item.get("content_base64"))
                journal["writes"] += 1
                atomic_private(journal_path, canonical(journal))
            journal["status"] = "activated"
            atomic_private(journal_path, canonical(journal))
        except (OSError, ValueError):
            rollback(journal, journal_path)
            raise
    return {
        "kind": journal["kind"],
        "status": journal["status"],
        "writes": journal["writes"],
    }
