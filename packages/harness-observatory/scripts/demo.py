"""Real adapters + temporary synthetic Git history + real DuckDB/Parquet, no network."""

import subprocess
import tempfile
from pathlib import Path

import duckdb

from harness_observatory.enrichers import enrich_actions, enrich_git
from harness_observatory.extractors import extract
from harness_observatory.warehouse import build_warehouse
from synthetic_inputs import synthetic_bytes


def main():
    with tempfile.TemporaryDirectory(prefix="observatory-demo-") as temp:
        root = Path(temp)
        repo = root / "repo"
        repo.mkdir()

        def git(*args):
            return (
                subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)
                .decode()
                .strip()
            )

        git("init", "-b", "main")
        git("config", "user.name", "Synthetic Demo")
        git("config", "user.email", "demo@example.invalid")
        (repo / "AGENTS.md").write_text("Verify synthetic changes.\n")
        git("add", ".")
        git("commit", "-m", "Synthetic governance")
        start = git("rev-parse", "HEAD")
        (repo / "example.py").write_text("print('synthetic')\n")
        git("add", ".")
        git("commit", "-m", "Synthetic change")
        end = git("rev-parse", "HEAD")
        bundles = []
        for provider in ["codex", "claude"]:
            bundle = extract(synthetic_bytes(provider), provider)
            bundle = enrich_git(
                bundle, repo, start, end, "synthetic:demo-endpoints", scope_path="."
            )
            bundle = enrich_actions(
                bundle,
                [{"id": 123, "head_sha": end, "status": "completed", "conclusion": "success"}],
                "synthetic/example",
            )
            bundles.append(bundle)
        path = build_warehouse(bundles, root / "warehouse")
        with duckdb.connect(str(path), read_only=True) as db:
            rows = db.execute(
                "SELECT agent, governance_snapshot_id, COUNT(*) AS runs, AVG(ci_poll_count) AS avg_ci_polls FROM runs GROUP BY 1,2 ORDER BY 1"
            ).fetchall()
            assert len(rows) == 2 and all(row[2:] == (1, 0.0) for row in rows)
            for row in rows:
                print(" | ".join(map(str, row)))


if __name__ == "__main__":
    main()
