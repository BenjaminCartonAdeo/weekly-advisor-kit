# 0008 — Exclude generated artefacts and lockfiles from the harness scan

- **Status:** proposed
- **Date:** 2026-10-07
- **Source:** commit `539c6fc`
- **Footprint:** `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/config.py:56-81`

## Context

The harness scanner treats every file it reaches as part of the agent configuration
subject to review. Three classes of file reached it that are not source:

- `graphify-out/**` is a generated knowledge-graph cache. It is large, it changes on
  every run, and it inflates the `lint_coverage` denominator — the ratio stops
  meaning "share of my configuration that was reviewed".
- Lockfiles (`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `bun.lock`) contain
  long integrity-hash strings. Those strings trip `mcp-tool-poisoning`, which is one
  of the three rules allowed to **block** the report (see
  [0007](0007-blocking-security-is-an-explicit-three-rule-allowlist.md)).
- `skills/_archive/**` holds skills that were deliberately retired. Inventorying them
  as live configuration is what coherence finding C13 flags.

The `service.json` exclusion belongs to the lockfile group: MCP manifests hash their
own contents for the same reason.

## Decision

Extend `DEFAULT_HARNESS_EXCLUDE_PATTERNS` to cover `graphify-out/**`, the four
lockfile formats plus `service.json`, and `skills/_archive/**`, each with a comment
recording which failure it prevents.

## Consequences

This buys a `lint_coverage` denominator that counts real configuration, and it removes
a class of false positive that could otherwise have blocked the report through
`mcp-tool-poisoning`.

The cost is that the exclusions are now load-bearing: a future file that *is* real
configuration but matches one of these globs becomes invisible, and invisibility is
silent. This is why each entry carries its reason in a comment rather than being
added bare.

The rejected alternative was to keep scanning and filter the false positives at the
finding level. It was rejected because the lockfile hashes would still be read,
parsed and counted on every run, and the `lint_coverage` denominator would still be
wrong — the filter would fix the finding, not the metric.

A second rejected alternative was to exclude by file extension only. It was rejected
because `graphify-out` and `skills/_archive` need path-scoped globs: the same
extensions occur inside real skill documentation.

## Evidence

```
sed -n '56,85p' .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/config.py
```

`config.py:71` is `.opencode/graphify-out/**`, `config.py:74` is
`**/package-lock.json`, `config.py:81` is `.opencode/skills/_archive/**`. Each carries
an inline comment naming the failure it prevents.

Note that `config.py:56-59` already excludes `.opencode/plugins/weekly-advisor-engine/**`
— the scanner does not scan its own implementation. The three entries added here
follow that same principle.

Provenance: `git show 539c6fc`.
