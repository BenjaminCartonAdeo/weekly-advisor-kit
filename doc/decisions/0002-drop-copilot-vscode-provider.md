# 0002 — Drop the `copilot-vscode` provider and keep `copilot-cli` as the only Copilot source

- **Status:** proposed
- **Date:** 2026-09-08
- **Source:** commit `7cec504` (folded into PR #4, squash `a87c75e`)
- **Footprint:** `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/providers/`, `weekly_telemetry_aggregator/registry.py`

## Context

Two providers claimed to read GitHub Copilot telemetry: `copilot_vscode` and
`copilot_cli`. They did not overlap — they read entirely different stores.

`copilot_vscode.py` was a 1112-line "god provider" reading
`workspaceStorage/state.vscdb`. Measured against a real machine, that database held
**zero** tokens. `copilot-cli` reads `session-store.db` with a `SQL LIMIT 500`.

Keeping both meant maintaining a thousand lines of code against a store that
produced nothing, while the one store that actually held sessions was a single SQL
query.

**Provenance note, stated because it matters.** This commit message is not unique in
the repository. Six commits carry the near-identical title *"remove copilot-vscode
keep copilot-cli"*: `1e12007`, `16d3c56` (branch `feat/harness_ventilation`),
`3d9c0a7` (`feat/copilot_compat`), `7cec504` (branch `review/oe-7cec504`), and
`d2b40a9`, `ed9f7bd` (`feat/improve-auto-laerning`). This record cites **`7cec504`**
because it is the only one whose actual diff matches its own body: the body claims
"17 files 463+/1786-" and `git show --stat 7cec504` reports exactly 17 files,
463 insertions, 1786 deletions. (`1e12007` is a different and later commit with an
English title — *"chore(providers): remove copilot_vscode provider"*, 2026-09-16,
1112 deletions.) The repository squash-merges, so none of these six is an ancestor
of the mainline; their content reached it through the PR #4 squash `a87c75e`.

## Decision

Delete `copilot_vscode.py` outright, drop its registry entry, and keep `copilot-cli`
as the single Copilot source; rename `copilot-vscode` to `copilot-cli` everywhere in
the docs so the documentation matches the code.

## Consequences

This buys roughly 1112 lines of dead provider code deleted, one registry entry
instead of two, and documentation that stops describing a provider that no longer
exists — the diff renamed it across `INSTALL.md`, `doc/architecture/README.md`,
`doc/spec/04-pipeline.md`, `doc/spec/08-criteres-acceptation.md` and the HTML
diagrams.

The cost is that anyone whose Copilot usage lives *only* in `state.vscdb` now reports
zero Copilot sessions. The commit accepts that cost on the measurement, not on
preference: the store was empty on the machine that was checked.

The rejected alternative was to keep both providers and treat `copilot_vscode` as a
latent source for VS Code users. It was rejected because the store it reads contains
no tokens, so the code could not be distinguished from correct — it could only be
distinguished from *exercised*.

A second rejected alternative was to delete the provider but leave the
`copilot-vscode` spelling in the docs. The commit body explicitly rejects this on the
principle that the code is the authority, which is why the rename was carried through
the documentation in the same commit rather than deferred.

## Evidence

Confirm the provider is gone and unregistered:

```
rg --hidden -n 'copilot_vscode|copilot-vscode' .opencode/plugins/weekly-advisor-engine/
```

The commit body records the expected result as zero matches, plus `pytest 98
passed / 1 skipped`.

Provenance of this specific record: `git show --stat 7cec504` (17 files,
463+/1786-, matching the body) and `git log -1 --format=%b 7cec504`.
