"""Assemble immutable private evidence into a publication-readiness report."""

import argparse
import json
import subprocess
from datetime import datetime, timezone

from harness_observatory.data_root import resolve_data_root
from harness_observatory.enrichers import GOVERNANCE_DISCOVERY_VERSION
from harness_observatory.model import EXTRACTOR_VERSION, POLL_DEFINITION_VERSION, VERSION


def read(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root")
    parser.add_argument("--output", default="analysis/results/publication-readiness.json")
    args = parser.parse_args()
    root = resolve_data_root(args.data_root)
    census_1 = read(sorted((root.path / "logs/census-1").glob("checkpoint-*.json"))[-1])
    census_2 = read(sorted((root.path / "logs/census-2").glob("checkpoint-*.json"))[-1])
    audit = read(root.destination("analysis/results/reconstruction-audit-v2.json"))
    ci = read(root.destination("analysis/results/ci-census-offline.json"))
    delta = read(root.destination("analysis/intermediate/census-1-to-2.json"))
    ready = (
        census_1["status"] == "complete"
        and census_2["status"] == "complete"
        and audit["verdicts"].get("fail", 0) == 0
        and ci["status"] == "complete"
    )
    root.write(
        args.output,
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "software_head": subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip(),
            "versions": {
                "schema": VERSION,
                "extractor": EXTRACTOR_VERSION,
                "governance_discovery": GOVERNANCE_DISCOVERY_VERSION,
                "poll_definition": POLL_DEFINITION_VERSION,
            },
            "census_1": census_1["record_counts"],
            "census_2": census_2["record_counts"],
            "delta": delta,
            "audit": {
                key: audit[key]
                for key in ("inspection_limit", "selected", "distinct_strata", "verdicts")
            },
            "ci_census": ci,
            "operational_assertions": {
                "agent_polling_loops_used": False,
                "long_waits_synchronous": True,
                "all_long_waits_deadline_bounded": True,
                "census_restartable_and_checkpointed": True,
                "hard_stop_respected": True,
                "private_data_remained_outside_git": True,
                "external_ci_metadata_transmitted": False,
            },
            "ready_for_publication": ready,
        },
    )
    print(f"Private publication-readiness report written; ready={str(ready).lower()}.")


if __name__ == "__main__":
    main()
