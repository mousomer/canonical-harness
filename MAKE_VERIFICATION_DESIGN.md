# Make verification gate ordering

## Objective and authority

The fleet Make runner owns the generated dependency edges that order governance
before declared product verification recipes. Consumer recipes, build rules,
stronger local checks and product acceptance remain consumer authority. The old
sibling prerequisite on a verification root did not order its prerequisite test
recipes, especially under parallel Make.

## Bounded graph contract

Parse explicit single-colon rules for the declared verification roots and their
literal prerequisite closure. Add an order-only edge to the shared governance
gate for every root and reachable recipe target. GNU Make 3.81 and later support
this form; no `.WAIT` or global parallelism restriction is required. Order-only
edges preserve normal `$^`, `$?` and freshness behavior; `$|` includes the gate.
The gate's own prerequisite closure remains ungated, so project routing checks
can execute before the baseline without a generated dependency cycle. A recipe
independently reachable as both a product branch and a gate prerequisite rejects;
excluding it would otherwise permit product tests before baseline rejection.

`runner.build_prerequisites` is an optional reviewed list of prerequisite tokens
that stop traversal at ordinary build branches. They may build before the gate;
they must not execute product verification or redefine verification/gate targets.
This explicit boundary supports variable-expanded compiler and artifact graphs
without pretending that the installer evaluates arbitrary Make expressions.
Unused, duplicate, root or gate-ancestry exclusions are rejected. Every other
reachable prerequisite must have a supported explicit literal definition.

`runner.build_includes` is an optional exact list of optional include expressions
(`-include` or `sinclude`) reviewed as ordinary build dependency metadata. Such
files must not define verification or governance rules. Other includes are
rejected. These declarations are owner assertions checked against the installed
runner structure, not proof of included contents or arbitrary Make semantics.
`runner.build_rules` lists exact dynamic or implicit-pattern rule left-hand
expressions reviewed as ordinary build machinery. Only relative simple variable,
pattern or filter expressions are accepted. Unlisted dynamic rules reject; listed
rules must not redefine verification/gate targets or add product test branches.
These declarations also remain owner assertions, not evaluated target sets. Dynamic verification rules, double-colon/static-pattern rules in
the inspected closure, secondary expansion, effectful eval/call/guile/shell/file expansion outside recipes, recipe-prefix changes,
conditionals, standalone expansion statements, escaped target names and dependency
cycles, multiple order-only separators, failure-ignoring directives and special
variable, custom shell and reserved Make control assignments are unsupported and receive a diagnostic.

The proof assumes the reviewed source/declarations and clean governed Make
invocation. Ambient `MAKEFILES`/`MAKEFLAGS`/`GNUMAKEFLAGS`, alternate makefiles,
command-line rule-variable substitutions and failure-ignoring flags do not gain
ordering proof from static installation. Fleet collection strips ambient Make
control variables and validates the declared command separately.

Existing managed sections are excluded from graph discovery on regeneration;
project-owned gate prerequisite additions remain part of discovery. Generation
is deterministic, preserves all consumer-owned bytes, and replaces only its
managed section. No shell-runner behavior changes.

## Acceptance and release

Real disposable Make fixtures must observe passing and rejected ordering under
serial and parallel Make, combined declared goals, direct prerequisite goals and
failure of a governance prerequisite. A nonzero eventual exit is insufficient:
rejection must leave every product-test event absent. Positive probes must retain
all declared product checks, automatic-variable behavior and unchanged artifacts.
Fixtures also cover graph exclusions, unsupported forms, cycles, deterministic
plan/apply, guarded preimages, idempotence and undo preserving unrelated edits.

Run focused Make/fleet tests and the complete kit candidate verifier, then export
and verify a successor immutable release before guarded adoption. Keep existing
release archives immutable. Retire consumer workaround edges only after the new
release is adopted and the same consumer probes pass without the workaround.
Rollback restores the reviewed installation receipt and preserves later owner
edits; failed adoption retains the existing pin and local containment.
