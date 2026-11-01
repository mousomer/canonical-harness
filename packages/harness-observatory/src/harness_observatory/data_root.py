"""Explicit private corpus storage. No directory inside a Git repository is safe."""

import json
import os
import subprocess
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from .model import dumps

ENVIRONMENT_KEY = "HARNESS_OBSERVATORY_DATA_ROOT"
MARKER = ".harness-observatory-data.json"
LAYOUT = (
    "raw/codex",
    "raw/claude",
    "raw/github",
    "normalized/runs",
    "normalized/events",
    "enriched/git",
    "enriched/governance",
    "enriched/ci",
    "warehouse/parquet",
    "identity/mappings",
    "analysis/intermediate",
    "analysis/results",
    "logs",
    "tmp",
)


class OutputBoundaryError(ValueError):
    """Only bounded error codes, never source values."""


def canonical(path):
    return Path(path).expanduser().resolve()


@lru_cache(maxsize=256)
def _git_probe(existing, directory_mtime_ns):
    del directory_mtime_ns  # Part of the cache key so Git creation invalidates it.
    return subprocess.run(
        ["git", "-C", existing, "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        env={**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "LC_ALL": "C"},
    )


def outside_git(path):
    """Walk physical parents, so existing case aliases and missing leaves are safe.

    Git environment overrides cannot redirect discovery. Even invalid .git markers
    are refused, avoiding reliance on Git accepting an incomplete repository.
    """
    target = canonical(path)
    existing = None
    for parent in (target, *target.parents):
        if parent.is_dir():
            if existing is None:
                existing = parent
            if os.path.lexists(parent / ".git") or (
                (parent / "HEAD").is_file()
                and (parent / "objects").is_dir()
                and (parent / "refs").is_dir()
            ):
                raise OutputBoundaryError("data_inside_git")
    if existing is None:
        raise OutputBoundaryError("unresolvable_data_path")
    result = _git_probe(str(existing), existing.stat().st_mtime_ns)
    if result.returncode == 0:
        raise OutputBoundaryError("data_inside_git")
    # A non-repository response is expected; other discovery errors fail closed.
    if b"not a git repository" not in result.stderr.lower():
        raise OutputBoundaryError("git_discovery_failed")
    return target


def within(path, root):
    # Filesystem identity, not textual prefixes, handles case-insensitive aliases.
    for ancestor in (path, *path.parents):
        if ancestor.exists() and ancestor.samefile(root):
            return True
    return False


def write_json_new(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(dumps(value) + "\n")


class DataRoot:
    def __init__(self, path):
        self.path = outside_git(path)
        marker = self.path / MARKER
        if marker.is_symlink() or not marker.is_file():
            raise OutputBoundaryError("data_root_not_initialized")
        metadata = json.loads(marker.read_text())
        if (
            not isinstance(metadata, dict)
            or metadata.get("schema_version") != "0.1"
            or not isinstance(metadata.get("data_root_id"), str)
            or not metadata["data_root_id"].startswith("corpus-")
        ):
            raise OutputBoundaryError("invalid_data_root_metadata")
        self.id = metadata["data_root_id"]
        # Recheck layout on every command, including aliases introduced after init.
        for relative in LAYOUT:
            self.destination(relative)

    def destination(self, relative):
        outside_git(self.path)
        candidate = Path(relative).expanduser()
        target = outside_git(candidate if candidate.is_absolute() else self.path / candidate)
        if not within(target, self.path):
            raise OutputBoundaryError("output_outside_data_root")
        return target

    def write(self, relative, value):
        path = self.destination(relative)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.destination(relative)
        write_json_new(path, value)
        return path

    def preserve_raw(self, provider, raw):
        from .model import digest

        if provider not in {"codex", "claude", "github"}:
            raise OutputBoundaryError("unknown_raw_provider")
        key = digest(raw).split(":", 1)[1]
        extension = "json" if provider == "github" else "jsonl"
        path = self.destination(f"raw/{provider}/{key}.{extension}")
        if path.exists():
            if path.read_bytes() != raw:
                raise OutputBoundaryError("raw_identity_conflict")
            return path
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        return path


def init_data_root(path):
    root = outside_git(path)
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise OutputBoundaryError("unsafe_data_root_location")
    if root.exists():
        if (root / MARKER).exists():
            return DataRoot(root)
        if not root.is_dir() or any(root.iterdir()):
            raise OutputBoundaryError("data_root_not_empty")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    for relative in LAYOUT:
        destination = outside_git(root / relative)
        if not within(destination, root):
            raise OutputBoundaryError("output_outside_data_root")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json_new(
        root / MARKER, {"schema_version": "0.1", "data_root_id": "corpus-" + uuid4().hex}
    )
    return DataRoot(root)


def resolve_data_root(explicit=None):
    value = explicit if explicit is not None else os.environ.get(ENVIRONMENT_KEY)
    if not value:
        raise OutputBoundaryError("data_root_required")
    return DataRoot(value)
