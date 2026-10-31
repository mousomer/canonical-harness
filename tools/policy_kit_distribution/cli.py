"""Selected release controls: bounded summaries and cooperative private writers."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import stat
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.policy_kit_distribution.contract import (
    active_configuration,
    canonical,
    digest,
)
from tools.policy_kit_distribution.state import (
    StateError,
    confined_path,
    distribution_lock,
)


class ControlError(ValueError):
    """A stable refusal code; raw private exceptions are not console diagnostics."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ControlError("INVALID_ARGUMENTS")


def _selected_fleet(runtime_root: Path):
    expected = runtime_root / "tools/workspace_governance/fleet.py"
    if not expected.is_file():
        raise ControlError("SELECTED_RUNTIME_MISSING")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(runtime_root))
    module = importlib.import_module("tools.workspace_governance.fleet")
    for name in ("fleet", "fleet_support", "fleet_execution"):
        selected = importlib.import_module("tools.workspace_governance." + name)
        if (
            Path(selected.__file__).resolve()
            != (runtime_root / "tools/workspace_governance" / (name + ".py")).resolve()
        ):
            raise ControlError("FOREIGN_RUNTIME_IMPORT")
    return module


def _context(kit: Path) -> dict[str, Any]:
    active, local, release_path, release = active_configuration(kit)
    return {
        "kit": kit,
        "active": active,
        "local": local,
        "release_path": release_path,
        "release": release,
        "runtime_root": release_path.parent / "payload",
        "lock_path": kit / "_machine/distribution.lock",
    }


def _private_path(value: str, local: dict[str, Any]) -> Path:
    path = Path(value)
    root = confined_path(Path(local["private_bundle"]))
    if not path.is_absolute():
        raise ControlError("PRIVATE_ARTIFACT_PATH_REQUIRED")
    path = confined_path(path)
    if not path.is_relative_to(root) or path == root:
        raise ControlError("PRIVATE_ARTIFACT_OUTSIDE_BUNDLE")
    return path


def _hold_ids(
    local: dict[str, Any], known: set[str], *, baseline: bool = False
) -> set[str]:
    holds = local.get(
        "blocked_baseline_execution" if baseline else "blocked_execution", []
    )
    if not isinstance(holds, list) or any(
        not isinstance(item, dict)
        or set(item) - {"id", "reason"}
        or item.get("id") not in known
        or not isinstance(item.get("reason"), str)
        or not item["reason"].strip()
        for item in holds
    ):
        raise ControlError("INVALID_EXECUTION_HOLD")
    ids = [item["id"] for item in holds]
    if len(ids) != len(set(ids)):
        raise ControlError("DUPLICATE_EXECUTION_HOLD")
    return set(ids)


def _summary(result: dict[str, Any], *, command: str) -> None:
    rows = result.get("checkouts", [])
    statuses = Counter(row.get("status", "unknown") for row in rows)
    allowed = {
        "ready",
        "unchanged",
        "external",
        "blocked",
        "installed",
        "failed",
        "undone",
        "undo_refused",
        "accepted",
        "incomplete",
        "governed_read_only",
        "governed_absent",
        "installed_only",
        "passed",
        "baseline_passed",
        "not_applicable",
    }
    if set(statuses) - allowed:
        raise ControlError("UNKNOWN_RESULT_STATUS")
    print(
        json.dumps(
            {
                "kind": "distribution-" + command,
                "checkouts": len(rows),
                "statuses": dict(statuses),
                "failures": len(result.get("failures", [])),
                "product_execution": "requested"
                if command == "verify"
                else "not_requested",
            },
            sort_keys=True,
        )
    )


def _archive_live(path: Path, local: dict[str, Any], fleet: Any) -> None:
    """Retain exact previous observations before replacing a live artifact."""
    if not path.exists():
        return
    path = _private_path(str(path), local)
    if not path.is_file():
        raise ControlError("PRIVATE_ARTIFACT_TYPE_CHANGED")
    data = path.read_bytes()
    history = confined_path(Path(local["private_bundle"]) / "history")
    history.mkdir(mode=0o700, exist_ok=True)
    if not history.is_dir() or stat.S_IMODE(history.stat().st_mode) & 0o077:
        raise ControlError("PRIVATE_HISTORY_PERMISSIONS")
    saved = history / (digest(data) + "-" + path.name)
    if saved.exists():
        if confined_path(saved).read_bytes() != data:
            raise ControlError("PRIVATE_HISTORY_CONFLICT")
    else:
        fleet.atomic(saved, data)


def _selfcheck(context: dict[str, Any]) -> int:
    kit, active, local, release = (
        context[name] for name in ("kit", "active", "local", "release")
    )
    general = control = 0
    for entry in release["files"]:
        if not entry["path"].startswith("tools/workspace_governance/"):
            continue
        target = confined_path(kit / entry["path"])
        if (
            not target.is_file()
            or digest(target.read_bytes()) != entry["sha256"]
            or stat.S_IMODE(target.stat().st_mode) != entry["mode"]
        ):
            raise ControlError("EDITABLE_RUNTIME_DRIFT")
        general += 1
    for entry in release["distribution"]["files"]:
        target = confined_path(kit / entry["path"])
        if (
            not target.is_file()
            or digest(target.read_bytes()) != entry["sha256"]
            or stat.S_IMODE(target.stat().st_mode) != entry["mode"]
        ):
            raise ControlError("EDITABLE_DISTRIBUTION_DRIFT")
        control += 1
    archive = kit / "compatibility/1.1.3"
    manifest = json.loads(confined_path(archive / "manifest.json").read_bytes())
    if (
        digest(canonical(manifest)) != active["legacy_manifest_sha256"]
        or len(manifest["files"]) != 19
    ):
        raise ControlError("LEGACY_ARCHIVE_IDENTITY")
    for entry in manifest["files"]:
        target = confined_path(archive / entry["path"])
        if (
            not target.is_file()
            or digest(target.read_bytes()) != entry["sha256"]
            or stat.S_IMODE(target.stat().st_mode) != entry["mode"]
        ):
            raise ControlError("LEGACY_ARCHIVE_DRIFT")
    links = local["skill_links"]
    if not isinstance(links, list) or len(links) != 4:
        raise ControlError("SKILL_DISCOVERY_LINK_COUNT")
    observed = set()
    for link in links:
        path = Path(link["path"])
        manager = path.parents[1].name
        name = path.name
        identity = (manager, name)
        if (
            manager not in {".codex", ".claude"}
            or name not in {"governance-context", "governance-maintenance"}
            or identity in observed
        ):
            raise ControlError("SKILL_DISCOVERY_LINK_IDENTITY")
        expected = context["runtime_root"] / "tools/workspace_governance/skills" / name
        if (
            link.get("target") != str(expected)
            or not path.is_symlink()
            or os.readlink(path) != str(expected)
            or not (expected / "SKILL.md").is_file()
        ):
            raise ControlError("SKILL_DISCOVERY_RELEASE_DRIFT")
        observed.add(identity)
    version = json.loads(confined_path(kit / "POLICY_VERSION.json").read_bytes())
    if version["policy_version"] != active["distribution_version"]:
        raise ControlError("DISTRIBUTION_VERSION_DRIFT")
    print(
        json.dumps(
            {
                "kind": "distribution-selfcheck",
                "ok": True,
                "general_source_files": general,
                "distribution_source_files": control,
                "legacy_source_files": 19,
                "skill_links": 4,
            },
            sort_keys=True,
        )
    )
    return 0


def dispatch(
    command: str, tail: list[str], *, context_provider: Callable[[], dict[str, Any]]
) -> int:
    """Refresh validated state under its persistent lock before any dispatch."""
    try:
        initial = context_provider()
        with distribution_lock(initial["lock_path"]):
            context = context_provider()
            if context["lock_path"] != initial["lock_path"]:
                raise ControlError("DISTRIBUTION_LOCK_MOVED")
            local, active, release_path = (
                context[name] for name in ("local", "active", "release_path")
            )
            # Validate all live write destinations before looking at commands.
            destinations = {
                name: _private_path(local[name], local)
                for name in ("registry", "plan", "evidence", "audit")
            }
            if len(set(destinations.values())) != len(destinations):
                raise ControlError("PRIVATE_ARTIFACT_DESTINATIONS_OVERLAP")
            if command == "selfcheck":
                if tail:
                    raise ControlError("INVALID_ARGUMENTS")
                return _selfcheck(context)
            fleet = _selected_fleet(context["runtime_root"])
            registry = fleet.load(destinations["registry"])
            known = {row["id"] for row in fleet.registry_rows(registry)}
            if digest(canonical(registry)) != local["registry_sha256"]:
                raise ControlError("REVIEWED_REGISTRY_DRIFT")
            # Both hold declarations must remain valid, even for a nonexecution command.
            held_product = _hold_ids(local, known)
            held_baseline = _hold_ids(local, known, baseline=True)
            if command == "audit":
                if tail:
                    raise ControlError("INVALID_ARGUMENTS")
                evidence = (
                    destinations["evidence"]
                    if destinations["evidence"].is_file()
                    else None
                )
                baseline = _private_path(
                    local.get(
                        "baseline_evidence",
                        str(
                            destinations["evidence"].with_name(
                                "fleet-baseline-evidence-v2.json"
                            )
                        ),
                    ),
                    local,
                )
                if baseline in set(destinations.values()):
                    raise ControlError("PRIVATE_ARTIFACT_DESTINATIONS_OVERLAP")
                result = fleet.audit(
                    destinations["registry"],
                    release_path,
                    run_checks=True,
                    evidence_path=evidence,
                    baseline_evidence_path=baseline if baseline.is_file() else None,
                )
                _archive_live(destinations["audit"], local, fleet)
                fleet.atomic(destinations["audit"], fleet.canonical(result))
                _summary(result, command="audit")
                return int(not result["ok"])
            if command != "sync":
                raise ControlError("INVALID_COMMAND")
            parts = ["plan"] if not tail or tail == ["--dry-run"] else tail
            operation, *options = parts
            if operation == "plan":
                if options:
                    raise ControlError("INVALID_ARGUMENTS")
                result = fleet.plan(destinations["registry"], release_path)
                _archive_live(destinations["plan"], local, fleet)
                fleet.atomic(destinations["plan"], fleet.canonical(result))
                _summary(result, command="plan")
                return int(
                    any(row["status"] == "blocked" for row in result["checkouts"])
                )
            if operation in {"verify", "baseline"}:
                parser = Parser(add_help=False)
                parser.add_argument("--checkout", action="append", default=[])
                selected = parser.parse_args(options).checkout
                if len(selected) != len(set(selected)) or not set(selected).issubset(
                    known
                ):
                    raise ControlError("UNKNOWN_OR_DUPLICATE_CHECKOUT")
                requested = set(selected) if selected else known
                if requested & (
                    held_baseline if operation == "baseline" else held_product
                ):
                    raise ControlError(
                        "HELD_BASELINE_VERIFICATION"
                        if operation == "baseline"
                        else "HELD_PRODUCT_VERIFICATION"
                    )
                target = (
                    _private_path(
                        local.get(
                            "baseline_evidence",
                            str(
                                destinations["evidence"].with_name(
                                    "fleet-baseline-evidence-v2.json"
                                )
                            ),
                        ),
                        local,
                    )
                    if operation == "baseline"
                    else destinations["evidence"]
                )
                if target in {
                    destinations[name] for name in ("registry", "plan", "audit")
                }:
                    raise ControlError("PRIVATE_ARTIFACT_DESTINATIONS_OVERLAP")
                _archive_live(target, local, fleet)
                result = fleet.collect_acceptance(
                    destinations["registry"],
                    release_path,
                    checkout_ids=selected or None,
                    output_path=target,
                    baseline_only=operation == "baseline",
                )
                _summary(result, command=operation)
                return int(not result["ok"])
            if operation in {"apply", "undo"}:
                parser = Parser(add_help=False)
                if operation == "apply":
                    parser.add_argument("--plan", required=True)
                parser.add_argument("--receipt", required=True)
                arguments = parser.parse_args(options)
                receipt = _private_path(arguments.receipt, local)
                if receipt in set(destinations.values()):
                    raise ControlError("RECEIPT_OVERLAPS_LIVE_STATE")
                if operation == "apply":
                    planned = _private_path(arguments.plan, local)
                    if planned == receipt:
                        raise ControlError("PLAN_OVERLAPS_RECEIPT")
                    saved = fleet.load(planned)
                    if (
                        saved.get("release_manifest_sha256")
                        != active["release_manifest_sha256"]
                        or saved.get("registry_sha256") != local["registry_sha256"]
                        or Path(saved.get("registry_path", ""))
                        != destinations["registry"]
                    ):
                        raise ControlError("SAVED_PLAN_IDENTITY_DRIFT")
                    result = fleet.apply(planned, receipt)
                    failed = any(
                        row["status"] in {"failed", "blocked"}
                        for row in result["checkouts"]
                    )
                else:
                    result = fleet.undo(receipt)
                    failed = any(
                        row["status"] == "undo_refused" for row in result["checkouts"]
                    )
                _summary(result, command=operation)
                return int(failed)
            raise ControlError("INVALID_SYNC_COMMAND")
    except ControlError as error:
        print(
            json.dumps(
                {"kind": "distribution-refused", "code": str(error)}, sort_keys=True
            ),
            file=sys.stderr,
        )
    except StateError:
        print(
            json.dumps(
                {"kind": "distribution-refused", "code": "PRIVATE_STATE_UNAVAILABLE"}
            ),
            file=sys.stderr,
        )
    except (OSError, ValueError, KeyError, TypeError, ImportError):
        print(
            json.dumps(
                {
                    "kind": "distribution-refused",
                    "code": "DISTRIBUTION_VALIDATION_FAILED",
                }
            ),
            file=sys.stderr,
        )
    return 2


def main(kit: Path, argv: list[str]) -> int:
    if not argv:
        print(
            json.dumps({"kind": "distribution-refused", "code": "INVALID_ARGUMENTS"}),
            file=sys.stderr,
        )
        return 2
    return dispatch(argv[0], argv[1:], context_provider=lambda: _context(kit))
