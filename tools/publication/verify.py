"""Bounded source allowlist, index, worktree and newly reachable history checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MANIFEST = "config/publication/source_allowlist.json"
KINDS = {"src", "test", "schema", "package", "docs", "governance"}
FORBIDDEN_DIRS = {
    "_machine",
    "state",
    "private",
    "raw",
    "logs",
    "normalized",
    "enriched",
    "extracted",
    "generated",
    "warehouse",
    "warehouses",
    "runs",
    "sessions",
    "transcripts",
    "analysis-output",
    "credentials",
    "local_runs",
    "outputs",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}
FORBIDDEN_SUFFIXES = {
    ".jsonl",
    ".ndjson",
    ".db",
    ".duckdb",
    ".sqlite",
    ".sqlite3",
    ".parquet",
    ".pem",
    ".key",
    ".log",
    ".csv",
    ".tsv",
    ".ipynb",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".pkl",
    ".pickle",
    ".npy",
    ".npz",
    ".zip",
    ".gz",
    ".tgz",
    ".arrow",
    ".feather",
}
PATTERNS = {
    "absolute_home": rb"(?:/Users/|/home/|[A-Za-z]:[\\/]Users[\\/])[A-Za-z0-9][A-Za-z0-9_.-]*",
    "provider_source_path": rb"(?:\.codex|\.claude)[/\\](?:sessions|projects|logs)(?:[/\\]|\b)",
    "credential_token": rb"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,})\b",
    "private_key": rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s",
    "credential_assignment": rb"(?i)\b(?:password|api[_-]?key|access[_-]?token|client[_-]?secret)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_+/=-]{12,}",
    "canonical_run_record": rb'"run_id"\s*:\s*"sha256:[0-9a-f]{64}"',
    "transcript_record": rb'"(?:sessionId|session_id|type)"\s*:\s*"(?:[0-9a-f]{8}-[0-9a-f-]{27,}|session_meta|response_item|tool_result)"',
}

# These exact already-published source bytes test private-policy non-disclosure.
# The exception covers the source-literal classifier only, never secret rules.
SYNTHETIC_SOURCE_SHA256 = (
    "73be05a178dd505966057f488f8a05828e4fc2a6356dd0b7ba6dbca45cac9f2a"
)
SYNTHETIC_SOURCE_PATHS = {
    "tests/workspace_governance/test_fleet_metadata_projection.py",
    *{
        f".releases/{version}/verification/tests/workspace_governance/test_fleet_metadata_projection.py"
        for version in ("1.3.1", "1.3.2", "1.4.0", "1.5.0", "1.6.0", "1.6.1", "1.6.2")
    },
}
# Real interpreter paths existed in these public legacy launchers. They are not
# synthetic or clean; full-history mode reports them separately and fails.
LEGACY_PUBLISHED_SOURCE = {
    "scripts/audit_workspace_policy.sh": "467fc1990a28f7fade0bc5f0a32bb70511af57358cc75538daade1f8b540fe62",
    "scripts/check_policy_kit_selftest.sh": "86ee5a6eb55adda7946525e9582391b6813e3f9805d4029ef1f90ad448061e47",
    "scripts/sync_workspace_policy.sh": "5cd56918d71c32ea3e29c2029e0558e649b1303b0a5156295b595d993912eac6",
}


class Refusal(ValueError):
    """A fixed classification, never an underlying path or source value."""


@dataclass(frozen=True)
class Limits:
    blob_bytes: int = 2 * 1024 * 1024
    total_bytes: int = 64 * 1024 * 1024
    git_output_bytes: int = 8 * 1024 * 1024
    entries: int = 30000
    commits: int = 256
    objects: int = 10000
    findings: int = 50
    seconds: float = 120


def safe_path(name: str) -> bool:
    if not isinstance(name, str) or not name or "\\" in name:
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name:
        return False
    if any(part in {".", "..", ".git"} for part in name.split("/")):
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        return False
    if any(
        part.lower() in FORBIDDEN_DIRS or part.lower().startswith(".env")
        for part in path.parts
    ):
        return False
    return path.suffix.lower() not in FORBIDDEN_SUFFIXES


def parse_manifest(data: bytes, published_base: str) -> dict:
    try:
        value = json.loads(data)
        if set(value) != {"schema_version", "published_base", "artifacts"}:
            raise ValueError
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError
        if value["published_base"] != published_base or not isinstance(
            value["artifacts"], list
        ):
            raise ValueError
        artifacts = {}
        for row in value["artifacts"]:
            if set(row) != {"path", "sha256", "mode", "kind"}:
                raise ValueError
            name = row["path"]
            if not safe_path(name) or name == MANIFEST or name in artifacts:
                raise ValueError
            if row["kind"] not in KINDS or row["mode"] not in {"100644", "100755"}:
                raise ValueError
            if not isinstance(row["sha256"], str) or not re.fullmatch(
                r"[0-9a-f]{64}", row["sha256"]
            ):
                raise ValueError
            artifacts[name] = row
        return artifacts
    except (ValueError, KeyError, TypeError):
        raise Refusal("source_manifest_invalid") from None


class Scanner:
    def __init__(self, repo: Path, published_base: str, limits: Limits):
        if repo.is_symlink():
            raise Refusal("repository_symlink")
        self.repo = repo.resolve()
        self.base = published_base
        self.limits = limits
        self.deadline = time.monotonic() + limits.seconds
        self.total_bytes = 0
        self.entries = 0
        self.cache = {}
        self.failures = []
        self.failure_count = 0
        self.reviewed_sources = []
        self.legacy_findings = []
        self.reviewed_source_count = 0
        self.legacy_finding_count = 0
        self.hash_algorithm = None

    def git(
        self, *arguments: str, cap: int | None = None, accepted_codes=(0,)
    ) -> bytes:
        limit = self.limits.git_output_bytes if cap is None else cap
        environment = {
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        process = subprocess.Popen(
            [
                "git",
                "--no-replace-objects",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(self.repo),
                *arguments,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        outputs = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            with selectors.DefaultSelector() as selector:
                for name in outputs:
                    stream = getattr(process, name)
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                stop = min(self.deadline, time.monotonic() + 15)
                while selector.get_map() or process.poll() is None:
                    if time.monotonic() >= stop:
                        raise Refusal("scan_deadline_exceeded")
                    for key, _ in selector.select(0.1):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        outputs[key.data].extend(data)
                        bound = limit if key.data == "stdout" else 16384
                        if len(outputs[key.data]) > bound:
                            raise Refusal("git_output_limit_exceeded")
            if process.wait() not in accepted_codes:
                raise Refusal("git_input_unavailable")
            return bytes(outputs["stdout"])
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            process.stdout.close()
            process.stderr.close()

    def fail(self, code: str, location: str) -> None:
        self.failure_count += 1
        if len(self.failures) < self.limits.findings:
            self.failures.append({"code": code, "location": location})

    def count_entry(self) -> None:
        self.entries += 1
        if self.entries > self.limits.entries:
            raise Refusal("entry_limit_exceeded")

    def account(self, data: bytes) -> bytes:
        if len(data) > self.limits.blob_bytes:
            raise Refusal("source_size_limit_exceeded")
        self.total_bytes += len(data)
        if self.total_bytes > self.limits.total_bytes:
            raise Refusal("aggregate_size_limit_exceeded")
        return data

    def blob(self, oid: str) -> bytes:
        if oid not in self.cache:
            size = self.git("cat-file", "-s", oid)
            if not size.strip().isdigit() or int(size) > self.limits.blob_bytes:
                raise Refusal("source_size_limit_exceeded")
            self.cache[oid] = self.account(
                self.git("cat-file", "blob", oid, cap=self.limits.blob_bytes)
            )
        return self.cache[oid]

    def disk(self, name: str) -> tuple[str, bytes]:
        current = self.repo
        for part in PurePosixPath(name).parts:
            current = current / part
            if current.is_symlink():
                raise Refusal("source_symlink")
        metadata = current.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise Refusal("non_regular_source")
        with current.open("rb") as stream:
            data = self.account(stream.read(self.limits.blob_bytes + 1))
        return ("100755" if metadata.st_mode & 0o111 else "100644"), data

    def tree(self, commit: str) -> dict:
        tree = {}
        for raw in self.git("ls-tree", "-r", "-z", commit).split(b"\0"):
            if not raw:
                continue
            self.count_entry()
            metadata, name = raw.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
            tree[os.fsdecode(name)] = (mode, kind, oid)
        return tree

    def content(self, name: str, data: bytes, location: str, legacy: bool) -> None:
        digest = hashlib.sha256(data).hexdigest()
        if b"\0" in data or data.startswith((b"PAR1", b"SQLite format 3")):
            self.fail("binary_artifact", location)
        try:
            data.decode("utf-8")
        except UnicodeError:
            self.fail("non_text_source", location)
        for rule, pattern in PATTERNS.items():
            if not re.search(pattern, data):
                continue
            if (
                rule == "absolute_home"
                and name in SYNTHETIC_SOURCE_PATHS
                and digest == SYNTHETIC_SOURCE_SHA256
            ):
                self.reviewed_source_count += 1
                if len(self.reviewed_sources) < self.limits.findings:
                    self.reviewed_sources.append(
                        {"rule": rule, "location": location, "sha256": digest}
                    )
            elif (
                legacy
                and rule == "absolute_home"
                and LEGACY_PUBLISHED_SOURCE.get(name) == digest
            ):
                self.legacy_finding_count += 1
                if len(self.legacy_findings) < self.limits.findings:
                    self.legacy_findings.append(
                        {"rule": rule, "location": location, "sha256": digest}
                    )
            else:
                self.fail(rule, location)

    def inspect(
        self,
        name,
        mode,
        oid,
        data,
        location,
        artifacts,
        baseline,
        legacy=False,
        require_manifest_bytes=False,
    ):
        self.count_entry()
        if not safe_path(name) or mode not in {"100644", "100755"}:
            self.fail("non_software_path_or_mode", location)
            return
        if (
            name != MANIFEST
            and baseline.get(name) != (mode, "blob", oid)
            and name not in artifacts
        ):
            self.fail("unallowlisted_source_bytes", location)
            return
        if data is None:
            data = self.blob(oid)
        if name == MANIFEST:
            parse_manifest(data, self.base)
        elif require_manifest_bytes or baseline.get(name) != (mode, "blob", oid):
            row = artifacts.get(name)
            if (
                not row
                or row["mode"] != mode
                or row["sha256"] != hashlib.sha256(data).hexdigest()
            ):
                self.fail("unallowlisted_source_bytes", location)
        self.content(name, data, location, legacy)

    def current(self, baseline, artifacts, manifest_bytes):
        indexed_manifest = self.git(
            "ls-files", "--error-unmatch", "--", MANIFEST, accepted_codes=(0, 1)
        ).strip()
        indexed = {}
        for ordinal, raw in enumerate(
            filter(None, self.git("ls-files", "--stage", "-z").split(b"\0"))
        ):
            metadata, encoded = raw.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
            name = os.fsdecode(encoded)
            indexed[name] = (mode, oid)
            if stage != "0":
                self.fail("unmerged_index", f"index:{ordinal}")
            if name == MANIFEST and self.blob(oid) != manifest_bytes:
                self.fail("source_manifest_index_mismatch", f"index:{ordinal}")
            self.inspect(
                name,
                mode,
                oid,
                None,
                f"index:{ordinal}",
                artifacts,
                baseline,
                require_manifest_bytes=bool(indexed_manifest) and name in artifacts,
            )
        if set(baseline) - set(indexed):
            self.fail("published_source_deleted_from_index", "index")
        if indexed_manifest and set(artifacts) - set(indexed):
            self.fail("allowlisted_source_missing_from_index", "index")
        names = sorted(
            {
                os.fsdecode(name)
                for name in self.git(
                    "ls-files", "--cached", "--others", "--exclude-standard", "-z"
                ).split(b"\0")
                if name
            }
        )
        for ordinal, name in enumerate(names):
            if not safe_path(name):
                self.fail("non_software_path_or_mode", f"worktree:{ordinal}")
                continue
            if name != MANIFEST and name not in baseline and name not in artifacts:
                self.fail("unallowlisted_source_bytes", f"worktree:{ordinal}")
                continue
            mode, data = self.disk(name)
            oid = self.object_hash(data)
            self.inspect(
                name,
                mode,
                oid,
                data,
                f"worktree:{ordinal}",
                artifacts,
                baseline,
                require_manifest_bytes=name in artifacts,
            )
        if set(artifacts) - set(names):
            self.fail("allowlisted_source_missing_or_ignored", "manifest")

    def object_hash(self, data):
        if self.hash_algorithm is None:
            self.hash_algorithm = (
                self.git("rev-parse", "--show-object-format").decode().strip()
            )
        algorithm = self.hash_algorithm
        if algorithm not in {"sha1", "sha256"}:
            raise Refusal("object_format_unsupported")
        return hashlib.new(
            algorithm, b"blob " + str(len(data)).encode() + b"\0" + data
        ).hexdigest()

    def history(self, baseline, full_history):
        args = ("rev-list", "--all", "HEAD")
        commits = (
            self.git(*args, *(() if full_history else ("--not", self.base)))
            .decode()
            .splitlines()
        )
        if len(commits) > self.limits.commits:
            raise Refusal("commit_limit_exceeded")
        old = (
            set(self.git("rev-list", self.base).decode().splitlines())
            if full_history
            else set()
        )
        for commit in commits:
            tree = self.tree(commit)
            manifest = tree.get(MANIFEST)
            artifacts = (
                parse_manifest(self.blob(manifest[2]), self.base) if manifest else {}
            )
            if commit not in old and set(baseline) - set(tree):
                self.fail("published_source_deleted_from_history", "commit:" + commit)
            if set(artifacts) - set(tree):
                self.fail("allowlisted_source_missing_from_history", "commit:" + commit)
            for ordinal, (name, (mode, _kind, oid)) in enumerate(tree.items()):
                self.inspect(
                    name,
                    mode,
                    oid,
                    None,
                    f"commit:{commit}:{ordinal}",
                    artifacts,
                    tree if commit in old else baseline,
                    commit in old,
                    require_manifest_bytes=name in artifacts,
                )
        objects = (
            self.git(
                "rev-list",
                "--objects",
                "--no-object-names",
                "--all",
                "HEAD",
                *(() if full_history else ("--not", self.base)),
            )
            .decode()
            .splitlines()
        )
        if len(objects) > self.limits.objects:
            raise Refusal("object_limit_exceeded")
        for oid in objects:
            kind = self.git("cat-file", "-t", oid).strip()
            if kind in {b"commit", b"tag"}:
                raw = self.account(
                    self.git("cat-file", kind.decode(), oid, cap=self.limits.blob_bytes)
                )
                self.content("", raw, "metadata:" + oid, False)
            elif kind == b"blob" and oid not in self.cache:
                self.fail("unassociated_reachable_blob", "object:" + oid)
        return len(commits)


def scan(repo: Path, published_base: str, *, full_history=False, limits=None) -> dict:
    limits = Limits() if limits is None else limits
    scanner = Scanner(repo, published_base, limits)
    commits = 0
    refusal = None
    try:
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", published_base):
            raise Refusal("published_base_invalid")
        if (
            Path(scanner.git("rev-parse", "--show-toplevel").decode().strip()).resolve()
            != scanner.repo
        ):
            raise Refusal("repository_identity_mismatch")
        graft_name = (
            scanner.git("rev-parse", "--git-path", "info/grafts").decode().strip()
        )
        graft_path = Path(graft_name)
        if not graft_path.is_absolute():
            graft_path = scanner.repo / graft_path
        if os.path.lexists(graft_path):
            raise Refusal("history_grafts_present")
        if scanner.git("rev-parse", "--is-shallow-repository").strip() != b"false":
            raise Refusal("shallow_history_unavailable")
        if scanner.git(
            "config",
            "--local",
            "--get-regexp",
            r"^(extensions\.partialclone|remote\..*\.promisor)$",
            accepted_codes=(0, 1),
        ).strip():
            raise Refusal("partial_history_unavailable")
        scanner.git("merge-base", "--is-ancestor", published_base, "HEAD")
        baseline = scanner.tree(published_base)
        _, manifest_bytes = scanner.disk(MANIFEST)
        artifacts = parse_manifest(manifest_bytes, published_base)
        scanner.current(baseline, artifacts, manifest_bytes)
        commits = scanner.history(baseline, full_history)
    except (
        Refusal,
        OSError,
        ValueError,
        UnicodeError,
        subprocess.SubprocessError,
    ) as error:
        refusal = str(error) if isinstance(error, Refusal) else "scan_input_failure"
        scanner.fail(refusal, "scan")
    return {
        "kind": "software-only-publication",
        "scope": "full-history" if full_history else "new-since-published-base",
        "published_base": published_base,
        "ok": scanner.failure_count == 0 and scanner.legacy_finding_count == 0,
        "failure_count": scanner.failure_count,
        "failures": scanner.failures,
        "findings_truncated": scanner.failure_count > len(scanner.failures),
        "reviewed_synthetic_source_findings": scanner.reviewed_sources,
        "reviewed_synthetic_source_finding_count": scanner.reviewed_source_count,
        "legacy_published_source_findings": scanner.legacy_findings,
        "legacy_published_source_finding_count": scanner.legacy_finding_count,
        "reachable_commits_checked": commits,
        "entries_checked": scanner.entries,
        "source_bytes_checked": scanner.total_bytes,
        "input_refusal": refusal,
        "limits": vars(limits),
        "product_execution": "not_requested",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--published-base", required=True)
    parser.add_argument("--full-history", action="store_true")
    args = parser.parse_args()
    try:
        result = scan(args.repo, args.published_base, full_history=args.full_history)
    except (Refusal, OSError):
        result = {
            "kind": "software-only-publication",
            "ok": False,
            "input_refusal": "repository_input_refused",
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
