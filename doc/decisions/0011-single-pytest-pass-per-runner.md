# 0011 — Run the test suite once per runner, made mutually exclusive by step-level `if`, not a shell conditional

- **Status:** proposed
- **Date:** 2026-10-08
- **Source:** commit `8472cc9`
- **Footprint:** `.github/workflows/ci.yml:37-45`

## Context

The CI matrix is `[ubuntu-latest, windows-latest]`. Two steps both invoked the full
1219-test suite: a plain `pytest` step, and a Linux-gated coverage step. Both ran on
Linux, so the Linux job paid for the suite twice — roughly **134 seconds** wasted per
run.

The two steps cannot simply be merged, because only the Linux runner emits the lcov
report that the repowise job downloads (see
[0010](0010-repowise-ci-gate-ships-non-blocking.md)), and the coverage flags differ
from the plain invocation.

The conditioning mechanism was the actual decision. The job declares no `shell:`
override, so the Windows runner's default shell is PowerShell. A shell conditional
written for bash is a syntax error there, not a no-op.

## Decision

Keep both steps and make them mutually exclusive with step-level
`if: runner.os == 'Windows'` and `if: runner.os == 'Linux'`, so exactly one runs per
runner and the Linux coverage pass also covers the plain case.

## Consequences

This buys one full suite execution per runner instead of two, and it keeps the
`--cov` flags on the step that needs them rather than on the step that runs on
Windows — where `pytest-cov` output is not consumed by anything.

The cost is the coupling: the two steps now have to be kept in sync manually. Adding
an environment variable or a flag to one and not the other silently changes what each
runner tests. The workflow comment at line 38 records the related constraint — the
collection steps must stay in the same uv/Python environment.

The rejected alternative was a shell conditional (`if: runner.os == 'Linux' && ...`
evaluated inside the step, or a bash `if` inside `run:`). It was rejected because the
Windows leg's default shell is PowerShell and the job has no `shell:` override, so the
same conditional cannot be written for both.

A second rejected alternative was to drop the Windows leg. It was rejected because
Windows-specific failures were real and live: see
[0013](0013-resolve-external-binaries-through-shutil-which.md).

## Evidence

```
sed -n '36,48p' .github/workflows/ci.yml
```

Line 39 is the plain `uv run python -m pytest -q` gated to Windows; line 45 is the
coverage invocation gated to Linux, with the comment "Single pytest pass per runner;
the Linux run also emits the lcov report consumed by the repowise job".

Provenance: `git show 8472cc9`, whose body states the 134-second figure and the
PowerShell reason for preferring step-level `if`.
