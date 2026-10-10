# 0012 — Never put `working-directory` on a `uses:` step; rely on the job's own default working directory

- **Status:** proposed
- **Date:** 2026-10-08
- **Source:** commit `267bf2d`
- **Footprint:** `.github/workflows/ci.yml:136-142`

## Context

The repowise job needed to run from a specific directory, so a `working-directory`
key was added to the `uses: repowise-dev/repowise` step.

GitHub rejects `working-directory` on a `uses` step. It is only valid on `run`. The
rejection made the **entire workflow file invalid** — not just that job. Every run
failed with 0 jobs executed, and the run name fell back to the file path, which is
what made the failure legible as a workflow error rather than as a failing job.

The premise behind the key was also wrong. The repowise job declares no
`defaults.run`, so its steps already execute from the repository root. The
`working-directory` was not fixing anything.

## Decision

Remove `working-directory` from the `uses:` step entirely and let the repowise action
resolve paths from the repository root, which is where the job already runs.

## Consequences

This buys a valid workflow file and a step whose paths resolve the way the action
expects — repowise reads from the repository root, while the test job defaults to the
package directory. The two jobs need different roots, and this is how the repowise job
gets one.

The cost is that the working directory of that step is now implicit — set by the
absence of `defaults.run` rather than by anything visible on the step. That is why
lines 137-141 of the workflow spell it out in a comment: they state both that the job
has no `defaults.run` and that `working-directory` is deliberately absent because
GitHub rejects it on a `uses` step.

The rejected alternative was moving the key to a wrapping `run:` step. It was rejected
because it would have been fighting the action's own entrypoint, which takes its paths
as inputs and does its own resolution.

## Evidence

```
sed -n '120,146p' .github/workflows/ci.yml
```

The comment block at lines 137-141 states the rejected key and the reason. Line 142 is
the bare `uses: repowise-dev/repowise@v0.55.0` with no `working-directory`.

Note that the `repowise` job also carries `fetch-depth: 0` at line 131 for an
unrelated reason recorded in
[0010](0010-repowise-ci-gate-ships-non-blocking.md).

Provenance: `git show 267bf2d`, whose body records that every run was failing with 0
jobs because the invalid key invalidated the whole file.
