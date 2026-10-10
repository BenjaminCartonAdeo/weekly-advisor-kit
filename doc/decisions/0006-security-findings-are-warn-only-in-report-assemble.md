# 0006 — Security findings are warn-only: `report-assemble` writes the report and sets `security.status = warn`

- **Status:** proposed
- **Date:** 2026-09-14
- **Source:** commit `a87c75e` (squash-merge of PR #4) — see the body line *"security findings warn-only in report assemble (rc=1, gates security.warn)"*
- **Footprint:** `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/report.py:3597-3643`

## Context

The harness digest feeds `report-assemble`. A security finding in that digest is
information about the repository's own agent configuration. The tension is that the
report is the channel through which a human learns about a security finding, and the
assembly step is the last automated step before a human reads anything.

Making security findings blocking looks correct and is wrong. A blocking gate exits
`rc>=2`, and `rc>=2` means **no report is written at all**. The one artefact that
would have told the reader about the security finding is the artefact that the
security finding suppressed.

The failure is also recursive in practice: the scanner flags its own configuration
surfaces, so a blocking security gate can fire on a repository that has done nothing
but adopt the scanner.

## Decision

`_assemble_security_gate` sets `status = "warn"` and returns a warning string. The
report is still assembled and written, and the process exits `rc=1` — a documented
warn-only degradation, not a pipeline failure.

## Consequences

This buys a guarantee that a security finding can never destroy the report that
describes it, at the cost of the strongest available signal: a critical security
finding no longer stops anything by itself. It has to be read.

The cost is real and is why this is recorded rather than merely implemented. A gate
that cannot block is advisory in the strict sense. The mitigation is that the
finding is carried into the machine-readable gates file
(`weekly-report-gates-<date>.json`) as well as the prose, so it is machine-visible
even though it is not process-blocking.

The rejected alternative was to make security findings block and exit `rc=2`. It was
rejected for the reason above: blocking destroys the report. A run that finds a
critical security issue and then produces nothing to read about it has failed at the
one job it was asked to do.

A second rejected alternative was to keep the block but still write the report, i.e.
`rc=2` *and* a file. It was rejected because `rc` is the contract every consumer
reads, and an `rc=2` with an artefact present is a state no consumer handles.

## Evidence

Read the gate:

```
rg -n '_assemble_security_gate' -A48 \
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/report.py
```

`report.py:3600` carries the docstring *"Gate sécurité warn-only"*;
`report.py:3638` is the literal `security_gate["status"] = "warn"`; `report.py:3643`
appends *"warn-only, rapport écrit"* to the warning.

The warning is surfaced at `report.py:3858-3860`, where the gate result is appended
to the run's warnings list.

The narrower question of *which* security rules are allowed to block at all is a
separate decision: [0007](0007-blocking-security-is-an-explicit-three-rule-allowlist.md).

Provenance: `git log -1 --format=%b a87c75e`.
