"""Run-line wrapper for one synchronous bounded census invocation."""

import argparse
import subprocess

from harness_observatory.census import run_census
from harness_observatory.data_root import OutputBoundaryError, resolve_data_root


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root")
    parser.add_argument("--codex-root", required=True)
    parser.add_argument("--claude-root", required=True)
    parser.add_argument("--census", default="census-1")
    parser.add_argument("--deadline-seconds", type=int, default=1800)
    args = parser.parse_args()
    try:
        if not 1 <= args.deadline_seconds <= 1800:
            raise ValueError("invalid_deadline")
        result = run_census(
            resolve_data_root(args.data_root),
            {"codex": args.codex_root, "claude": args.claude_root},
            census=args.census,
            deadline_seconds=args.deadline_seconds,
        )
    except (OutputBoundaryError, OSError, ValueError, subprocess.SubprocessError):
        parser.exit(2, "Census failed [bounded_census_failure]; inspect private checkpoints.\n")
    print(f"Census returned: {result['status']}. Details remain in private checkpoints.")
    if result["status"] != "complete":
        raise SystemExit(3)


if __name__ == "__main__":
    main()
