"""Composable commands. Source reads never modify evidence."""

import argparse
import json
import subprocess
from uuid import uuid4

from duckdb import Error as WarehouseError
from jsonschema import ValidationError
from pathlib import Path

from .data_root import (
    OutputBoundaryError,
    outside_git,
    write_json_new,
    init_data_root,
    resolve_data_root,
)
from .model import EXTRACTOR_VERSION, VERSION, digest, dumps
from .enrichers import enrich_actions, enrich_git, fetch_actions
from .extractors import extract, inspect_source
from .warehouse import build_warehouse, read_bundle


def private_destination(path):
    return outside_git(path)


def write_bundle(path, bundle):
    # Low-level writer still refuses every Git worktree. CLI writes additionally
    # require an initialized root and confinement beneath it.
    path = private_destination(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json_new(path, bundle)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Operation failed [invalid_arguments].\n")


def main(argv=None):
    parser = SafeArgumentParser(prog="harness-observatory")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init-data-root")
    p.add_argument("path")
    for name in ["inspect-source", "extract"]:
        p = sub.add_parser(name)
        p.add_argument("provider", choices=["codex", "claude"])
        p.add_argument("source", type=Path)
        p.add_argument("--data-root")
        p.add_argument("--output", help="Destination within the private root")
    p = sub.add_parser("enrich-git")
    p.add_argument("bundle")
    p.add_argument("--data-root")
    p.add_argument("--repo", required=True)
    p.add_argument("--start-sha")
    p.add_argument("--end-sha")
    p.add_argument("--evidence")
    p.add_argument("--document", action="append", default=[])
    p.add_argument("--scope-path")
    p.add_argument("--output")
    p = sub.add_parser("enrich-ci")
    p.add_argument("bundle")
    p.add_argument("--data-root")
    p.add_argument("--repository", required=True)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--actions-json", type=Path)
    source.add_argument("--fetch-sha")
    p.add_argument("--output")
    p = sub.add_parser("build-warehouse")
    p.add_argument("bundles", nargs="+")
    p.add_argument("--data-root")
    p.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        if args.command == "init-data-root":
            init_data_root(args.path)
            print("Private data root initialized.")
            return
        root = resolve_data_root(args.data_root)
        if args.output:
            root.destination(args.output)
        if args.command in {"inspect-source", "extract"}:
            raw = args.source.read_bytes()
            source_key = digest(raw).split(":", 1)[1]
            if args.command == "inspect-source":
                root.write(
                    args.output or f"logs/inspection-{source_key}.json",
                    inspect_source(raw, args.provider),
                )
            else:
                root.preserve_raw(args.provider, raw)
                bundle = extract(raw, args.provider, sanitize_output=False)
                key = bundle["run"]["run_id"].split(":", 1)[1]
                root.write(args.output or f"normalized/runs/{key}.json", bundle)
        elif args.command == "enrich-git":
            bundle = enrich_git(
                read_bundle(root.destination(args.bundle)),
                args.repo,
                args.start_sha,
                args.end_sha,
                args.evidence,
                args.document,
                scope_path=args.scope_path,
                sanitize_output=False,
            )
            key = bundle["run"]["run_id"].split(":", 1)[1]
            root.write(args.output or f"enriched/git/{key}.json", bundle)
        elif args.command == "enrich-ci":
            if args.actions_json:
                raw = args.actions_json.read_bytes()
                records = json.loads(raw)
            else:
                records = fetch_actions(args.repository, args.fetch_sha)
                raw = dumps(records).encode()
            root.preserve_raw("github", raw)
            if isinstance(records, dict):
                records = records["workflow_runs"]
            bundle = enrich_actions(
                read_bundle(root.destination(args.bundle)),
                records,
                args.repository,
                sanitize_output=False,
            )
            key = bundle["run"]["run_id"].split(":", 1)[1]
            root.write(args.output or f"enriched/ci/{key}.json", bundle)
        else:
            bundles = [read_bundle(root.destination(p)) for p in args.bundles]
            # Content identity includes enrichment, not merely the original run IDs.
            key = digest(dumps(bundles).encode()).split(":", 1)[1]
            build_warehouse(
                bundles,
                root.destination(args.output or f"warehouse/{key}"),
                temp_directory=root.destination("tmp"),
            )
        root.write(
            f"logs/operation-{uuid4().hex}.json",
            {
                "schema_version": "0.1",
                "data_root_id": root.id,
                "canonical_schema_version": VERSION,
                "extractor_version": EXTRACTOR_VERSION,
                "enrichment_version": "0.1.0",
                "command": args.command,
                "sanitize_output": False,
            },
        )
        print("Private operation completed; artifacts remain under the configured data root.")
    except OutputBoundaryError as exc:
        parser.exit(
            2,
            f"Operation refused [{exc}]. Initialize an external non-Git directory with init-data-root; select it with --data-root or HARNESS_OBSERVATORY_DATA_ROOT.\n",
        )
    except ValidationError:
        parser.exit(2, "Operation failed [schema_validation].\n")
    except subprocess.SubprocessError:
        parser.exit(2, "Operation failed [subprocess_failure].\n")
    except WarehouseError:
        parser.exit(2, "Operation failed [warehouse_failure].\n")
    except OSError:
        parser.exit(2, "Operation failed [io_failure].\n")
    except (ValueError, KeyError, TypeError):
        parser.exit(2, "Operation failed [invalid_input].\n")


if __name__ == "__main__":
    main()
