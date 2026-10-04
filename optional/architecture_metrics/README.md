# Optional Architecture Metrics Pack

Shared optional module for workspace repositories:

- class-aware folder balancer (`code_default`, `tests_lenient`, `non_code_exempt`)
- tech-debt score builder (backlog priority, backlog bug, CI gate, code balance)
- folder-balance and tech-debt gate evaluators
- budget refresh helpers

Import path:

- add `${POLICY_KIT_DIR:-${HOME}/workspace/policy-kit}/optional/architecture_metrics` to `sys.path`
- import from `architecture_metrics`

This pack is optional and does not change baseline template-hash enforcement.
