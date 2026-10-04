"""Guarded fleet plan/apply/audit/undo. Machine registries remain private."""

from __future__ import annotations

import argparse
import base64
import json
import math
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.workspace_governance.fleet_execution import (
    COLLECTOR_ID,
    make_environment,
    run_digest,
    safe_environment,
)
from tools.workspace_governance.fleet_support import (
    BEGIN,
    END,
    PROTECTED,
    SCHEMA,
    FleetError,
    atomic,
    canonical,
    confined,
    digest,
    git_identity,
    load,
    registry_rows,
    relative,
    release_files,
    remove,
    rendered,
    state,
)

__all__ = [
    "BEGIN",
    "END",
    "FleetError",
    "apply",
    "atomic",
    "audit",
    "canonical",
    "collect_acceptance",
    "digest",
    "git_identity",
    "main",
    "plan",
    "relative",
    "release_files",
    "undo",
]


def plan(registry_path: Path, release_path: Path) -> dict[str, Any]:
    registry = load(registry_path)
    release, identity, payload = release_files(release_path)
    output: dict[str, Any] = {
        "schema_version": SCHEMA,
        "kind": "fleet-adoption-plan",
        "registry_sha256": digest(canonical(registry)),
        "registry_path": str(registry_path.absolute()),
        "release_path": str(release_path.absolute()),
        "release_manifest_sha256": identity,
        "scope": registry.get("scope", {}),
        "checkouts": [],
    }
    modes = {entry["path"]: entry.get("mode", 0o644) for entry in release["files"]}
    for row in registry_rows(registry):
        item = {
            "id": row["id"],
            "path": row["path"],
            "repository_id": row["repository_id"],
            "integration_mode": row["integration_mode"],
            "operations": [],
            "errors": [],
        }
        output["checkouts"].append(item)
        if row["integration_mode"] in {"external", "blocked"}:
            item["status"] = (
                "external" if row["integration_mode"] == "external" else "blocked"
            )
            continue
        try:
            if row.get("classification") in PROTECTED:
                raise FleetError(
                    "Protected repository classification cannot be mutated"
                )
            root = Path(row["path"]).absolute()
            item["git"] = git_identity(root)
            desired = {name: (data, modes[name]) for name, data in payload.items()}
            for name, entry in rendered(row, root, release, identity).items():
                if name in desired:
                    raise FleetError(f"Release and binding both own {name}")
                desired[name] = entry
            for name, (data, mode) in desired.items():
                current = state(confined(root, name))
                after = {"sha256": digest(data), "mode": mode}
                if current == after:
                    continue
                section_owned = name in {
                    "AGENTS.md",
                    "CLAUDE.md",
                    row["verification"]["runner"]["path"],
                }
                replace = row.get("replace_sha256", {}).get(name)
                if (
                    current["sha256"] is not None
                    and not section_owned
                    and replace != current["sha256"]
                ):
                    raise FleetError(f"Unapproved preexisting file: {name}")
                item["operations"].append(
                    {
                        "path": name,
                        "before": current,
                        "after": after,
                        "content_base64": base64.b64encode(data).decode(),
                    }
                )
            item["status"] = "ready" if item["operations"] else "unchanged"
        except (
            FleetError,
            OSError,
            KeyError,
            UnicodeError,
            subprocess.TimeoutExpired,
        ) as exc:
            item["status"] = "blocked"
            item["errors"].append(str(exc))
            item["operations"] = []
    return output


def restore(root: Path, operation: dict[str, Any]) -> None:
    path = confined(root, operation["path"])
    if state(path) != operation["after"]:
        raise FleetError(f"Rollback refuses changed file: {operation['path']}")
    before = operation["before"]
    if before["sha256"] is None:
        remove(path)
    else:
        data = base64.b64decode(operation["backup_base64"], validate=True)
        if digest(data) != before["sha256"]:
            raise FleetError("Rollback backup hash mismatch")
        atomic(path, data, before["mode"])


def pending_operations(
    item: dict[str, Any],
    approved: dict[str, tuple[bytes, int]],
    root: Path,
    row: dict[str, Any],
) -> list[tuple[dict[str, Any], bytes, str | None]]:
    pending = []
    paths = [operation["path"] for operation in item["operations"]]
    if len(paths) != len(set(paths)):
        raise FleetError("Plan contains duplicate managed paths")
    for operation in item["operations"]:
        path = confined(root, operation["path"])
        current = state(path)
        if current == operation["after"]:
            continue
        section_owned = operation["path"] in {
            "AGENTS.md",
            "CLAUDE.md",
            row["verification"]["runner"]["path"],
        }
        if (
            current["sha256"] is not None
            and not section_owned
            and row.get("replace_sha256", {}).get(operation["path"])
            != current["sha256"]
        ):
            raise FleetError(
                f"Registry does not authorize replacing: {operation['path']}"
            )
        if current != operation["before"]:
            raise FleetError(f"Preimage changed: {operation['path']}")
        data = base64.b64decode(operation["content_base64"], validate=True)
        if digest(data) != operation["after"]["sha256"] or approved.get(
            operation["path"]
        ) != (data, operation["after"]["mode"]):
            raise FleetError("Plan content differs from approved release or binding")
        backup = base64.b64encode(path.read_bytes()).decode() if path.exists() else None
        pending.append((operation, data, backup))
    return pending


def rollback_writes(record: dict[str, Any], root: Path) -> None:
    for saved in reversed(record["writes"]):
        try:
            if state(confined(root, saved["path"])) == saved["after"]:
                restore(root, saved)
                saved["status"] = "rolled_back"
        except (FleetError, OSError) as error:
            record["errors"].append(str(error))


def apply_checkout(
    item: dict[str, Any],
    row: dict[str, Any],
    record: dict[str, Any],
    receipt: dict[str, Any],
    receipt_path: Path,
    release: dict[str, Any],
    identity: str,
    payload: dict[str, bytes],
) -> None:
    root = Path(item["path"])
    try:
        if (
            row["path"] != item["path"]
            or row.get("classification") in PROTECTED
            or row["integration_mode"] not in {"direct", "inherited_checkout"}
        ):
            raise FleetError("Plan location differs from approved registry")
        if git_identity(root)["common_dir_sha256"] != item["git"]["common_dir_sha256"]:
            raise FleetError("Repository identity changed since planning")
        approved = {
            entry["path"]: (payload[entry["path"]], entry.get("mode", 0o644))
            for entry in release["files"]
        }
        approved.update(rendered(row, root, release, identity))
        pending = pending_operations(item, approved, root, row)
        for operation, data, backup in pending:
            saved = {k: operation[k] for k in ("path", "before", "after")}
            saved["backup_base64"] = backup
            saved["status"] = "pending"
            record["writes"].append(saved)
            atomic(receipt_path, canonical(receipt))
            atomic(confined(root, operation["path"]), data, operation["after"]["mode"])
            saved["status"] = "written"
            atomic(receipt_path, canonical(receipt))
        record["status"] = "installed" if pending else "unchanged"
    except (FleetError, OSError, KeyError, subprocess.TimeoutExpired) as error:
        record["status"] = "failed"
        record["errors"].append(str(error))
        rollback_writes(record, root)


def apply(plan_path: Path, receipt_path: Path) -> dict[str, Any]:
    declaration = load(plan_path)
    if declaration.get("kind") != "fleet-adoption-plan":
        raise FleetError("Not an adoption plan")
    release, identity, payload = release_files(
        Path(declaration["release_path"]), declaration["release_manifest_sha256"]
    )
    registry = load(Path(declaration["registry_path"]))
    if digest(canonical(registry)) != declaration["registry_sha256"]:
        raise FleetError("Registry changed since planning")
    rows = {row["id"]: row for row in registry_rows(registry)}
    planned_ids = [row["id"] for row in declaration["checkouts"]]
    if len(planned_ids) != len(set(planned_ids)) or set(planned_ids) != set(rows):
        raise FleetError(
            "Plan does not account for every registry checkout exactly once"
        )
    if receipt_path.exists():
        raise FleetError("Receipt already exists; preserve previous undo evidence")
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA,
        "kind": "fleet-adoption-receipt",
        "plan_sha256": digest(canonical(declaration)),
        "checkouts": [],
    }
    atomic(receipt_path, canonical(receipt))
    for item in declaration["checkouts"]:
        record = {
            "id": item["id"],
            "path": item["path"],
            "status": item["status"],
            "writes": [],
            "errors": [],
        }
        receipt["checkouts"].append(record)
        if item["status"] in {"ready", "unchanged"}:
            apply_checkout(
                item,
                rows[item["id"]],
                record,
                receipt,
                receipt_path,
                release,
                identity,
                payload,
            )
        atomic(receipt_path, canonical(receipt))
    return receipt


def undo(receipt_path: Path) -> dict[str, Any]:
    receipt = load(receipt_path)
    if receipt.get("kind") != "fleet-adoption-receipt":
        raise FleetError("Not an adoption receipt")
    for record in reversed(receipt["checkouts"]):
        root = Path(record["path"])
        for operation in reversed(record["writes"]):
            if operation["status"] in {"undone", "rolled_back"}:
                continue
            try:
                current = state(confined(root, operation["path"]))
                if current == operation["before"]:
                    operation["status"] = "undone"
                else:
                    restore(root, operation)
                    operation["status"] = "undone"
            except (FleetError, OSError) as exc:
                record["errors"].append(str(exc))
                record["status"] = "undo_refused"
        if record["writes"] and all(
            op["status"] in {"undone", "rolled_back"} for op in record["writes"]
        ):
            record["status"] = "undone"
        atomic(receipt_path, canonical(receipt))
    return receipt


def installed_metadata(
    row: dict[str, Any],
    root: Path,
    release: dict[str, Any],
    identity: str,
    payload: dict[str, bytes],
) -> tuple[dict[str, Any], str]:
    git = git_identity(root)
    for entry in release["files"]:
        name = entry["path"]
        expected = {"sha256": digest(payload[name]), "mode": entry.get("mode", 0o644)}
        if state(confined(root, name)) != expected:
            raise FleetError(f"Release file absent or drifted: {name}")
    binding = confined(root, ".governance/fleet.json")
    if load(binding).get("release_manifest_sha256") != identity:
        raise FleetError("Binding release identity differs")
    for name, (data, mode) in rendered(row, root, release, identity).items():
        if state(confined(root, name)) != {"sha256": digest(data), "mode": mode}:
            raise FleetError(f"Managed binding drifted: {name}")
    return git, digest(binding.read_bytes())


def acceptance_matches(
    item: dict[str, Any], row: dict[str, Any], record: dict[str, Any], identity: str
) -> bool:
    if row["verification"].get("coverage") == "governance-only":
        return False
    expected = {
        "id": row["id"],
        "release_manifest_sha256": identity,
        "binding_sha256": record["binding_sha256"],
        "head": record["git"]["head"],
        "state_sha256": record["git"]["state_sha256"],
        "argv": row["verification"]["argv"],
        "governance_exit_code": 0,
        "exit_code": 0,
        "timed_out": False,
        "collector_id": COLLECTOR_ID,
        "make_environment": {},
        "status": "passed",
        "input_state_stable": True,
        "pre_verification": verification_snapshot(
            record["git"], record["binding_sha256"]
        ),
        "post_verification": verification_snapshot(
            record["git"], record["binding_sha256"]
        ),
    }
    return (
        bool(item.get("started_at"))
        and bool(item.get("completed_at"))
        and all(item.get(key) == value for key, value in expected.items())
    )


def verification_timeout(
    verification: dict[str, Any], key: str, default: float
) -> float:
    value = verification.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise FleetError("Verification timeout must be a finite positive number")
    return float(value)


def verification_snapshot(git: dict[str, Any], binding: str) -> dict[str, Any]:
    return {
        "head": git["head"],
        "head_state": git["head_state"],
        "state_sha256": git["state_sha256"],
        "binding_sha256": binding,
    }


def finish_verification_identity(record: dict[str, Any], root: Path) -> None:
    git = git_identity(root)
    binding = state(confined(root, ".governance/fleet.json"))["sha256"]
    current = verification_snapshot(git, binding)
    record.update(
        post_verification=current,
        input_state_stable=record["pre_verification"] == current,
        head=git["head"],
        head_state=git["head_state"],
        state_sha256=git["state_sha256"],
        binding_sha256=binding,
        completed_at=datetime.now(UTC).isoformat(),
    )


def collect_checkout(
    row: dict[str, Any],
    record: dict[str, Any],
    release: dict[str, Any],
    identity: str,
    payload: dict[str, bytes],
) -> None:
    if row["integration_mode"] == "external":
        record["status"] = "not_applicable"
        return
    if (
        row["integration_mode"] not in {"direct", "inherited_checkout"}
        or row.get("classification") in PROTECTED
    ):
        raise FleetError("Checkout is not eligible for verification execution")
    root = Path(row["path"])
    before_git, before_binding = installed_metadata(
        row, root, release, identity, payload
    )
    record["pre_verification"] = verification_snapshot(before_git, before_binding)
    verification = row["verification"]
    environment = safe_environment()
    record.update(
        collector_id=COLLECTOR_ID,
        make_environment=make_environment(environment),
        argv=verification["argv"],
        started_at=datetime.now(UTC).isoformat(),
    )
    runner = verification["runner"].get("governance_runner", "gov")
    baseline_argv = [str(confined(root, runner))] + (
        ["check"] if runner == "gov" else []
    )
    baseline = run_digest(
        baseline_argv,
        root,
        environment,
        verification_timeout(verification, "governance_timeout_seconds", 120),
    )
    record.update({f"governance_{key}": value for key, value in baseline.items()})
    if baseline["exit_code"] != 0 or baseline["timed_out"]:
        finish_verification_identity(record, root)
        raise FleetError("Required governance baseline failed or timed out")
    if verification.get("coverage") == "governance-only":
        finish_verification_identity(record, root)
        record.update(verification_coverage="governance-only", exit_code=None)
        raise FleetError(
            "Product acceptance unavailable: verification coverage is governance-only"
        )
    execution = run_digest(
        verification["argv"],
        root,
        environment,
        verification_timeout(verification, "timeout_seconds", 600),
    )
    record.update(execution)
    finish_verification_identity(record, root)
    if not record["input_state_stable"]:
        raise FleetError(
            "Verification changed Git-visible inputs or the governance binding"
        )
    installed_metadata(row, root, release, identity, payload)
    if execution["exit_code"] != 0 or execution["timed_out"]:
        raise FleetError("Declared product verification failed or timed out")
    record["status"] = "passed"


def collect_acceptance(
    registry_path: Path,
    release_path: Path,
    *,
    checkout_ids: list[str] | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Execute declared runners; only this collector emits admissible evidence."""
    registry = load(registry_path)
    release, identity, payload = release_files(release_path)
    rows = registry_rows(registry)
    selected = (
        set(checkout_ids) if checkout_ids is not None else {row["id"] for row in rows}
    )
    if not selected or not selected.issubset({row["id"] for row in rows}):
        raise FleetError("Verification selected no checkout or an unknown checkout")
    if output_path is not None and any(
        output_path.absolute().is_relative_to(Path(row["path"]).absolute())
        for row in rows
    ):
        raise FleetError(
            "Verification evidence must remain outside consumer repositories"
        )
    result = {
        "schema_version": SCHEMA,
        "kind": "fleet-verification-evidence",
        "collector_id": COLLECTOR_ID,
        "release_manifest_sha256": identity,
        "registry_sha256": digest(canonical(registry)),
        "checkouts": [],
        "failures": [],
    }
    if output_path is not None:
        atomic(output_path, canonical(result))
    for row in rows:
        if row["id"] not in selected:
            continue
        record = {
            "id": row["id"],
            "release_manifest_sha256": identity,
            "status": "failed",
            "errors": [],
        }
        result["checkouts"].append(record)
        try:
            collect_checkout(row, record, release, identity, payload)
        except (
            FleetError,
            OSError,
            KeyError,
            UnicodeError,
            subprocess.TimeoutExpired,
        ) as error:
            record["errors"].append(str(error))
            result["failures"].append(row["id"])
        if output_path is not None:
            atomic(output_path, canonical(result))
    result["ok"] = not result["failures"]
    if output_path is not None:
        atomic(output_path, canonical(result))
    return result


def observe_governance(record: dict[str, Any], root: Path) -> None:
    checked = subprocess.run(
        [str(root / "gov"), "check"],
        cwd=root,
        capture_output=True,
        check=False,
        timeout=120,
    )
    record["governance_check"] = "passed" if checked.returncode == 0 else "failed"
    record["governance_exit_code"] = checked.returncode
    record["governance_output_sha256"] = digest(checked.stdout + checked.stderr)
    record["governance_output_bytes"] = len(checked.stdout) + len(checked.stderr)
    if checked.returncode:
        raise FleetError("Consumer governance check failed")


def audit_checkout(
    row: dict[str, Any],
    record: dict[str, Any],
    release: dict[str, Any],
    identity: str,
    payload: dict[str, bytes],
    evidence: list[dict[str, Any]],
    run_checks: bool,
) -> None:
    root = Path(row["path"])
    if row["integration_mode"] == "blocked":
        raise FleetError("Location remains blocked")
    if row["integration_mode"] != "external" and row.get("classification") in PROTECTED:
        raise FleetError(
            "Protected repository classification cannot be directly adopted"
        )
    if row["integration_mode"] == "external":
        policy = row.get("external_policy", {})
        if (
            not root.exists()
            or policy.get("mode") != "read-only"
            or not policy.get("owner")
            or not policy.get("reason")
            or policy.get("release_manifest_sha256") != identity
        ):
            raise FleetError(
                "External integration lacks location or declared read-only policy"
            )
        record.update(
            external_policy=policy,
            status="governed_read_only",
            observation={
                "location_exists": True,
                "policy_release_manifest_sha256": identity,
            },
        )
        return
    git, binding = installed_metadata(row, root, release, identity, payload)
    record.update(installed=True, git=git, binding_sha256=binding)
    if run_checks:
        observe_governance(record, root)
    accepted = any(acceptance_matches(item, row, record, identity) for item in evidence)
    if accepted:
        record["product_acceptance"] = "passed"
    if row["verification"].get("coverage") == "governance-only":
        record["verification_coverage"] = "governance-only"
        raise FleetError(
            "Product acceptance unavailable: verification coverage is governance-only"
        )
    if row.get("acceptance_required", True) and not accepted:
        raise FleetError("Product acceptance evidence missing or stale")
    if not run_checks and not accepted:
        raise FleetError("Governance validation has not been observed")
    record["status"] = "accepted" if accepted else "installed_only"


def audit(
    registry_path: Path,
    release_path: Path,
    *,
    run_checks: bool = False,
    evidence_path: Path | None = None,
) -> dict[str, Any]:
    registry = load(registry_path)
    release, identity, payload = release_files(release_path)
    evidence = load(evidence_path).get("checkouts", []) if evidence_path else []
    result: dict[str, Any] = {
        "schema_version": SCHEMA,
        "kind": "fleet-adoption-audit",
        "release_manifest_sha256": identity,
        "completed_at": datetime.now(UTC).isoformat(),
        "coverage_complete": bool(
            registry.get("scope", {}).get("whole_computer_coverage")
        ),
        "checkouts": [],
        "failures": [],
    }
    for row in registry_rows(registry):
        record = {
            "id": row["id"],
            "path": row["path"],
            "integration_mode": row["integration_mode"],
            "installed": False,
            "governance_check": "not_run",
            "product_acceptance": "not_run",
            "errors": [],
        }
        result["checkouts"].append(record)
        try:
            audit_checkout(
                row, record, release, identity, payload, evidence, run_checks
            )
        except (
            FleetError,
            OSError,
            KeyError,
            UnicodeError,
            subprocess.TimeoutExpired,
        ) as error:
            record["errors"].append(str(error))
            record["status"] = "incomplete"
            result["failures"].append(row["id"])
    if not result["coverage_complete"]:
        result["failures"].append("scan_scope_incomplete")
    if registry.get("scope", {}).get("scan_errors") or registry.get("scope", {}).get(
        "unresolved"
    ):
        result["coverage_complete"] = False
        result["failures"].append("unresolved_scan_evidence")
    result["ok"] = not result["failures"]
    return result


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "audit", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--registry", required=True, type=Path)
        command.add_argument("--release", required=True, type=Path)
        command.add_argument("--output", type=Path, required=name in {"plan", "verify"})
        if name == "audit":
            command.add_argument("--run-checks", action="store_true")
            command.add_argument("--evidence", type=Path)
        if name == "verify":
            command.add_argument("--checkout", action="append", dest="checkout_ids")
    command = sub.add_parser("apply")
    command.add_argument("--plan", required=True, type=Path)
    command.add_argument("--receipt", required=True, type=Path)
    command = sub.add_parser("undo")
    command.add_argument("--receipt", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    try:
        if args.command == "plan":
            result = plan(args.registry, args.release)
            failed = any(item["status"] == "blocked" for item in result["checkouts"])
        elif args.command == "apply":
            result = apply(args.plan, args.receipt)
            failed = any(
                item["status"] in {"failed", "blocked"} for item in result["checkouts"]
            )
        elif args.command == "verify":
            result = collect_acceptance(
                args.registry,
                args.release,
                checkout_ids=args.checkout_ids,
                output_path=args.output,
            )
            failed = not result["ok"]
        elif args.command == "undo":
            result = undo(args.receipt)
            failed = any(
                item["status"] == "undo_refused" for item in result["checkouts"]
            )
        else:
            result = audit(
                args.registry,
                args.release,
                run_checks=args.run_checks,
                evidence_path=args.evidence,
            )
            failed = not result["ok"]
        if getattr(args, "output", None):
            atomic(args.output, canonical(result))
        if args.command in {"plan", "apply", "undo", "verify"}:
            # Plans/receipts contain preserved consumer bytes; keep them out of logs.
            summary = {
                "kind": result.get("kind"),
                "checkouts": [
                    {key: row[key] for key in ("id", "status", "errors") if key in row}
                    for row in result["checkouts"]
                ],
            }
            print(canonical(summary).decode(), end="")
        elif not getattr(args, "output", None):
            print(canonical(result).decode(), end="")
        return int(failed)
    except (FleetError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
