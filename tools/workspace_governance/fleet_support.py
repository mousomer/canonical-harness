"""Guarded local-fleet adoption; registry and receipts are private machine data.

A pinned release authenticates bytes; it does not infer publisher trust or policy
approval. Existing verification and instructions retain their original bodies.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = 1
BEGIN = "<!-- BEGIN workspace-governance managed dispatch -->"
END = "<!-- END workspace-governance managed dispatch -->"
VERIFY_BEGIN = "# BEGIN workspace-governance managed verification"
VERIFY_END = "# END workspace-governance managed verification"
PROTECTED = {
    "application_private_git_store",
    "repository_metadata_store",
    "dependency_or_tool_cache",
    "dependency_or_extension_checkout",
    "registered_submodule",
    "experiment_generated_checkout",
    "test_fixture_repository",
    "backup_checkout",
    "package_manager_tool_source",
}
MODES = {"direct", "inherited_checkout", "external", "blocked"}
MAKE_ENVIRONMENT_VARIABLES = ("MAKEFLAGS", "MFLAGS", "MAKEFILES", "GNUMAKEFLAGS")


class FleetError(ValueError):
    """An invalid declaration or unsafe write."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA:
        raise FleetError(f"{path.name}: expected schema_version {SCHEMA}")
    return value


def relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(p in {"..", ".git"} for p in path.parts):
        raise FleetError(f"Unsafe relative path: {value!r}")
    if "\\" in value or value != path.as_posix() or value == ".":
        raise FleetError(f"Noncanonical relative path: {value!r}")
    return value


def confined(root: Path, name: str) -> Path:
    path = root / relative(name)
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise FleetError(f"Symlink in managed path: {name}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise FleetError(f"Path escapes checkout: {name}")
    return path


def state(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise FleetError(f"Managed file is a symlink: {path.name}")
    if not path.exists():
        return {"sha256": None, "mode": None}
    if not path.is_file():
        raise FleetError(f"Managed path is not a regular file: {path.name}")
    return {
        "sha256": digest(path.read_bytes()),
        "mode": stat.S_IMODE(path.stat().st_mode),
    }


@contextmanager
def parent_descriptor(path: Path, *, create: bool = True) -> Iterator[int]:
    """Open every directory without following symlinks, including during writes."""
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.absolute().parent.parts[1:]:
            try:
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o755, dir_fd=descriptor)
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor
    finally:
        os.close(descriptor)


def atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    path = path.absolute()
    temporary = ".fleet-" + secrets.token_hex(12)
    with parent_descriptor(path) as directory:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), mode)
                os.fsync(stream.fileno())
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def remove(path: Path) -> None:
    with parent_descriptor(path.absolute()) as directory:
        os.unlink(path.name, dir_fd=directory)


def git_query(root: Path, *args: str, optional: bool = False) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False, timeout=15
    )
    if result.returncode:
        if optional:
            return None
        raise FleetError("Unresolved Git checkout")
    return result.stdout


def untracked_entry(root: Path, name: str) -> dict[str, Any]:
    """Hash visible bytes with bounded memory; never follow an untracked symlink."""
    path = root / name.rstrip("/")
    with parent_descriptor(path.absolute(), create=False) as directory:
        info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        record = {
            "path": name,
            "type": stat.S_IFMT(info.st_mode),
            "mode": stat.S_IMODE(info.st_mode),
        }
        if stat.S_ISLNK(info.st_mode):
            record["sha256"] = digest(
                os.fsencode(os.readlink(path.name, dir_fd=directory))
            )
        elif stat.S_ISREG(info.st_mode):
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory
            )
            with os.fdopen(descriptor, "rb") as stream:
                content = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    content.update(chunk)
            record["sha256"] = content.hexdigest()
        else:
            record["device"] = info.st_rdev
    return record


def git_head(root: Path) -> tuple[str | None, str]:
    head = git_query(root, "rev-parse", "--verify", "HEAD", optional=True)
    if head is not None:
        return head.decode().strip(), "commit"
    symbolic = git_query(root, "symbolic-ref", "--quiet", "HEAD", optional=True)
    if symbolic is None:
        raise FleetError("Unresolved Git HEAD")
    branch = symbolic.decode().strip()
    if (
        git_query(root, "show-ref", "--verify", "--quiet", branch, optional=True)
        is not None
    ):
        raise FleetError("Unresolved existing Git HEAD")
    return None, "unborn"


def git_identity(root: Path) -> dict[str, Any]:
    if root.is_symlink() or not root.is_dir():
        raise FleetError("Checkout root is missing or symlinked")
    top = os.fsdecode(git_query(root, "rev-parse", "--show-toplevel")).strip()
    if Path(top).resolve() != root.resolve():
        raise FleetError("Declared location is not a Git checkout root")
    common = Path(os.fsdecode(git_query(root, "rev-parse", "--git-common-dir")).strip())
    if not common.is_absolute():
        common = root / common
    head, head_state = git_head(root)
    status = git_query(root, "status", "--porcelain", "-z", "--untracked-files=all")
    delta = (
        git_query(root, "diff", "--binary", "HEAD")
        if head
        else (
            git_query(root, "diff", "--binary", "--cached")
            + git_query(root, "diff", "--binary")
        )
    )
    names = git_query(root, "ls-files", "--others", "--exclude-standard", "-z")
    visible = [
        untracked_entry(root, os.fsdecode(name)) for name in names.split(b"\0") if name
    ]
    snapshot = hashlib.sha256()
    for data in (status, delta, canonical(visible)):
        snapshot.update(len(data).to_bytes(8, "big"))
        snapshot.update(data)
    return {
        "common_dir_sha256": digest(os.fsencode(common.resolve())),
        "head": head,
        "head_state": head_state,
        "state_sha256": snapshot.hexdigest(),
        "dirty_entries": len([entry for entry in status.split(b"\0") if entry]),
        "untracked_entries": len(visible),
    }


def release_files(
    path: Path, expected: str | None = None
) -> tuple[dict[str, Any], str, dict[str, bytes]]:
    release = load(path)
    identity = digest(canonical(release))
    if expected is not None and identity != expected:
        raise FleetError("Release manifest changed since planning")
    for key in ("name", "version", "revision"):
        if not isinstance(release.get(key), str) or not release[key]:
            raise FleetError(f"Release missing {key}")
    source_root = (path.parent / release.get("release_root", ".")).resolve()
    payload: dict[str, bytes] = {}
    for entry in release.get("files", []):
        name = relative(entry["path"])
        if name in payload:
            raise FleetError(f"Duplicate release destination: {name}")
        data = confined(source_root, entry["source"]).read_bytes()
        if digest(data) != entry["sha256"]:
            raise FleetError(f"Release byte mismatch: {name}")
        if entry.get("mode", 0o644) not in {0o644, 0o755}:
            raise FleetError(f"Unsupported release mode: {name}")
        payload[name] = data
    if not payload:
        raise FleetError("Release has no files")
    return release, identity, payload


def registry_rows(registry: dict[str, Any]) -> list[dict[str, Any]]:
    rows = registry.get("checkouts")
    if not isinstance(rows, list):
        raise FleetError("Registry requires explicit checkouts, not a discovery census")
    ids: set[str] = set()
    paths: set[str] = set()
    for row in rows:
        if not isinstance(row.get("id"), str) or not row["id"] or row["id"] in ids:
            raise FleetError("Missing or duplicate checkout id")
        path = str(Path(row.get("path", "")).absolute())
        if not row.get("path") or not Path(row["path"]).is_absolute() or path in paths:
            raise FleetError("Missing or duplicate checkout path")
        ids.add(row["id"])
        paths.add(path)
        if row.get("integration_mode") not in MODES:
            raise FleetError(f"{row['id']}: explicit integration_mode required")
        if not row.get("repository_id"):
            raise FleetError(f"{row['id']}: repository identity required")
    return rows


def managed_block(
    original: str, start: str, end: str, body: str, *, after_shebang: bool = False
) -> str:
    if original.count(start) != original.count(end) or original.count(start) > 1:
        raise FleetError("Malformed or duplicate managed section")
    block = f"{start}\n{body.rstrip()}\n{end}\n"
    if start in original:
        first, last = original.index(start), original.index(end) + len(end)
        if last < first:
            raise FleetError("Managed section ends before it begins")
        if original[last : last + 1] == "\n":
            last += 1
        return original[:first] + block + original[last:]
    if after_shebang and original.startswith("#!"):
        first, separator, rest = original.partition("\n")
        return first + separator + block + rest
    return original.rstrip("\n") + ("\n\n" if original else "") + block


def shell_runner(text: str, name: str, governance_runner: str = "gov") -> str:
    if not text.startswith("#!") or not re.search(
        r"(?:sh|bash|zsh)(?:\s|$)", text.splitlines()[0]
    ):
        raise FleetError("Shell runner binding requires a shell shebang")
    upward = "/".join([".."] * len(PurePosixPath(name).parent.parts)) or "."
    hook = (
        'GOVERNANCE_REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/'
        + upward
        + '" && pwd)"\n'
        '"$GOVERNANCE_REPO_ROOT/'
        + relative(governance_runner)
        + '"'
        + (" check" if governance_runner == "gov" else "")
        + ' || exit "$?"'
    )
    return managed_block(text, VERIFY_BEGIN, VERIFY_END, hook, after_shebang=True)


def make_runner(text: str, runner: dict[str, Any]) -> str:
    targets = runner.get("targets", [])
    if not targets or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", target) for target in targets
    ):
        raise FleetError("Make binding requires explicit safe targets")
    for target in targets:
        if not re.search(r"^" + re.escape(target) + r"\s*:", text, re.MULTILINE):
            raise FleetError(f"Declared Make target is absent: {target}")
    if "workspace-governance-check:" in text and VERIFY_BEGIN not in text:
        raise FleetError("Existing workspace-governance-check target is unmanaged")
    hook = (
        ".PHONY: workspace-governance-check\nworkspace-governance-check:\n\t@./"
        + relative(runner.get("governance_runner", "gov"))
        + (" check" if runner.get("governance_runner", "gov") == "gov" else "")
        + "\n\n"
        + " ".join(targets)
        + ": workspace-governance-check"
    )
    return managed_block(text, VERIFY_BEGIN, VERIFY_END, hook)


def command_without_env(argv: list[str]) -> list[str]:
    if Path(argv[0]).name != "env":
        return argv
    index = 1
    while index < len(argv) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[index]):
        if argv[index].split("=", 1)[0] in MAKE_ENVIRONMENT_VARIABLES:
            raise FleetError("Make environment must not replace or bypass the verifier")
        index += 1
    if index < len(argv) and argv[index] == "--":
        index += 1
    if index >= len(argv) or argv[index].startswith("-"):
        raise FleetError("Verification env argv requires an explicit command")
    return argv[index:]


def runner_path_matches(value: str, name: str, root: Path) -> bool:
    path = Path(value)
    if path.is_absolute():
        return path == root / name
    return path.as_posix().removeprefix("./") == name


def shell_command_matches(command: list[str], name: str, root: Path) -> bool:
    if runner_path_matches(command[0], name, root) and (
        Path(command[0]).is_absolute() or "/" in command[0]
    ):
        return True
    if Path(command[0]).name not in {"sh", "bash", "zsh"}:
        return False
    index = 1
    while index < len(command) and command[index] in {"-e", "-u", "-eu", "-ue", "--"}:
        index += 1
    return index < len(command) and runner_path_matches(command[index], name, root)


def make_command_targets(
    command: list[str], runner: dict[str, Any], root: Path
) -> list[str]:
    targets = []
    filenames = {path.name for path in root.iterdir()}
    makefile = next(
        (
            name
            for name in ("GNUmakefile", "makefile", "Makefile")
            if name in filenames and (root / name).is_file()
        ),
        "Makefile",
    )
    index = 1
    while index < len(command):
        argument = command[index]
        if argument in {"-f", "--file"} and index + 1 < len(command):
            index += 1
            makefile = command[index]
        elif (
            argument in {"-j", "--jobs"}
            and index + 1 < len(command)
            and command[index + 1].isdigit()
        ):
            index += 1
        elif argument in {"-s", "--silent", "--no-print-directory"} or re.fullmatch(
            r"(?:-j|--jobs=)[0-9]+", argument
        ):
            pass
        elif argument.startswith("-") or "=" in argument:
            raise FleetError(
                "Verification make flags may not bypass or replace the runner"
            )
        else:
            targets.append(argument)
        index += 1
    if not runner_path_matches(makefile, runner["path"], root):
        raise FleetError("Verification makefile differs from the hooked runner")
    return targets


def validate_verification_argv(verification: dict[str, Any], root: Path) -> None:
    argv = verification.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(arg, str) or not arg for arg in argv)
    ):
        raise FleetError("An explicit verification argv is required")
    runner = verification.get("runner")
    if not runner:
        raise FleetError("A real verification runner binding is required")
    command = command_without_env(argv)
    if runner["kind"] == "shell":
        matched = shell_command_matches(command, relative(runner["path"]), root)
    elif runner["kind"] == "make" and Path(command[0]).name in {"make", "gmake"}:
        targets = make_command_targets(command, runner, root)
        matched = bool(targets) and set(targets).issubset(runner.get("targets", []))
    else:
        matched = False
    if not matched:
        raise FleetError("Verification argv does not invoke the hooked runner")


def verification_text(verification: dict[str, Any], root: Path) -> tuple[str, str, int]:
    runner = verification["runner"]
    name = relative(runner["path"])
    path = confined(root, name)
    seed = runner.get("seed_content")
    if seed is not None and (
        runner["kind"] != "shell" or verification.get("coverage") != "governance-only"
    ):
        raise FleetError("New runners require shell seed and governance-only coverage")
    if path.is_file():
        return name, path.read_text(), state(path)["mode"] or 0o644
    if seed is None:
        raise FleetError(f"Verification runner is absent: {name}")
    if not isinstance(seed, str) or runner.get("seed_mode", 0o755) not in {
        0o644,
        0o755,
    }:
        raise FleetError("Invalid shell runner seed")
    return name, seed, runner.get("seed_mode", 0o755)


def verification_binding(row: dict[str, Any], root: Path) -> tuple[str, bytes, int]:
    verification = row.get("verification", {})
    validate_verification_argv(verification, root)
    name, text, mode = verification_text(verification, root)
    runner = verification["runner"]
    if runner["kind"] == "shell":
        text = shell_runner(text, name, runner.get("governance_runner", "gov"))
    elif runner["kind"] == "make":
        text = make_runner(text, runner)
    else:
        raise FleetError("Unsupported verification runner kind")
    return name, text.encode(), mode


def rendered(
    row: dict[str, Any], root: Path, release: dict[str, Any], identity: str
) -> dict[str, tuple[bytes, int]]:
    body = (
        "Use `./gov check` before work and `./gov resolve` for governed context.\n"
        "The adopted pack is pinned in `.governance/fleet.json`. Existing local\n"
        "instructions and stronger verification duties continue to apply.\n"
        "Run the declared project verifier; its managed hook checks the pack first."
    )
    generated: dict[str, tuple[bytes, int]] = {}
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = confined(root, name)
        text = path.read_text() if path.exists() else ""
        generated[name] = (
            managed_block(text, BEGIN, END, body).encode(),
            state(path)["mode"] or 0o644,
        )
    metadata = {
        "schema_version": SCHEMA,
        "release": {k: release[k] for k in ("name", "version", "revision")},
        "release_manifest_sha256": identity,
        "profile": row.get("profile", "tools"),
        "integration_mode": row["integration_mode"],
        "verification": row.get("verification", {}),
        "authorities": row.get("authorities", []),
    }
    generated[".governance/fleet.json"] = (canonical(metadata), 0o644)
    for entry in row.get("managed_files", []):
        name = relative(entry["path"])
        if name in generated:
            raise FleetError(f"Duplicate generated destination: {name}")
        generated[name] = (entry["content"].encode(), entry.get("mode", 0o644))
    name, data, mode = verification_binding(row, root)
    if name in generated:
        raise FleetError(f"Binding and verification both own {name}")
    generated[name] = (data, mode)
    return generated
