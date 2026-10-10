# 0005 — Report rendering is deterministic, and a missing section 4 produces a labelled placeholder, never a silent gap

- **Status:** proposed
- **Date:** 2026-09-14
- **Source:** commit `a87c75e` (squash-merge of PR #4)
- **Footprint:** `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/report.py:1-9`, `:3657`, `:3665`, `:3681`, `:3697`

## Context

The weekly report is assembled in two passes. `report-prep` renders every section
that comes from JSON artefacts and git log — no LLM — and leaves
`<!-- QUALITY_BLOCK -->` where the agent's qualitative prose will go.
`report-assemble` injects the agent's block into that marker and writes the final
file.

The failure mode is specific. If the agent's prose block is absent, or is rejected by
the anti-hallucination validator, the naive implementation drops the block and emits
a report that is one section short. The reader sees a complete-looking document with
a hole in it, and has no way to tell that a section was *supposed* to be there.

A weekly report with a silent gap is worse than no report: it is trusted anyway.

## Decision

Make every non-LLM section deterministic, and when section 4 cannot be filled from
validated agent prose, emit an explicit placeholder that **names which fallback
fired**. Never omit the section.

## Consequences

This buys a report that is always structurally complete and self-describing: a reader
who sees `auto_draft_fallback` in section 4 knows immediately that the qualitative
pass did not contribute, and the gates file records the same verdict.

The cost is that a degraded report is still produced and still written to disk, which
means the pipeline can return `rc=1` while emitting a deliverable. That is a
deliberate trade: the alternative was to withhold the report entirely, and a withheld
report is indistinguishable from a crashed run.

Four distinct fallback statuses exist, and they are distinguishable precisely so the
cause is recoverable from the artefact alone:

| Location | Status text | Cause |
| --- | --- | --- |
| `report.py:3657` | `non disponible (placeholder)` | no block, nothing to fall back to |
| `report.py:3665` | `brouillon automatique (bloc LLM rejeté : <v>) — auto_draft_fallback` | one validation violation |
| `report.py:3681` | `brouillon automatique (bloc LLM rejeté : <v1> ; <v2>) — auto_draft_fallback` | several violations |
| `report.py:3697` | `brouillon automatique (report-blocks-draft) — auto_draft_fallback` | agent prose absent, deterministic draft used |

The rejected alternative was to leave the section empty. It was rejected because an
absent section and a section that was never attempted are the same artefact, and only
one of those is a bug.

A second rejected alternative was to fail the run and write no report. It was
rejected because the deterministic sections are still valid information; discarding
them to signal a missing agent block destroys real content to communicate a process
problem.

## Evidence

The contract is stated in the module docstring at `report.py:1-9` — *"Missing blocks
=> explicit placeholder, never a silent gap."*

List the four fallbacks and their trigger conditions:

```
rg -n 'auto_draft_fallback|non disponible \(placeholder\)' \
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/report.py
```

`report.py:3706` is the warning appended when section 4 falls back, and `report.py:3796`
is where the gates file records `"validated"` versus `"auto_draft_fallback"`.

Coverage: `tests/test_report_gates.py` and `tests/test_blocks_check.py` in the plugin
directory. Provenance: `git log -1 --format=%b a87c75e`.
