# Project governance

Design authority: docs/architecture.md and docs/data-model.md. Before changing behavior, update affected design and docs/BACKLOG.md. Verify with scripts/verify.sh. Use synthetic fixtures only. Preserve missing values and provenance; never infer historical Git state from current HEAD. No real evidence in Git. Keep this a small local library and CLI.
