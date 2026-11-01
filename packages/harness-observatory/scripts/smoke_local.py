"""Bounded post-landing smoke. All corpus values and reports stay in the private root."""

import subprocess
from collections import Counter
from pathlib import Path
from uuid import uuid4

from duckdb import Error as WarehouseError
from jsonschema import ValidationError

from harness_observatory.cli import SafeArgumentParser
from harness_observatory.data_root import OutputBoundaryError, resolve_data_root
from harness_observatory.enrichers import enrich_git
from harness_observatory.extractors import extract
from harness_observatory.model import EXTRACTOR_VERSION, VERSION, digest
from harness_observatory.warehouse import build_warehouse


def smoke(root, sources):
    operation = uuid4().hex
    report = {
        "schema_version": VERSION,
        "data_root_id": root.id,
        "extractor_version": EXTRACTOR_VERSION,
        "enrichment_version": "0.1.0",
        "sanitize_output": False,
        "providers": {},
        "records": [],
    }
    bundles = []
    for provider, source in sources.items():
        paths = sorted(Path(source).rglob("*.jsonl"))
        if not paths:
            raise ValueError("empty_source")
        count = min(10, len(paths))
        selected = [paths[i * (len(paths) - 1) // max(1, count - 1)] for i in range(count)]
        stats = Counter()
        for path in selected:
            raw = path.read_bytes()
            preserved = root.preserve_raw(provider, raw)
            bundle = extract(raw, provider, sanitize_output=False)
            key = digest(raw).split(":", 1)[1]
            normalized = f"normalized/runs/{operation}-{provider}-{key}.json"
            root.write(normalized, bundle)
            stats["sessions"] += 1
            stats["events"] += len(bundle["events"])
            stats.update(w["code"] for w in bundle["warnings"])
            facts = bundle["run"]["facts"]
            cwd, sha = facts["cwd"]["value"], facts["start_sha"]["value"]
            if cwd and sha and Path(cwd).is_dir():
                try:
                    bundle = enrich_git(bundle, cwd, scope_path=".", sanitize_output=False)
                    stats["historical_governance_joins"] += bool(bundle["governance_snapshots"])
                except (ValueError, OSError, subprocess.CalledProcessError):
                    stats["unresolved_git_evidence"] += 1
            enriched = f"enriched/git/{operation}-{provider}-{key}.json"
            root.write(enriched, bundle)
            bundles.append(bundle)
            report["records"].append(
                {
                    "provider": provider,
                    "original_source": str(path.resolve()),
                    "raw": str(preserved.relative_to(root.path)),
                    "normalized": normalized,
                    "enriched": enriched,
                }
            )
        report["providers"][provider] = dict(stats)
    output = root.destination(f"warehouse/smoke-{operation}")
    build_warehouse(bundles, output, temp_directory=root.destination("tmp"))
    report["warehouse"] = str(output.relative_to(root.path))
    report["status"] = (
        "passed"
        if all(not s.get("malformed_record") for s in report["providers"].values())
        else "needs_review"
    )
    root.write(f"logs/smoke-{operation}.json", report)
    if report["status"] != "passed":
        raise ValueError("malformed_records_need_private_review")
    return report


def main(argv=None):
    parser = SafeArgumentParser()
    parser.add_argument("--data-root")
    parser.add_argument("--codex-root", required=True, type=Path)
    parser.add_argument("--claude-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        root = resolve_data_root(args.data_root)
        smoke(root, {"codex": args.codex_root, "claude": args.claude_root})
    except (
        OutputBoundaryError,
        ValidationError,
        WarehouseError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
    ):
        parser.exit(
            2,
            "Bounded smoke failed [private_smoke_failure]; inspect the private report if created.\n",
        )
    print("Bounded private smoke passed. Artifacts and report remain under the data root.")


if __name__ == "__main__":
    main()
