# Portable workspace-governance verification component

This directory is the canonical suite for reusable runtime contracts. It is a
release verification component, outside the consumer installer payload. The
kit's full gate selects and validates an exact runtime and this component before
starting the suite. Ordinary context commands do not launch these tests.

The dependency and executable contract is `verification-dependencies.json`.
Use the already governed interpreter; checks do not create an environment,
install packages, access a product model, or require network access. The test
runtime needs Python 3.11 or newer, pytest, packaging, Ruff 0.16.0, Git, Bash and GNU Make 3.81
or newer. The gate rejects an unavailable or incompatible Make before dispatch. Integration
fixtures execute synthetic shell/Python checks only.
The complete kit gate runs read-only runtime lint/C901 and formatting phases
before pytest, without installing dependencies. Each isolated child checks that
verifier packages originate in the selected interpreter's installed libraries.
Failure reports retain tool outcomes, full output byte counts and hashes, and
bounded diagnostic codes/relative locations without source or fix contents.

For an editable candidate, the declared invocation is:

```sh
<governed-python> -m pytest -q tests/workspace_governance
```

For an extracted immutable release, the kit-owned gate points
`POLICY_KIT_RUNTIME_ROOT` at that release's `payload/tools/workspace_governance`
and invokes this component's tests. The suite checks imported module origins
and schema/catalog confinement. A preimported foreign runtime is rejected;
poisoned ambient Python imports and overlay paths cannot supply fixture truth.

`support.py` constructs complete synthetic Python and tools/native consumers.
The tools consumer has no application metadata. Both have their own human and
structured authorities, routes, generated relationships, instruction roots and
verification declarations. Multi-checkout tests use independent synthetic source
packages against the same interpreter and workspace identity. Fixtures isolate
configuration and telemetry state without reading a host execution declaration.
They never clone a product checkout or import its package.

The suite owns consumed-field and behavior-family proof; contradiction catalog
execution and every advertised diagnostic class; authority, role, route,
membership, environment, source-binding, overlay and project-independence
contracts; telemetry isolation/readiness; fleet installation/undo/execution and
evidence boundaries; sanitation; and deterministic release export. Consumed
field tests disable schema rejection as evidence, observe the applicable
semantic family, and fail on unexpected exceptions. New consumed optional
fields require representative behavioral fixtures.

Tet4D retains its six-owner authority assertions, specific Godot route/scenario
matrix, application bootstrap/packaging integration, policy references and actual
canonical gate tests. Its verifier should invoke this shared suite through the
kit-owned command and retain those consumer adapters. Generic contract copies in
a consumer must not become a second policy-kit authority; retire their generic
portions as that consumer's adapter migration is reviewed. Product acceptance
remains a separate invocation of each consumer's declared verifier.

Make ordering fixtures execute the generated runner under serial and parallel
Make and check event order and absence, including combined goals and governance
prerequisite failures. They exercise reviewed build boundaries and reject unsafe
graphs; successful installation alone is not their acceptance criterion.

Fixture output, coverage and test counts are verification of these component
bytes. They do not establish acceptance for unrelated product repositories or
strengthen the same-user trust limit of collector-reported execution evidence.
