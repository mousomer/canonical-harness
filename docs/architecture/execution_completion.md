# Shared execution completion contract

The shared governance pack owns completion-based execution for local tests,
scripts and remote CI. MANIFEST.json binds policies/execution.json with the
synchronous-completion-v2 identity, which supersedes v1 and withdraws its
blocking-watch allowance. Runtime 0.9.0 requires the binding and its complete,
exactly equal requirements; missing, weakened and foreign/symlinked policy bytes
fail validation even if a caller regenerates the ordinary pack lock.

## Root cause of the v1 defect

v1 declared `remote_completion: blocking_watch_or_notification` and permitted
"a blocking watch's internal service queries". `gh pr checks --watch` re-queries
GitHub every 10 seconds and reprints, so it was policy-compliant polling. No
runtime hook enforced anything (`gov check` proves binding, not behaviour), and
the observatory recorded `gh run watch` as a compliant blocking watch and any
`gh pr checks`, including `--watch`, as a single query.

## Enforcement

`command_policy.py` is the single semantic definition. It parses a supported
shell subset (quotes, operators, loops, functions, command/process
substitution, heredocs and here-strings, `sh/bash/zsh -c` recursion, `eval`,
common wrappers such as `env`, `timeout`, `nohup`, and GitHub CLI aliases) and
returns ALLOW, DENY or UNDETERMINED. Remote-looking invocations it cannot
classify (`case`, dynamic program names, unknown `gh` extensions, inline
interpreters combining remote access and waiting, unparseable text) are
UNDETERMINED and denied. It never executes, contacts a service or writes.

`hooks/no_polling.sh` launches `hooks/no_polling.py` under the consumer's
governance bootstrap interpreter (`scripts/resolve_bootstrap_python.sh`, else
`GOVERNANCE_PYTHON`). Any launcher or classifier failure exits 2, which both
runtimes treat as a pre-execution block; the UserPromptSubmit mode never
blocks. Repeated observations of the same target are denied within one epoch:
the Codex `turn_id`, or for Claude Code the span between UserPromptSubmit
events. Epoch state is a small per-session file under the temporary directory.

`hook_adapters.py` merges one managed entry per event into
`.claude/settings.json` (`Bash|Monitor|mcp__.*`, UserPromptSubmit) and
`.codex/hooks.json` (`Bash|mcp__.*`, UserPromptSubmit), preserving unrelated
configuration. The fleet installer renders them; `gov check` reports
`execution.enforcement` PACK_DRIFT when either binding, the guard or its mode is
missing or altered, or hooks are disabled.

`gov ci-status <pr>` makes one GraphQL request and returns PASS (0), FAIL (10),
PENDING (11), UNKNOWN (12) or ERROR (13); zero, truncated, missing or stale
check sets are UNKNOWN, and a failed request is ERROR, never a CI failure.

## Coverage limits (explicit)

- Timeouts: the managed Claude PreToolUse handler sets `onFailure: "block"`
  (honoured from Claude Code 2.1.295; observed blocking a hung hook in 2.1.317).
  Independently, the launcher kills the classifier at a 20 s deadline and the
  classifier arms an 18 s alarm, both well inside the 30 s hook timeout, and
  both turn a hang into a block (observed live with `onFailure: "continue"`).
  Remaining fail-open: a Claude runtime older than 2.1.295 or Codex whose hook
  process is killed or stalled outside the launcher (for example the shell
  itself never starts) before either deadline fires; a handler without
  `onFailure` was observed to let a timed-out call proceed.
- Observation identity: the epoch is Codex `turn_id`, else Claude `prompt_id`
  (observed in 2.1.317), else the UserPromptSubmit token. Subagents share the
  parent's `session_id` and `prompt_id` (observed), so a read delegated to a
  subagent in the same turn is a repeat. The agent's own `git push` or `gh`
  pr/run/workflow/release mutation authorizes one new read, at most twice per
  turn; a no-op push still counts, which is a recorded residual.
- Codex project hooks run only in a trusted project and after the hook hash is
  trusted (`/hooks` or the app's Settings > Hooks); untrusted or changed hooks
  are skipped silently (observed: `codex exec` ran `gh pr checks 176 --watch`
  with the untrusted hook present). `write_stdin` and hosted tools bypass
  PreToolUse. Malformed hook output fails open in Codex.
- Codex 0.153.2 reads a linked git worktree's project `.codex/` from the main
  checkout, ignoring the worktree's own `.codex/hooks.json` (observed with the
  app-server `hooks/list`). Codex enforcement in any worktree therefore needs
  the adopted bindings in the main checkout; the Codex command falls back to the
  main checkout's guard when the worktree's own commit predates the pack.
- Defense in depth for Codex: `hooks/codex_no_polling.rules`, installed
  separately as `~/.codex/rules/workspace-governance-no-polling.rules`, forbids
  `gh run watch`, `watch` and `gh pr checks` with a leading watch/interval flag
  without hook trust (observed: rejected inside `zsh -lc`). Prefix rules cannot
  match `gh pr checks 176 --watch`; only the trusted hook covers that form.
- Claude Code opens a new observation epoch on every UserPromptSubmit,
  including host task and CI notifications (observed), so any notification, not
  only one about the observed target, authorizes one new observation.
- `--dangerously-bypass-hook-trust`, `disableAllHooks` and `[features] hooks =
  false` disable enforcement; `gov check` detects only the repository-level
  `disableAllHooks`.
- Shell scripts the command runs (`bash f.sh`, `source f`, `./f` with a shell
  shebang) are read (64 KiB bound) relative to the hook `cwd` and classified;
  an unreadable script run by a shell is UNDETERMINED. Shell functions defined
  in earlier tool calls, Makefiles, compiled programs and non-shell scripts on
  disk are not inspected. MCP tools are covered only for repeated
  status-named tools (name tokens such as `status`, `checks`, `ci`).
- Observation keys normalize spellings of one target (`gh pr checks 7`, the PR
  URL, `gh pr view 7 --json statusCheckRollup`, `./gov ci-status 7` share
  `pr:7`), but `gh pr checks` without a number (current branch) is a separate
  key that cannot be resolved statically.
- Scheduled re-invocation (`ScheduleWakeup`, `CronCreate`, `/loop`) is agent
  callable in Claude Code and not intercepted; the matcher covers shell,
  Monitor and MCP tools only.
- The Codex rules file is installed manually; the pack neither installs nor
  checks it.
- `gov check` reports an adapter that git ignores, because an ignored binding
  never reaches clones, CI or new worktrees.

## Review

`execution_policy.review_execution` applies the same classifier to explicit
traces. It distinguishes blocked attempts, executed polling, repeated
observations, compliant one-shot observations and notifications; pending
observations report `pending_external`, never success. The Harness Observatory
loads the same `command_policy.py` by path, records the classifier version on
every derived event, and reports `polling_classifier_unavailable` when it
cannot. Historical normalized bundles are not rewritten: extractor 0.4.0
reconstruction is versioned separately.
