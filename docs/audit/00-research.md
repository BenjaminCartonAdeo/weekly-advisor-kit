# Audit — Recherche & inventaire (repowise)

Index repowise: commit `bab07da37587`, age 33 j. Date audit: 2026-11-14.

## 1. Périmètre & stack

- Repo: `weekly-advisor-kit` (monorepo, 2 packages: `.opencode`, `scripts`).
- Langages: Python 103 fichiers, JS 17, TS 8, Shell 3. Docs/config: MD 51, JSON 16, HTML 5, YAML 2, TOML 1.
- Total: 206 fichiers, 82 924 lignes, 3 716 symboles, 2 modules logiques.
- Stack: Python 3.13+, Jinja2, pytest/pytest-cov, ruff; plugin TS weekly-advisor.
- Entrées: `weekly_telemetry_aggregator/__main__.py`, `cli.py`, `main.py`.

## 2. Structure des modules

| Module | Fichiers | Rôle |
|---|---|---|
| `.opencode/` | 135 | Plugin weekly-advisor (TS) + moteur Python weekly-advisor-engine |
| `scripts/` | 20 | Tooling (check-flow-docs, smoke-dual-runtime, sync, install) |
| `doc/`, `docs/` | — | Documentation, spec, plans |
| `.harness-eval/` | — | Règles custom scanner |

Sous-arborescence moteur: `weekly_telemetry_aggregator/` (aggregator/, providers/, releases/, rules/) + `tests/`.

## 3. Santé globale (repowise)

KPIs (scope all, counts everything):
- Fichiers analysés: 131 (production 69).
- Santé moyenne: 7.56 / 10 — bande `good`.
- Hotspot health: 6.36.
- Maintainability moy: 8.13 — Performance moy: 9.58 — Structure moy: 1.47 — History moy: 0.97 — Production moy: 5.97.
- Pires performers:
  - `report.py` — 2.05 (CCN max 35, nesting 6, 3257 NLOC)
  - `main.py` — 2.20 (CCN 22, nesting 4, 765 NLOC)
  - `harness_scope.py` — 2.35 (CCN 16, nesting 4, 777 NLOC)
  - `sqlite_reader.py` — 2.82
  - `watch_context.py` — 3.26
- Pire test: `tests/test_harness_scope.py` — 6.12.
- Fichiers sans historique git: 75 (sur 218 indexés).

Findings ouverts:
- Defect: 306 (top_findings) ; dimension_counts defect=412 incl. tests.
- Maintainability: 58.
- Performance: 28 opportunités (10 agrégées `performance_opportunities_total`).
- Top findings total: 392 (prod) + 56 (tests).

## 4. Hotspots (churn)

- Hotspot count global: 32 (`.opencode` 31, `scripts` 1).
- Top churn (90 j):
  - `main.py` — 35 commits, percentile 0.99
  - `tests/test_main.py` — 27 commits, 0.98
  - `releases/__init__.py` — 13 commits, 0.98
  - `aggregator/__init__.py` — 12 commits, 0.97
  - `cli.py` — 24 commits, 0.96
  - `scripts/check-flow-docs.mjs` — 8 commits, 0.84
- Bus factor moyen: 1.0; 127 fichiers avec bus factor 1 (silo Benjamin CARTON 96.2%).
- Silos de connaissance (owner_pct >= 0.9): main.py, test_main.py, config.py, cli.py, report.py, runtime.ts, test_report.py, releases/__init__.py, aggregator/__init__.py, insights.py, harness_scope.py, watch_context.py, watch_distill.py, test_aggregator.py.

## 5. Code mort

4 findings (confidence 0.4, tier medium), 412 lignes reclaimables, 0 safe-to-delete:
- `.opencode/plugins/weekly-advisor/adapters/v2.ts` — fichier injoignable (368 lignes) + exports inutilisés `setup` (40), `V2ToolResult` (3).
- `.opencode/plugins/weekly-advisor/paths.ts` — export inutilisé `ENGINE_REL` (1 ligne).
- Note: 3 `unused_internal` masqués (faux positifs TS/Python).

## 6. Top findings (échantillon haute priorité)

Defect (health_impact décroissant):
- `scripts/smoke-dual-runtime.mjs::checkRegistry` — nested_complexity high (nesting 5, CCN 16, cog 40).
- `cli_skill_curate.py::_merge_lifecycle_metadata` — complex_conditional high (5 opérateurs).
- `scripts/check-flow-docs.mjs::checkEnvelope` — complex_method critical (CCN 26).
- `html_report.py::_parse_security_head` — CCN 15.
- `skill_surface.py::scan_skill_records` — CCN 16.
- `util.py::_strip_jsonc` — CCN 15.
- Nombreux nested_complexity medium sur insights, rule_context, sqlite_reader, watch_*, providers.

Maintainability: 58 findings (low_cohesion, brain_method, primitive_obsession, error_handling).

Performance: 28 opportunités (membership_test en boucle, I/O en boucle, sync I/O hot path, concat string en boucle, read unbounded).

## 7. Tests & couverture

- Tests Python: ~40 fichiers dans `tests/`; JS tests: 13 dans `scripts/tests/`.
- Fichiers prod sans test dédié: `__init__.py`, `__main__.py`, `cli_commands.py`, `cli_debug.py`, `security_rules.py`, `providers/implementations/__init__.py`.
- Repowise: pas de rapport de couverture ingéré → `test_map` indisponible; recommandation d'ajouter un coverage report.

## 8. Actions suggérées par repowise

- Hebdo: tidy code shape — 36 findings dans 14 fichiers (priorité `report.py` 16 findings).
- Trimestriel: ingérer coverage report; revoir 18 décisions proposées (aucune acceptée).
- Indisponibles: fix_events (pas de commits fix), security (plan Pro), test_map.

## 9. Livrables pour les autres cellules

- Cibles prioritaires de refactor: `report.py`, `main.py`, `harness_scope.py`, `sqlite_reader.py`, `watch_context.py`, `insights.py`.
- Fichiers à auditer sécurité/qualité: TS plugin (`.opencode/plugins/weekly-advisor/*.ts`), scripts mjs.
- Code mort candidat (review avant suppression): `adapters/v2.ts`.
- Risque de silo: tout changement sur les 6 fichiers pires performers doit être accompagné de tests (bus factor 1).
