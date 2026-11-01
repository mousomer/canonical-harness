"""Historical Git/governance and limited read-only Actions correlation."""

import copy
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

from .model import VERSION, digest, dumps, fact, provenance, timestamp, validate_bundle
from .redaction import sanitize

GOVERNANCE_DISCOVERY_VERSION = "2"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)


def resolve_commit(repo, sha):
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        raise ValueError("Use an evidenced commit SHA, not a moving ref")
    return git(repo, "rev-parse", "--verify", sha + "^{commit}").decode().strip()


def governance_snapshot(repo, sha, documents=(), scope_path="."):
    sha = resolve_commit(repo, sha)
    tracked = git(repo, "ls-tree", "-r", "--name-only", "-z", sha).decode().split("\0")
    scope = PurePosixPath(scope_path)
    if scope.is_absolute() or ".." in scope.parts:
        raise ValueError("Scope must be repository-relative")
    scope = PurePosixPath(".") if str(scope) in {"", "."} else scope
    governance_paths = {p for p in tracked if PurePosixPath(p).name in {"AGENTS.md", "CLAUDE.md"}}
    paths = set(documents) | governance_paths
    ancestors = {PurePosixPath(".")}
    current = scope
    while str(current) not in {"", "."}:
        ancestors.add(current)
        current = current.parent
    docs, missing = [], []
    for path in sorted(paths):
        if not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise ValueError("Governance paths must be repository-relative")
        if path not in tracked:
            missing.append(path)
        else:
            candidate = PurePosixPath(path)
            applicable = path in documents or candidate.parent in ancestors
            docs.append(
                {
                    "path": path,
                    "sha256": digest(git(repo, "show", f"{sha}:{path}")),
                    "kind": (
                        "agents"
                        if candidate.name == "AGENTS.md"
                        else "claude"
                        if candidate.name == "CLAUDE.md"
                        else "declared"
                    ),
                    "applicable": applicable,
                }
            )
    # Identity describes document content, independent of unrelated repo commits.
    applicable = [doc for doc in docs if doc["applicable"]]
    state = (
        "reconstructed" if applicable else "not_applicable" if governance_paths else "none_existed"
    )
    identity = dict(
        schema_version=VERSION,
        documents=docs,
        missing_optional=missing,
        coverage="historical_scoped_candidates",
        governance_discovery_version=GOVERNANCE_DISCOVERY_VERSION,
        state=state,
        scope_path=str(scope),
    )
    snapshot = dict(
        **identity,
        governance_snapshot_id=digest(dumps(identity).encode()),
        repo_sha=sha,
        provenance=provenance("git", "historical_document_hash", [f"git:{sha}"]),
    )
    return snapshot


def enrich_git(
    bundle,
    repo,
    start_sha=None,
    end_sha=None,
    evidence=None,
    documents=(),
    *,
    scope_path=None,
    sanitize_output=True,
):
    bundle = copy.deepcopy(bundle)
    run = bundle["run"]
    if run.get("schema_version") != VERSION:
        raise ValueError("schema_migration_required")
    if (start_sha or end_sha) and not evidence:
        raise ValueError("Operator-supplied endpoints require an evidence reference")
    supplied_start = start_sha is not None
    if start_sha is None:
        start_sha = run["facts"]["start_sha"]["value"]
    if not start_sha:
        run["governance_state"] = "reconstruction_failed"
        bundle["warnings"].append(
            {"code": "missing_start_sha", "source_id": run["source_id"], "line": 0}
        )
        return bundle
    start = resolve_commit(repo, start_sha)
    if supplied_start:
        run["facts"]["start_sha"] = fact(start, "operator", "operator_assertion", [evidence], 0.8)
    else:
        original = run["facts"]["start_sha"]
        run["facts"]["start_sha"] = fact(
            start,
            "git",
            "resolve_recorded_commit",
            original["provenance"]["evidence_refs"],
            original["provenance"]["confidence"],
        )
    if run["facts"]["repository"]["value"] is None:
        run["facts"]["repository"] = fact(
            str(Path(repo).resolve()), "operator", "repository_selection", [f"git:{start}"], 0.8
        )
    if scope_path is None:
        recorded_cwd = run["facts"]["cwd"]["value"]
        if recorded_cwd:
            cwd = Path(recorded_cwd).expanduser().resolve()
            repository_path = Path(repo).resolve()
            if cwd == repository_path or cwd.is_relative_to(repository_path):
                scope_path = cwd.relative_to(repository_path).as_posix() or "."
    if scope_path is None:
        run["governance_state"] = "reconstruction_failed"
        bundle["warnings"].append(
            {"code": "governance_scope_unresolved", "source_id": run["source_id"], "line": 0}
        )
        return bundle
    snapshot = governance_snapshot(repo, start, documents, scope_path)
    bundle["governance_snapshots"] = [snapshot]
    run["governance_snapshot_id"] = snapshot["governance_snapshot_id"]
    run["governance_state"] = snapshot["state"]
    if end_sha:
        end = resolve_commit(repo, end_sha)
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", start, end],
            check=True,
            capture_output=True,
        )
        run["facts"]["end_sha"] = fact(end, "operator", "operator_assertion", [evidence], 0.8)
        changes = []
        commits = git(repo, "rev-list", "--reverse", f"{start}..{end}").decode().splitlines()
        for commit in commits:
            changes.append(
                dict(
                    run_id=run["run_id"],
                    kind="commit",
                    commit_sha=commit,
                    path=None,
                    added=None,
                    deleted=None,
                    provenance=provenance("git", "endpoint_commit_range", [f"git:{start}..{end}"]),
                )
            )
        fields = (
            git(repo, "diff", "--no-renames", "--numstat", "-z", start, end).decode().split("\0")
        )
        for field in filter(None, fields):
            added, deleted, path = field.split("\t", 2)
            changes.append(
                dict(
                    run_id=run["run_id"],
                    kind="diff",
                    commit_sha=end,
                    path=path,
                    added=int(added) if added != "-" else None,
                    deleted=int(deleted) if deleted != "-" else None,
                    provenance=provenance("git", "endpoint_diff", [f"git:{start}..{end}"]),
                )
            )
        bundle["git_changes"] = changes
    if sanitize_output:
        bundle = sanitize(bundle)
    validate_bundle(bundle)
    return bundle


def fetch_actions(repository, sha):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected owner/repository")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Expected full SHA")
    pages = json.loads(
        subprocess.check_output(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repository}/actions/runs?head_sha={sha}&per_page=100",
            ],
            stderr=subprocess.PIPE,
        )
    )
    return [run for page in pages for run in page["workflow_runs"]]


def enrich_actions(bundle, records, repository, *, sanitize_output=True):
    bundle = copy.deepcopy(bundle)
    run = bundle["run"]
    if run.get("schema_version") != VERSION:
        raise ValueError("schema_migration_required")
    shas = {run["facts"][key]["value"] for key in ["start_sha", "end_sha"]}
    shas.update(c["commit_sha"] for c in bundle["git_changes"])
    correlated = []
    for row in records:
        if row.get("head_sha") not in shas or not row.get("head_sha"):
            continue
        row_repository = (row.get("repository") or {}).get("full_name")
        if row_repository and row_repository != repository:
            raise ValueError("Actions repository mismatch")
        ci_id = str(row["id"])
        polls = [
            e["timestamp"]
            for e in bundle["events"]
            if e["event_type"] == "ci.status_query"
            and e["data"].get("ci_run_id") == ci_id
            and e["data"].get("status") == "poll_v2"
            and e["timestamp"]
        ]
        correlated.append(
            dict(
                run_id=run["run_id"],
                ci_run_id=ci_id,
                repository=repository,
                head_sha=row["head_sha"],
                workflow_id=str(row.get("workflow_id"))
                if row.get("workflow_id") is not None
                else None,
                created_at=timestamp(row.get("created_at")),
                started_at=timestamp(row.get("run_started_at")),
                updated_at=timestamp(row.get("updated_at")),
                completed_at=None,
                conclusion=row.get("conclusion"),
                status=row.get("status"),
                poll_timestamps=sorted(polls),
                poll_count=len(polls),
                provenance=provenance(
                    "github_actions",
                    "exact_sha_join",
                    [f"github:{repository}/actions/runs/{ci_id}"],
                ),
            )
        )
    bundle["ci_runs"] = sanitize(correlated) if sanitize_output else correlated
    return bundle
