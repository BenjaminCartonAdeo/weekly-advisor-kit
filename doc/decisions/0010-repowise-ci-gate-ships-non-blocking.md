# 0010 — The repowise CI gate starts non-blocking, pinned to a release tag, with `coverage-fail-under: 0`

- **Status:** proposed
- **Date:** 2026-10-08
- **Source:** commit `225a15b`
- **Footprint:** `.github/workflows/ci.yml:120-162`

## Context

A `repowise` gate was added to CI as a sibling job: it needs `test`, checks out full
history, downloads the lcov artifact, and runs coverage, security and risk checks.

Three properties had to be decided before it could be turned on, and each one is a
trade rather than a setting.

`coverage-fail-under` compares **patch** coverage on changed lines against the
project's overall figure (88.47%). These are different numbers. Reusing the project
figure on a 3-line change is a false negative machine.

`doc-drift` validates the entire tree against a committed baseline. This repository's
documentation has already drifted, and the baseline cannot be generated from inside
CI — it needs `repowise doc-drift --check --write-baseline` run locally and committed.
Enabling it would have failed from the first run, on a check whose subject is
documentation quality rather than code quality.

The `risk` gate ranks a change against the repository's own recent commits and needs a
merge-base with the target branch. A default shallow checkout makes it exit 2 and
guess, so `fetch-depth: 0` is required.

## Decision

Ship the gate with `continue-on-error: true`, `checks: coverage,security,risk`
(doc-drift excluded), `coverage-fail-under: 0`, `coverage-min-coverable-lines: 5`, and
`uses:` pinned to the `v0.55.0` release tag rather than `main`.

## Consequences

This buys a gate that runs on every pull request and reports real signal from day
one, without the failure mode where a brand-new untested gate turns every open pull
request red. The three exclusions each carry their reason as a comment in the
workflow, so a future reader knows they are deliberate and what to do about them.

The cost is that none of this blocks anything yet. `continue-on-error: true` makes the
whole job advisory, and `coverage-fail-under: 0` means the coverage check reports and
never fails. The workflow says so at line 143-145: remove `continue-on-error` "once
the gates have been green long enough to trust them". Until then, a critical security
regression does not stop a merge.

The rejected alternative was to enable the gate blocking on arrival. It was rejected
because the gates had never run against this repository — a first red gate on an
untested check blocks everything and gets the gate switched off rather than fixed.

A second rejected alternative was to set `coverage-fail-under: 88.47`, the project
figure. It was rejected because patch coverage and project coverage are different
measures; the gate would have reported failure on changes that are fully covered.

A third rejected alternative was to pin to `main`. Rejected in favour of the release
tag so that the `uses:` ref and the `version:` input name the same release, and a
retagged `main` cannot change what CI runs.

## Evidence

```
sed -n '120,162p' .github/workflows/ci.yml
```

Every exclusion in this record is present as a comment in that block: `fetch-depth: 0`
at line 131, the non-blocking rationale at 143-145, the doc-drift exclusion at
148-152, and the coverage-scope note at 155-157.

The `path=prefix` form at line 154 is a separate necessary detail: the lcov `SF`
records are package-relative while the repowise tree is repo-root relative, so
without the prefix every path is unmatched and the gate exits 2.

Provenance: `git show 225a15b`.
