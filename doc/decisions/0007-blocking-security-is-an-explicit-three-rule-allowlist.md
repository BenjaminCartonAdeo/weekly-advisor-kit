# 0007 — Blocking is an explicit three-rule allowlist, matched after stripping a `security/` prefix

- **Status:** proposed
- **Date:** 2026-09-12
- **Source:** commit `e3d402b` (squashed into PR #4, squash `a87c75e`)
- **Footprint:** `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/security_rules.py:13-24`, `weekly_telemetry_aggregator/report.py:2444-2462`

## Context

[0006](0006-security-findings-are-warn-only-in-report-assemble) makes the security
gate warn-only. That decision raises the obvious follow-up: if nothing blocks, does
*nothing* ever block?

The naive rule is "any finding whose severity is critical blocks". That couples the
gate to the scanner's severity taxonomy, which is version-dependent and owned by an
external tool. A scanner upgrade that promotes a new finding to `critical` silently
changes this repository's blocking behaviour.

A second problem surfaced at the same time. Nested findings arrive prefixed —
`security/mcp-tool-poisoning` — while the allowlist holds the bare identifier
`mcp-tool-poisoning`. A literal set membership test misses every prefixed finding, so
nested criticals were reported and then not counted as blocking.

**Provenance note.** This decision and
[0004](0004-advisory-harness-profile-is-a-deliberate-superset.md) come from the
*same commit*, `e3d402b`, whose subject is "apply advisory allowlist superset and
prefixed blocking security rules". It carries two separable choices and is cited by
both records because that is what it did.

## Decision

Declare `BLOCKING_RULES_ORDERED` as an explicit, ordered, three-entry frozenset, and
match it through `_is_blocking_security_rule`, which lowercases, trims, and strips a
leading `security/` prefix before the membership test.

## Consequences

This buys three properties. The blocking set is readable in one place and does not
drift with a scanner upgrade. The matching is prefix-tolerant, so a nested finding and
a top-level one are treated identically. And `cfg.harness_ignored_rules` can still
subtract from the set, so an operator can silence a rule without patching code.

The three blocking rules are `mcp-tool-poisoning`, `memory-write-unscoped` and
`unbounded-delegation`. Each also carries a human-readable paraphrase in
`security_rules.py:20-24` — the finding is something a person must act on, not a
tuning knob.

The rejected alternative was severity-based blocking ("critical blocks"). It was
rejected because severity is assigned upstream by a tool this repository does not
control; binding a gate to it makes the gate's behaviour a function of someone else's
release cadence.

The second rejected alternative was bare string membership without prefix stripping.
It was rejected on the observed bug: it silently classified prefixed nested findings
as non-blocking.

## Evidence

```
rg -n 'BLOCKING_RULES' -A8 \
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/security_rules.py
```

`_is_blocking_security_rule` is at `report.py:2447`; line 2450 is the
`removeprefix("security/")` that the commit added. `_BLOCKING_SECURITY_RULES =
BLOCKING_RULES` at `report.py:2444` is the alias that keeps the older name working.

`cfg.harness_ignored_rules` is applied inside `_assemble_security_gate`
(`report.py:3618`) and in `_critical_security_findings` (`report.py:1180`), so an
ignored rule cannot trigger the gate.

Provenance: `git log -1 --format=%b e3d402b` — "_is_blocking_security_rule normalizes
security/ prefix so nested critical findings block with rc=2; nested prefixed test
added."
