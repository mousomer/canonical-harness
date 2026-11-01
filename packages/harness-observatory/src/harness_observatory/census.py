"""Deadline-bounded, restartable full-population reconstruction."""

import subprocess
import time
from copy import deepcopy
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import ValidationError

from .data_root import DataRoot, OutputBoundaryError
from .enrichers import GOVERNANCE_DISCOVERY_VERSION, enrich_git
from .extractors import extract
from .model import EXTRACTOR_VERSION, POLL_DEFINITION_VERSION, VERSION, digest, validate_bundle
from .warehouse import build_warehouse, read_bundle


def now():
    return datetime.now(timezone.utc).isoformat()


def repository_for(cwd):
    if not cwd or not Path(cwd).is_dir():
        return None
    try:
        return Path(
            subprocess.check_output(
                ["git", "-C", cwd, "rev-parse", "--show-toplevel"], stderr=subprocess.PIPE
            )
            .decode()
            .strip()
        )
    except subprocess.SubprocessError:
        return None


def checked_existing(path, source_id):
    if not path.exists():
        return None
    bundle = read_bundle(path)
    validate_bundle(bundle)
    run = bundle["run"]
    if (
        run["source_id"] != source_id
        or run["schema_version"] != VERSION
        or run["extractor_version"] != EXTRACTOR_VERSION
    ):
        raise OutputBoundaryError("census_identity_conflict")
    return bundle


def write_checkpoint(root, census, sequence, started, status, stats, outputs, next_phase):
    root.write(
        f"logs/{census}/checkpoint-{sequence:06d}.json",
        {
            "phase": census,
            "software_HEAD": subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip(),
            "schema_version": VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "governance_discovery_version": GOVERNANCE_DISCOVERY_VERSION,
            "poll_definition_version": POLL_DEFINITION_VERSION,
            "start_time": started,
            "end_time": now(),
            "status": status,
            "outputs": outputs,
            "record_counts": dict(stats),
            "warnings": [],
            "next_phase": next_phase,
        },
    )


def run_census(root, sources, *, census="census-1", deadline_seconds=1800, checkpoint_every=25):
    if not isinstance(root, DataRoot):
        raise TypeError("Initialized DataRoot required")
    started = now()
    deadline = time.monotonic() + deadline_seconds
    population = [
        (provider, path)
        for provider, source in sorted(sources.items())
        for path in sorted(Path(source).rglob("*.jsonl"))
    ]
    stats = Counter(population=len(population))
    outputs = []
    existing_checkpoints = sorted((root.path / "logs" / census).glob("checkpoint-*.json"))
    checkpoint = max(
        (int(path.stem.removeprefix("checkpoint-")) for path in existing_checkpoints), default=0
    )
    for provider, path in population:
        if time.monotonic() >= deadline:
            checkpoint += 1
            write_checkpoint(root, census, checkpoint, started, "timeout", stats, outputs, census)
            return {"status": "timeout", "stats": dict(stats), "outputs": outputs}
        try:
            raw = path.read_bytes()
        except OSError:
            stats["source_read_failed"] += 1
            continue
        root.preserve_raw(provider, raw)
        source_id = digest(raw)
        run_id = digest((provider + source_id).encode()).split(":", 1)[1]
        normalized_rel = f"normalized/runs/{VERSION}/{EXTRACTOR_VERSION}/{run_id}.json"
        normalized_path = root.destination(normalized_rel)
        try:
            bundle = checked_existing(normalized_path, source_id)
            if bundle is None:
                bundle = extract(raw, provider, sanitize_output=False)
                root.write(normalized_rel, bundle)
                stats["normalized_written"] += 1
            else:
                stats["normalized_skipped"] += 1
        except (OSError, ValueError, ValidationError):
            stats["normalization_failed"] += 1
            failure = f"logs/{census}/normalization-failure-{run_id}.json"
            if not root.destination(failure).exists():
                root.write(
                    failure,
                    {
                        "provider": provider,
                        "source_id": source_id,
                        "category": "normalization_failed",
                    },
                )
            continue
        enriched_rel = (
            f"enriched/governance/v{GOVERNANCE_DISCOVERY_VERSION}/{EXTRACTOR_VERSION}/{run_id}.json"
        )
        enriched_path = root.destination(enriched_rel)
        enriched = checked_existing(enriched_path, source_id)
        if enriched is None:
            repo = repository_for(bundle["run"]["facts"]["cwd"]["value"])
            try:
                if repo:
                    enriched = enrich_git(bundle, repo, sanitize_output=False)
                else:
                    enriched = deepcopy(bundle)
                    enriched["run"]["governance_state"] = "reconstruction_failed"
                    enriched["warnings"].append(
                        {"code": "repository_unresolved", "source_id": source_id, "line": 0}
                    )
            except (OSError, ValueError, ValidationError, subprocess.SubprocessError):
                enriched = deepcopy(bundle)
                enriched["run"]["governance_state"] = "reconstruction_failed"
                enriched["warnings"].append(
                    {"code": "governance_reconstruction_failed", "source_id": source_id, "line": 0}
                )
                stats["governance_errors"] += 1
            root.write(enriched_rel, enriched)
            stats["enriched_written"] += 1
        else:
            stats["enriched_skipped"] += 1
        stats[f"provider_{provider}"] += 1
        stats[f"governance_{enriched['run']['governance_state']}"] += 1
        stats["warnings"] += len(enriched["warnings"])
        outputs.append(enriched_rel)
        if (stats["normalized_written"] + stats["normalized_skipped"]) % checkpoint_every == 0:
            checkpoint += 1
            write_checkpoint(
                root,
                census,
                checkpoint,
                started,
                "running",
                stats,
                outputs[-checkpoint_every:],
                census,
            )

    if time.monotonic() >= deadline:
        checkpoint += 1
        write_checkpoint(root, census, checkpoint, started, "timeout", stats, outputs, census)
        return {"status": "timeout", "stats": dict(stats), "outputs": outputs}
    warehouse_rel = f"warehouse/{census}-{VERSION}-{EXTRACTOR_VERSION}"
    warehouse = root.destination(warehouse_rel)
    if not warehouse.exists():
        build_warehouse(
            [read_bundle(root.destination(relative)) for relative in outputs],
            warehouse,
            temp_directory=root.destination("tmp"),
        )
        stats["warehouse_written"] = 1
    else:
        stats["warehouse_skipped"] = 1
    checkpoint += 1
    write_checkpoint(root, census, checkpoint, started, "complete", stats, [warehouse_rel], "D")
    return {"status": "complete", "stats": dict(stats), "outputs": [warehouse_rel]}
