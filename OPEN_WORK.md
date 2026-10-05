# Policy-kit open work

## Published Make verification ordering repair

- Distribution 1.3.2/runtime 0.6.2 implements the bounded Make graph contract in
  `MAKE_VERIFICATION_DESIGN.md`. Full candidate and selected immutable gates
  passed 641 tests; immutable reconstruction returned unchanged content.
- Guarded adoption installed 32 proven direct consumers without installer
  failures. The following plan reports all 32 unchanged. Protected/read-only
  locations, excluded checkouts, execution holds and historical evidence remain
  preserved.
- All 32 fresh governance baselines passed with stable inputs. Three of five
  approved product verifiers passed, including Xyntific after its temporary edge
  was retired and all 18 ordering/routing probes and its full verifier passed.

## Follow-up: consumer source-style compatibility

Fresh product collection found new Make-parser complexity and formatting failures
in harness-observatory. Refactor the parser into bounded helpers and format the
changed source under the consuming verifier contract; preserve diagnostics,
ordering semantics and rejection boundaries. Do not alter published 1.3.2 bytes
or suppress the consumer checks. Publish and verify a successor, adopt it with
guarded preimages, and recollect fresh product proof.

Gifted-masking also failed its declared product verifier with stable inputs.
That failure remains recorded and requires separate diagnosis. Governance-only
consumers, execution holds, blocked locations and scan gaps remain explicit
nonzero audit findings. Shared source gates and installation do not establish
whole-fleet product acceptance.
