# 0001 — Enrich the copilot-cli session title from `workspace.yaml`, with `checkpoints/index.md` as fallback only

- **Status:** proposed
- **Date:** 2026-09-08
- **Source:** PR #7 (squash `ae5decc`), originating commit `a153e50`
- **Footprint:** `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/providers/implementations/copilot_cli.py:81`, `:252-283`

## Context

copilot-cli stores its per-session metadata in SQLite (`session-store.db`), but the
human-readable title lives outside it. Two files carry it: `workspace.yaml` and
`checkpoints/index.md`.

The pre-existing `_state_enrichment` read only the `cwd` key out of `workspace.yaml`
(`_WS_YAML_KEYS` = `cwd|workspace|directory|path`). The title keys were never
matched, so `title` stayed `None` whenever `sessions.summary` was `NULL` *and*
`checkpoints/index.md` was empty. Sessions then entered the weekly report
untitled, which is exactly the session a reader most needs a title to recognise.

The commit body states the cause directly: `workspace.yaml` carries
`name:Java compilation fix` but `_state_enrichment` read only `cwd`, so the title
stayed `None` when `sessions.summary` was `NULL` and `index.md` was empty.

## Decision

Parse `name`, `title` or `summary` out of `workspace.yaml` into a dedicated
`_WS_YAML_TITLE_KEYS` regex and fill `title` from whichever appears first; fall back
to a markdown heading in `checkpoints/index.md` only when no title was found.

## Consequences

This buys a title for the majority of copilot-cli sessions that previously had none,
at the cost of one extra regex and one extra file read per session. The value is
truncated to 80 characters and both readers are fail-soft: an unreadable
`workspace.yaml` or `index.md` emits a warning and yields no title rather than
raising.

The rejected alternative was to keep reading `cwd` alone and depend on
`checkpoints/index.md` for every title. That was the status quo and it failed in
exactly the common case — an empty `index.md` left the session untitled.

The second rejected alternative was to break out of the line scan on the first key
match. An earlier draft did, and it misses a title that appears *after* `directory`
on a later line; the loop therefore continues until both `directory` and `title` are
populated.

A third option — parsing `workspace.yaml` with a real YAML library — was rejected as
overkill for matching two flat keys, and as a new dependency.

## Evidence

Run `rg -n '_WS_YAML_TITLE_KEYS|_enrich_from_workspace|_enrich_from_index' \
.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/providers/implementations/copilot_cli.py`
to see both readers and their key regexes.

The fallback ordering is pinned at `copilot_cli.py:274`, where `_enrich_from_index`
returns immediately if `"title" in out` — that single guard *is* the "fallback only"
rule.

Behavioural coverage: `tests/test_provider_copilot_cli.py` in the same plugin
directory. Provenance: `git show a153e50`.
