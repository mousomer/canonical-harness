# Prior art and reuse

Inspected an existing local session-extraction utility, an adjacent governance-manifest implementation, reusable governance templates, and a personal repository’s GitHub Actions history. No source content or operational identifiers are retained here.

Reused conventions: src-layout Python, JSONL, UTC normalization, SHA-256 content addressing, a single verification entry point, explicit design authority and backlog.

Kept independent: heuristic recursive field extraction cannot establish evidence-grade facts. Prompt cache hashes do not define governance snapshots. Governance templates require explicit routing. No large subsystem, shared schema modification, or historical fixture was copied.

Actions exposes run identifiers, associated SHA, status, conclusion and creation/start/update timestamps. Update time is not guaranteed completion time.
