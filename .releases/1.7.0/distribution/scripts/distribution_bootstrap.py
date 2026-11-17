"""Small stdlib dispatcher: validate immutable bytes before selected-code import."""

import hashlib
import json
import os
import pathlib
import stat
import sys
import types


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def confined(root, relative):
    name = pathlib.PurePosixPath(relative)
    if (
        not relative
        or name.is_absolute()
        or name.as_posix() != relative
        or "\\" in relative
        or any(part in {".", "..", ".git"} for part in relative.split("/"))
    ):
        raise ValueError("invalid selected relative path")
    path = root / relative
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("symlink in selected component")
    return path


def validate(root, release, expected):
    if hashlib.sha256(canonical(release)).hexdigest() != expected:
        raise ValueError("active release identity changed")
    if (
        not isinstance(release, dict)
        or type(release.get("schema_version")) is not int
        or release["schema_version"] != 2
        or release.get("preview_only")
        or release.get("release_root", ".") != "."
    ):
        raise ValueError("active release has no executable component contract")
    if not isinstance(release.get("files"), list) or not release["files"]:
        raise ValueError("invalid runtime declaration")
    provenance = release.get("provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("payload_identity")
        != hashlib.sha256(canonical(release["files"])).hexdigest()
    ):
        raise ValueError("runtime payload identity changed")
    rows = [("payload", row) for row in release["files"]]
    for component in ("verification", "distribution"):
        declaration = release[component]
        if (
            not isinstance(declaration, dict)
            or declaration.get("schema_version") != 1
            or not isinstance(declaration.get("files"), list)
            or not declaration["files"]
        ):
            raise ValueError("invalid component declaration")
        if (
            hashlib.sha256(canonical(declaration["files"])).hexdigest()
            != declaration["identity_sha256"]
        ):
            raise ValueError("selected component identity changed")
        rows.extend((component, row) for row in declaration["files"])
    expected_paths = {root / "release.json"}
    for component, row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("path"), str)
            or not isinstance(row.get("source"), str)
            or row["source"] != component + "/" + row["path"]
            or not isinstance(row.get("sha256"), str)
            or type(row.get("mode")) is not int
        ):
            raise ValueError("invalid component source contract")
        path = confined(root, row["source"])
        if path in expected_paths or not path.is_file():
            raise ValueError("missing or duplicate selected source")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("selected source bytes changed")
        if stat.S_IMODE(path.stat().st_mode) != row["mode"] or row["mode"] not in (
            0o644,
            0o755,
        ):
            raise ValueError("selected source mode changed")
        expected_paths.add(path)
    for base, directories, files in os.walk(root, followlinks=False):
        if any((pathlib.Path(base) / name).is_symlink() for name in directories):
            raise ValueError("symlink selected directory")
        if any(pathlib.Path(base) / name not in expected_paths for name in files):
            raise ValueError("undeclared selected source")


def main():
    kit = pathlib.Path(__file__).resolve().parents[1]
    config_path = kit / "_machine/distribution.local.json"
    if config_path.is_symlink() or stat.S_IMODE(config_path.stat().st_mode) & 0o077:
        raise ValueError("private configuration must be owner-only")
    local = json.loads(config_path.read_bytes())
    interpreter = local["governance_python"]
    if (
        not isinstance(interpreter, str)
        or not pathlib.Path(interpreter).is_absolute()
        or interpreter != os.path.normpath(interpreter)
        or not pathlib.Path(interpreter).is_file()
        or not os.access(interpreter, os.X_OK)
    ):
        raise ValueError("declared governance interpreter unavailable")
    # Re-exec once with isolated Python before importing any packaged implementation.
    if not sys.flags.isolated or os.path.normpath(sys.executable) != interpreter:
        clean = {
            key: value
            for key, value in os.environ.items()
            if key in {"HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "TZ"}
        }
        clean["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        os.execve(
            interpreter,
            [
                interpreter,
                "-I",
                "-B",
                str(pathlib.Path(__file__).resolve()),
                *sys.argv[1:],
            ],
            clean,
        )
    command, *arguments = sys.argv[1:]
    candidate = None
    if command == "verify-kit" and "--candidate" in arguments:
        index = arguments.index("--candidate")
        candidate = pathlib.Path(arguments[index + 1]).resolve()
    if candidate is not None:
        runtime = candidate / "tools/workspace_governance"
        control = candidate / "tools/policy_kit_distribution"
    else:
        active = json.loads((kit / ".governance/distribution.json").read_bytes())
        path = confined(kit, active["release_manifest"])
        release = json.loads(path.read_bytes())
        validate(path.parent, release, active["release_manifest_sha256"])
        runtime = path.parent / "payload/tools/workspace_governance"
        control = path.parent / "distribution/tools/policy_kit_distribution"
    namespace = types.ModuleType("tools")
    namespace.__package__ = "tools"
    namespace.__path__ = [
        str(runtime.parents[1] / "tools"),
        str(control.parents[1] / "tools"),
    ]
    sys.modules["tools"] = namespace
    sys.path[:0] = [str(runtime.parents[1]), str(control.parents[1])]
    if command == "verify-kit":
        from tools.policy_kit_distribution.verification import main as selected_main
    else:
        from tools.policy_kit_distribution.cli import main as selected_main

        arguments = [command, *arguments]
    return selected_main(kit, arguments)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        KeyError,
        ImportError,
        IndexError,
        TypeError,
        AttributeError,
    ) as error:
        print(
            json.dumps(
                {"kind": "distribution-error", "error_class": type(error).__name__}
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)
