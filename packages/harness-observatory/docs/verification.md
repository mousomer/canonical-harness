# Verification

Run `scripts/verify.sh` in the project Python environment. This runs lint, formatting, synthetic unit/regression tests, CLI import, the synthetic Git/DuckDB/Parquet demonstration and `scripts/verify_public_safe.sh`.

The canonical repository publication gate validates an exact software allowlist, worktree/index content and newly reachable history relative to the pinned published base. Its full-history audit reports pre-existing historical findings separately. It checks both current tracked files and index blobs, every historical tree path and reachable blob, plus commit and tag messages. It excludes unreachable objects and reflogs. Run `git fsck --unreachable --no-reflogs` separately to inventory local-only garbage without treating it as a publication blocker.

All regression inputs are generated synthetically in temporary directories, including deliberately unsafe strings assembled in temporary test repositories. No real historical source, account, network connection or Git hook is needed. Tests cover canonical-path output rejection, sanitized diagnostics, deleted-but-reachable secrets, alternate refs and safe synthetic fixtures.

The end-to-end demonstration uses temporary synthetic Git history and real adapters, historical governance hashing, DuckDB and Parquet. Its SQL result is one run per provider and one observed CI poll per run. Qualitative reconnaissance conclusions are in source-capability-matrix.md; private corpus statistics are intentionally omitted.

Private-root regression coverage includes initialization, explicit/environment precedence, any-Git and linked-worktree rejection, traversal/symlink/case aliases, private output confinement, raw preservation, bounded synthetic smoke, and a complete private CLI workflow. Real sources are not part of this suite. The separately authorized post-landing smoke records its details only under the private root; no real-data results belong in this document.
