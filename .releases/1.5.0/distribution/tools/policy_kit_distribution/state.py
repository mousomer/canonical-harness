"""Guarded owner-private state migration with durable snapshots and a writer lock."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path


class StateError(ValueError):
    """Private state cannot safely be activated."""


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def confined_path(path: Path) -> Path:
    path = path.absolute()
    if str(path) != os.path.normpath(str(path)):
        raise StateError("Noncanonical private state path")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise StateError("Symlink in private state path")
    return path


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_private(path: Path, data: bytes, *, expected: str | None = None) -> None:
    path = confined_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and (not path.is_file() or path.stat().st_uid != os.getuid()):
        raise StateError("Private output has unsupported owner/type")
    if expected is not None and (
        not path.is_file() or digest(path.read_bytes()) != expected
    ):
        raise StateError("Private output changed before activation")
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if expected is not None and digest(path.read_bytes()) != expected:
            raise StateError("Private output changed during activation")
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def distribution_lock(path: Path, *, timeout: float = 60):
    """All distribution writers and migrations share the same persistent inode."""
    path = confined_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise StateError("Distribution lock is not an owned regular file")
        os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StateError(
                        "Distribution writer is active; retry after it finishes"
                    ) from None
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def inventory(root: Path) -> dict:
    root = confined_path(root)
    if not root.is_dir() or root.stat().st_uid != os.getuid():
        raise StateError("Private bundle is not an owned directory")
    files = []
    directories = []
    for base, dirs, names in os.walk(root, followlinks=False):
        for name in sorted(dirs):
            path = confined_path(Path(base) / name)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise StateError("Unsupported private directory")
            directories.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "source_mode": stat.S_IMODE(info.st_mode),
                    "mode": 0o700,
                }
            )
        for name in sorted(names):
            path = confined_path(Path(base) / name)
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
                raise StateError("Unsupported private file")
            data = path.read_bytes()
            after = path.lstat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise StateError("Private source changed while reading")
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": digest(data),
                    "bytes": len(data),
                    "source_mode": stat.S_IMODE(before.st_mode),
                    "mode": stat.S_IMODE(before.st_mode) & 0o700,
                }
            )
    return {
        "schema_version": 1,
        "kind": "private-state-snapshot",
        "files": sorted(files, key=lambda row: row["path"]),
        "directories": sorted(directories, key=lambda row: row["path"]),
    }


def validate_snapshot_paths(snapshot: dict) -> None:
    from pathlib import PurePosixPath

    if (
        not isinstance(snapshot, dict)
        or snapshot.get("schema_version") != 1
        or not isinstance(snapshot.get("files"), list)
        or not isinstance(snapshot.get("directories"), list)
    ):
        raise StateError("Invalid snapshot declaration")
    names = set()
    for row in [*snapshot["files"], *snapshot["directories"]]:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise StateError("Invalid snapshot path")
        name = row["path"]
        relative = PurePosixPath(name)
        if (
            not name
            or relative.is_absolute()
            or name != relative.as_posix()
            or "\\" in name
            or any(part in {".", ".."} for part in name.split("/"))
            or name in names
        ):
            raise StateError("Unsafe snapshot relative path")
        names.add(name)
    for row in snapshot["files"]:
        mode, source_mode = row.get("mode"), row.get("source_mode")
        if (
            type(mode) is not int
            or type(source_mode) is not int
            or not 0 <= source_mode <= 0o7777
            or mode != source_mode & 0o700
        ):
            raise StateError(
                "Private snapshot file mode must retain only original owner bits"
            )
    if any(row.get("mode") != 0o700 for row in snapshot["directories"]):
        raise StateError("Private snapshot directory mode must be owner-only")


def verify_snapshot(root: Path, snapshot: dict) -> None:
    validate_snapshot_paths(snapshot)
    root = confined_path(root)
    if (
        not root.is_dir()
        or stat.S_IMODE(root.stat().st_mode) != 0o700
        or root.stat().st_uid != os.getuid()
    ):
        raise StateError("Private snapshot root permissions changed")
    expected = set()
    expected_directories = set()
    for row in snapshot["directories"]:
        path = confined_path(root / row["path"])
        if (
            not path.is_relative_to(root)
            or not path.is_dir()
            or stat.S_IMODE(path.stat().st_mode) != 0o700
            or path.stat().st_uid != os.getuid()
        ):
            raise StateError("Private snapshot directory changed")
        expected_directories.add(path)
    for row in snapshot["files"]:
        path = confined_path(root / row["path"])
        if (
            not path.is_relative_to(root)
            or not path.is_file()
            or path.stat().st_uid != os.getuid()
            or stat.S_IMODE(path.stat().st_mode) != row["mode"]
            or digest(path.read_bytes()) != row["sha256"]
        ):
            raise StateError("Private snapshot file changed")
        expected.add(path)
    actual_files, actual_directories = set(), set()
    for base, directories, names in os.walk(root, followlinks=False):
        for name in directories:
            path = confined_path(Path(base) / name)
            if not path.is_dir() or path.stat().st_uid != os.getuid():
                raise StateError("Private snapshot directory changed")
            actual_directories.add(path)
        for name in names:
            path = confined_path(Path(base) / name)
            if not path.is_file() or path.stat().st_uid != os.getuid():
                raise StateError("Private snapshot file changed")
            actual_files.add(path)
    if actual_files != expected or actual_directories != expected_directories:
        raise StateError("Private snapshot has missing/extra files or directories")


def copy_snapshot(source: Path, destination: Path, snapshot: dict) -> None:
    validate_snapshot_paths(snapshot)
    source, destination = confined_path(source), confined_path(destination)
    if destination.exists():
        raise StateError("Private snapshot destination already exists")
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise StateError("Private snapshot roots overlap")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staged = Path(tempfile.mkdtemp(prefix=".bundle-", dir=destination.parent))
    try:
        staged.chmod(0o700)
        for row in snapshot["directories"]:
            (staged / row["path"]).mkdir(parents=True, exist_ok=True, mode=0o700)
        for row in snapshot["files"]:
            origin = confined_path(source / row["path"])
            if not origin.is_relative_to(source):
                raise StateError("Private source escapes bundle")
            data = origin.read_bytes()
            if digest(data) != row["sha256"]:
                raise StateError("Private source changed before copy")
            target = staged / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as handle:
                os.fchmod(handle.fileno(), row["mode"])
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        verify_snapshot(staged, snapshot)
        for base, _, _ in os.walk(staged, topdown=False):
            fsync_directory(Path(base))
        if destination.exists():
            raise StateError("Private destination appeared during copy")
        os.rename(staged, destination)
        fsync_directory(destination.parent)
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def migrate_state(
    source: Path,
    destination: Path,
    backup: Path,
    configuration: Path,
    lock_path: Path,
    journal: Path,
    *,
    expected_configuration: str,
    lock_timeout: float = 60,
) -> dict:
    """Copy all original bytes, then activate new pointers under the writer lock.

    Source modes are retained in the immutable journal. Destination modes remove
    group/other access, preserving owner executable bits; source files stay intact.
    """
    source, destination, backup = map(confined_path, (source, destination, backup))
    if (
        destination == backup
        or destination.is_relative_to(backup)
        or backup.is_relative_to(destination)
    ):
        raise StateError("Live and backup roots must be disjoint")
    with distribution_lock(lock_path, timeout=lock_timeout):
        if journal.exists() or journal.is_symlink():
            raise StateError("Migration journal already exists")
        original = confined_path(configuration).read_bytes()
        if digest(original) != expected_configuration:
            raise StateError("Private configuration differs from reviewed preimage")
        settings = json.loads(original)
        if confined_path(Path(settings["private_bundle"])) != source:
            raise StateError("Configuration does not own source bundle")
        snapshot = inventory(source)
        copy_snapshot(source, destination, snapshot)
        copy_snapshot(source, backup, snapshot)
        if inventory(source) != snapshot:
            raise StateError("Source changed while migration was quiescent")
        verify_snapshot(destination, snapshot)
        verify_snapshot(backup, snapshot)
        updated = dict(settings)
        for key in ("registry", "plan", "evidence", "audit", "private_bundle"):
            value = confined_path(Path(settings[key]))
            if not value.is_relative_to(source):
                raise StateError("State pointer is outside reviewed bundle")
            updated[key] = str(destination / value.relative_to(source))
        updated["historical_path_mappings"] = [
            *settings.get("historical_path_mappings", []),
            {"source": str(source), "destination": str(destination)},
        ]
        record = {
            "schema_version": 1,
            "kind": "private-state-migration",
            "status": "prepared",
            "source": str(source),
            "destination": str(destination),
            "backup": str(backup),
            "snapshot": snapshot,
            "configuration_before_sha256": digest(original),
            "configuration_after_sha256": digest(canonical(updated)),
            "configuration_before": settings,
            "configuration_after": updated,
        }
        atomic_private(journal, canonical(record))
        atomic_private(
            configuration, canonical(updated), expected=expected_configuration
        )
        record["status"] = "activated"
        atomic_private(journal, canonical(record))
        return {
            "status": "activated",
            "copied_files": len(snapshot["files"]),
            "copied_bytes": sum(row["bytes"] for row in snapshot["files"]),
            "backup_verified": True,
            "configuration_sha256": digest(canonical(updated)),
            "journal_sha256": digest(canonical(record)),
        }


def restore_snapshot(backup: Path, destination: Path, journal: Path) -> dict:
    record = json.loads(confined_path(journal).read_text())
    if record.get("kind") != "private-state-migration" or record.get("status") not in {
        "prepared",
        "activated",
    }:
        raise StateError("Unknown migration journal")
    if Path(record["backup"]).absolute() != backup.absolute():
        raise StateError("Backup differs from journal")
    verify_snapshot(backup, record["snapshot"])
    copy_snapshot(backup, destination, record["snapshot"])
    return {"status": "restored", "files": len(record["snapshot"]["files"])}


def recover_activation(journal: Path, configuration: Path, lock_path: Path) -> dict:
    """Resolve a crash after durable prepare without overwriting owner changes."""
    with distribution_lock(lock_path):
        record = json.loads(confined_path(journal).read_text())
        if record.get("kind") != "private-state-migration" or record.get(
            "status"
        ) not in {"prepared", "activated"}:
            raise StateError("Unknown migration recovery record")
        current = digest(confined_path(configuration).read_bytes())
        if current == record["configuration_before_sha256"]:
            return {"status": "not-activated", "source_preserved": True}
        if current != record["configuration_after_sha256"]:
            raise StateError("Recovery refuses intervening configuration changes")
        verify_snapshot(Path(record["destination"]), record["snapshot"])
        verify_snapshot(Path(record["backup"]), record["snapshot"])
        record["status"] = "activated"
        atomic_private(journal, canonical(record))
        return {"status": "activated", "backup_verified": True}
