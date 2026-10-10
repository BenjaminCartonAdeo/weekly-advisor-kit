# 0004 — The `advisory` harness profile is a deliberate superset: policy surfaces plus documentation surfaces

- **Status:** proposed
- **Date:** 2026-09-12
- **Source:** commit `e3d402b` (squashed into PR #4, squash `a87c75e`)
- **Footprint:** `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/config.py:34-53`

## Context

`harness-eval` runs against a named include-profile. `strict` covers the surfaces that
*are* policy: `AGENTS.md`, agents, commands, plugin sources, `opencode.json`. That is
the obvious reading of the feature — scan the configuration.

It is also the wrong reading. A skill is loaded by its `SKILL.md`, but its
behaviour is shaped by `references/`, `rules/` and `assets/` too. An earlier `advisory`
profile enumerated exactly `SKILL.md` and `references/`, which left every
`rules/*.md` outside the scan. An audit dated 2026-10-07 counted **97 files**
classified as "unscoped".

`advisory` therefore exists as a profile that is *not* a subset of `strict` in spirit
— it is `strict` plus everything that can influence skill loading and guidance.

## Decision

Define `advisory` as a deliberate superset: the policy surfaces, plus the whole of the
skill documentation tree (`skills/**/*.md`, `examples/**`, `assets/**`, `skills/**/*.json`),
and state that intent in a comment above the tuple so the next reader does not "fix"
it back into a subset.

## Consequences

This buys coverage of the surfaces that actually change agent behaviour. Once
`advisory` was widened, the "unscoped" count in the audit of 2026-10-07 went from
**97** to **0**.

The cost is scan surface: `advisory` walks strictly more files than `strict`, so it is
slower and produces more components per run. It is also the reason `advisory` and
`strict` cannot be reasoned about as a simple ordering — a future editor who treats
`advisory` as "strict, but looser matching" will re-narrow it.

The rejected alternative was to keep the old `SKILL.md` + `references/` pair and treat
`rules/` as out of scope. It was rejected on the audit measurement, not on taste:
97 unscanned files.

A second rejected alternative was to make the two profiles share a base tuple and
express `advisory` as a delta. It was rejected because it makes the superset
relationship invisible at the point of reading, which is precisely the failure being
fixed.

## Evidence

Read the intent comment and both profiles side by side:

```
sed -n '23,54p' .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/config.py
```

Lines 34-36 carry the comment *"Advisory is deliberately a superset: policy surfaces
plus the documentation surfaces that influence skill loading and guidance"*, and lines
44-46 record the rejected narrower pair with its 97-file audit figure.

The follow-up that measured the result is `396e9a3`; see [0009](0009-iterate-all-harness-inspection-sections.md).

Provenance: `git log -1 --format=%b e3d402b`.
