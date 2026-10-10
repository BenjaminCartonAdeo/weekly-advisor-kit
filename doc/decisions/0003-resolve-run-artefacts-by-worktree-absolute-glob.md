# 0003 — Resolve run artefacts by globbing the root, `runs/*/` and `runs/*/legacy/`, and always hand back absolute paths

- **Status:** proposed
- **Date:** 2026-09-14
- **Source:** commit `a87c75e` (squash-merge of PR #4)
- **Footprint:** `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/insights.py:1127-1153`, `weekly_telemetry_aggregator/util.py:37-46`

## Context

Artefacts used to sit flat in the output directory (`weekly-summary-<date>.json`).
They now live under `runs/<id>/`, and pre-v6.0.k artefacts were migrated into
`runs/<id>/legacy/`. `insights` computes week-over-week deltas by comparing the
current artefact against the previous one, and must find that previous artefact
without being told where it is.

Two failure modes had to be closed at once. A single `output_dir.glob(pattern)` sees
only the root, so every migrated run is invisible — and the week-over-week delta is
not lost loudly, it is silently absent. Separately, artefact paths handed back to the
agent were relative, and a relative path depends on the shell's current directory.

**Provenance note.** No commit in this repository carries the *title* describing this
change. The rationale is in the **body** of `a87c75e`, which is the squash-merge of
PR #4 and aggregates the squashed messages; the relevant line reads "absolute
worktree globs (fix P1 split-brain), weekly-summary pattern". `a87c75e` is the only
provenance for this decision and it is a body, not a subject line.

**Correction to the brief this record was drafted from.** The decision was described
as a "worktree-absolute glob". `rg -n 'worktree|absolute' insights.py` returns
**nothing** — there is no worktree concept in that module. What the code actually
does is the three-glob union in `_pattern_paths`, combined with `_abs()` forcing
absolute paths on the way out. This record names what the code does.

## Decision

Resolve a dated artefact by unioning three globs — root, `runs/*/`, and
`runs/*/legacy/` — and normalise every artefact path to absolute before returning it.

## Consequences

This buys artefact discovery that survives both the migration to run directories and
the migration of pre-v6.0.k artefacts, and it returns paths that are correct
regardless of the caller's working directory. The cost is three glob walks per
lookup instead of one, and a duplicated-code hazard: any future artefact location has
to be added to this tuple or it becomes invisible again.

The rejected alternative was the single root glob. It was rejected because it is
indistinguishable from correct — the run simply reports no previous week, which reads
as "no change" rather than "artifact not found". The docstring at `insights.py:1130`
names this as case C2.

A second rejected alternative was to have the caller pass the previous artefact path
explicitly. It was rejected because it moves a directory-layout detail into every
call site, and the caller does not know it.

## Evidence

The three globs are visible at `insights.py:1135-1141`:

```
rg -n '_pattern_paths' -A14 .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/insights.py
```

The absolute-path contract is at `util.py:37`, whose docstring records its own
origin — an incident where a relative path produced a ~20 minute loop because the
agent had to guess the working directory.

Provenance: `git log -1 --format=%b a87c75e`.
