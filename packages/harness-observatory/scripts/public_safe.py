"""Offline tracked-content and reachable-history publication gate."""

import argparse
import os
import re
import subprocess
from pathlib import Path

UNSAFE_DIRS = {
    "private",
    "raw",
    "logs",
    "extracted",
    "generated",
    "warehouse",
    "warehouses",
    "runs",
    "sessions",
    "transcripts",
    "analysis-output",
    "credentials",
}
ARTIFACTS = {".jsonl", ".db", ".duckdb", ".sqlite", ".sqlite3", ".parquet", ".pem", ".key", ".log"}
PATTERNS = {
    "absolute_home": rb"(?:/Users/|/home/|[A-Za-z]:[\\/]Users[\\/])[A-Za-z0-9][A-Za-z0-9_.-]*",
    "provider_source_path": rb"(?:\.codex|\.claude)[/\\](?:sessions|projects|logs)(?:[/\\]|\b)",
    "credential_token": rb"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,})\b",
    "private_key": rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s",
    "credential_assignment": rb"(?i)\b(?:password|api[_-]?key|access[_-]?token|client[_-]?secret)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_+/=-]{12,}",
    "canonical_run_record": rb'"run_id"\s*:\s*"sha256:[0-9a-f]{64}"',
    "transcript_record": rb'"(?:sessionId|session_id|type)"\s*:\s*"(?:[0-9a-f]{8}-[0-9a-f-]{27,}|session_meta|response_item|tool_result)"',
}


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)


def content_issues(data, fixture=False):
    issues = [
        name
        for name, pattern in PATTERNS.items()
        if not (fixture and name == "transcript_record") and re.search(pattern, data)
    ]
    if b"\0" in data or data.startswith((b"PAR1", b"SQLite format 3")):
        issues.append("binary_artifact")
    return issues


def path_issues(path, mode):
    p = Path(path)
    issues = []
    if mode not in {"100644", "100755"}:
        issues.append("non_regular_tracked_entry")
    if any(part.lower() in UNSAFE_DIRS or part.lower().startswith(".env") for part in p.parts):
        issues.append("unsafe_directory_or_env")
    if p.suffix.lower() in ARTIFACTS:
        issues.append("unapproved_data_artifact")
    return issues


def scan(repo, include_history=True):
    failures = []
    cache = {}

    def inspect(path, mode, oid, location, data=None):
        # Location uses Git object IDs and entry ordinals, never matched values.
        issues = path_issues(path, mode)
        if data is None:
            if oid not in cache:
                cache[oid] = git(repo, "cat-file", "blob", oid) if mode.startswith("100") else b""
            data = cache[oid]
        issues += content_issues(data)
        failures.extend(f"{location} [{issue}]" for issue in sorted(set(issues)))

    tracked = git(repo, "ls-files", "--stage", "-z").split(b"\0")
    for index, entry in enumerate(filter(None, tracked)):
        meta, name = entry.split(b"\t", 1)
        mode, oid, stage = meta.decode().split()
        path = os.fsdecode(name)
        inspect(path, mode, oid, f"index entry={index} blob={oid}")
        target = repo / path
        if not target.resolve().is_relative_to(repo):
            failures.append(f"worktree entry={index} [external_symlink_ancestor]")
        elif target.is_symlink():
            failures.append(f"worktree entry={index} [symlink]")
        elif target.is_file():
            inspect(path, mode, oid, f"worktree entry={index}", target.read_bytes())
        else:
            failures.append(f"worktree entry={index} [missing_tracked_file]")
        if stage != "0":
            failures.append(f"index entry={index} [unmerged]")
    commits = []
    if include_history:
        # --all includes branches, tags, remote refs, notes and other refs; HEAD
        # additionally includes detached HEAD. Reflogs/unreachable objects excluded.
        commits = git(repo, "rev-list", "--all", "HEAD").decode().splitlines()
        for commit in commits:
            entries = git(repo, "ls-tree", "-r", "-z", commit).split(b"\0")
            for index, entry in enumerate(filter(None, entries)):
                meta, name = entry.split(b"\t", 1)
                mode, _, oid = meta.decode().split()
                inspect(os.fsdecode(name), mode, oid, f"commit={commit} entry={index} blob={oid}")
        objects = (
            git(repo, "rev-list", "--objects", "--no-object-names", "--all", "HEAD")
            .decode()
            .splitlines()
        )
        for oid in objects:
            kind = git(repo, "cat-file", "-t", oid).decode().strip()
            if kind in {"commit", "tag"}:
                data = git(repo, "cat-file", kind, oid).split(b"\n\n", 1)[-1]
                failures.extend(f"{kind}={oid} [{code}]" for code in content_issues(data))
            elif kind == "blob" and oid not in cache:
                failures.extend(
                    f"blob={oid} [{code}]"
                    for code in content_issues(git(repo, "cat-file", "blob", oid))
                )
    return sorted(set(failures)), len(commits)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--current-only",
        action="store_true",
        help="Pre-rewrite check only; not publication approval",
    )
    args = parser.parse_args()
    try:
        failures, count = scan(args.repo.resolve(), not args.current_only)
    except (OSError, ValueError, subprocess.SubprocessError):
        parser.exit(2, "Public-safety scan failed [scan_input_failure].\n")
    if failures:
        parser.exit(1, "Public-safety scan rejected content:\n" + "\n".join(failures) + "\n")
    print(f"Public-safety scan passed; reachable commits checked: {count}.")


if __name__ == "__main__":
    main()
