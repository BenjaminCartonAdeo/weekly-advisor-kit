# 0009 — Iterate every harness inspection section instead of a hardcoded list of section names

- **Status:** proposed
- **Date:** 2026-10-07
- **Source:** commit `396e9a3`
- **Footprint:** `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/util.py:105-117`, `weekly_telemetry_aggregator/harness_scope.py:511`

## Context

A harness digest groups its components under `inspection.<section>`. The section
names are **version-dependent**: harness-eval 7.9.0 emits `skill` and `command`,
older digests emitted `claude_md`. The reader code enumerated a hardcoded tuple:

```
(command, claude_md, uncategorized)
```

That tuple is simultaneously too narrow and out of date. Every `skill` finding was
dropped on the floor. The audit of 2026-10-07 measured the consequence: digest
findings went from **0** to **102** once the sections were enumerated rather than
listed.

A count of zero is the dangerous part. A hardcoded list that has drifted still runs,
still returns a well-formed result, and reports that nothing was found.

## Decision

Replace the hardcoded section tuple with `iter_inspection_sections`, which walks the
`inspection` mapping and yields every section whose value is a list, skipping a
declared set of non-component keys.

## Consequences

This buys forward compatibility with the scanner's section taxonomy: a new section
name is picked up with no code change, and an old digest is still read correctly.
After the change the unscoped-file count reported by the same commit went from 82
to 0.

The cost is that the set of scanned sections is no longer visible at the call site —
it is a property of the input. A section named like a component but holding metadata
would be scanned as a component. This is bounded by `_NON_COMPONENT_SECTIONS`, and
that bound is the thing to watch.

The rejected alternative was to update the tuple to the current version
(`command, claude_md, uncategorized, skill`). It was rejected because it re-creates
the same bug at the next scanner release, and the failure mode is a silent zero
rather than an error.

A second rejected alternative was to read the section list from the scanner's own
schema. That would be correct but adds a schema dependency to a path that is
currently a plain mapping walk, and the mapping walk is already version-proof.

## Evidence

```
rg -n 'iter_inspection_sections' -A14 \
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/util.py
```

The docstring at `util.py:108-111` records the version dependency and the audit
finding by name. Call sites are `harness_scope.py:511` and `util.py:195`.

Related: the same commit widened the `advisory` profile itself — see
[0004](0004-advisory-harness-profile-is-a-deliberate-superset.md) — and added the
generated-artefact exclusions tracked in
[0008](0008-exclude-generated-artefacts-and-lockfiles-from-harness-scan.md).

Provenance: `git show 396e9a3` — the commit body records the digest finding count
rising from 0 to 102 after the change. The commit message itself is written in
French; this record paraphrases it rather than quoting it, per the language rule in
this directory.
