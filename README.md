# weekly-advisor-kit

[![CI](https://github.com/BenjaminCartonAdeo/weekly-advisor-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/BenjaminCartonAdeo/weekly-advisor-kit/actions/workflows/ci.yml)

Revue hebdomadaire automatisée de votre usage d'OpenCode : chaque semaine, le kit
analyse votre télémétrie locale, surveille l'écosystème des plugins/skills, audite
vos sessions coûteuses et produit **un rapport unique** `weekly-report-<date>.md`.

Chaque lundi, il vous dit : combien vous avez dépensé, ce qui a coûté cher et pourquoi,
quels skills/commands sont inutilisés ou redondants, quoi surveiller dans l'écosystème,
et quelles corrections peuvent être auto-rédigées (proposées, jamais imposées).

Le kit tient dans **un seul dossier** `.opencode/` : un agent dédié, 5 skills
qualitatifs, 2 commands, un **plugin enveloppe** (13 tools `weekly_*`) et un
**moteur Python** déterministe (`weekly-telemetry-aggregator`).

**Spécification** : [`opencode-weekly-advisor`](opencode-weekly-advisor) (le contrat complet, 8 parties)
**Installation pas à pas** : [`INSTALL.md`](INSTALL.md)

## Ce que fait le kit

Le cœur est **100 % déterministe, zéro LLM** : le moteur Python lit directement la
base SQLite locale d'OpenCode (pas de SDK, pas de serveur) et produit des JSON
reproductibles. Le LLM n'intervient que sur les étapes qualitatives, encadrées par
des skills dédiés et des contrats anti-hallucination.

| Étape | Contenu | Exécuté par |
|---|---|---|
| 1 · Télémétrie | coûts, tokens, cache, modèles, sessions top/outliers, usage tools/skills/commands, prompts répétés, subagents | `weekly_run` (déterministe) |
| 2 · Veille écosystème | npm, topics GitHub, registre MCP, releases OpenCode, repos/listes/RSS suivis | `weekly_releases` (déterministe) |
| 3 · Audit qualitatif | sessions candidates (coût, outliers, boucles, cache, prompts répétés) → constats archivés | skill `weekly-quality-audit` |
| 3.5 · Veille critique | marché × environnement × constats → recommandations adopt / improve / ignore | skill `weekly-watch-review` |
| 4 · Auto-drafting | candidats skills/commands → rédaction + commit sécurisé et scoped | skill `weekly-drafting` + `weekly_commit_draft` |
| 5 · Lint | `harness-eval` sur `.opencode/` (version épinglée) | `weekly_harness` (déterministe) |
| 6 · Insights | deltas vs semaine précédente, alertes budget/cache/spikes, maintenance R1-R4 | `weekly_insights` (déterministe) |
| 6.5 · Cohérence | état déclaratif (skills/agents) vs usage réel | skill `weekly-coherence-review` |
| 7 · Rapport | sections déterministes + prose LLM validée → **`weekly-report-<date>.md`** | `weekly_report_prep` / `weekly_report_blocks_draft` / `weekly_report_assemble` |
| 8 · Coût propre | coût de la session du pipeline lui-même (annexe du rapport) | `weekly_self_cost` (déterministe) |

Le rapport final est **le signal** : le cron (ou vous) l'attend chaque semaine — s'il
est absent, quelque chose s'est mal passé. Codes de sortie : `0` complet, `1` partiel
(warnings tolérés), `2` fatal (stoppe sans rapport). En cas de doute sur le poste :
`weekly_doctor` diagnostique base, config et binaires.

## Quickstart

Prérequis : `opencode` ≥ 1.18 avec un modèle authentifié (`opencode auth login`), `uv` —
détails et alternatives : [`INSTALL.md`](INSTALL.md).
Optionnel : `harness-eval` (étape 5 — lint de `.opencode/`) et `gh` (repos privés de la
veille) — les étapes concernées dégradent proprement (warnings) en leur absence.

```sh
git clone https://github.com/BenjaminCartonAdeo/weekly-advisor-kit.git && cd weekly-advisor-kit
uv sync --project .opencode/plugins/weekly-advisor-engine --all-extras
# 1 fichier à adapter : project_root + output_dir (chemins absolus)
$EDITOR .opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json
opencode run --agent weekly-advisor "Lance la revue hebdomadaire"
```

- Comptez 10-30 min pour un run complet (télémétrie, réseau et lint compris) ; le rapport est dans
  `<output_dir>/weekly-report-<date>.md`
- Première fois ? Remplacez la dernière commande par
  `opencode run --agent weekly-advisor "Exécute weekly_doctor et donne son verdict"` —
  il valide base, config et binaires avant le premier run
- Modèle : celui par défaut d'opencode, ou `--model <votre-modèle>` — jamais en dur dans le kit
- Les placeholders `/path/to/...` de la config sont volontaires : tant qu'ils ne sont
  pas adaptés, `weekly_doctor` échoue proprement au lieu de lancer un run sur un mauvais chemin
- **Fenêtre du run** : par défaut `lookback_days` de la config (7 j). Besoin d'une autre
  fenêtre ? Dites-le en toutes lettres (« revue des 3 dernières semaines ») — l'agent
  déduit `N semaines → N×7` et passe `--lookback-days` en override du run, **la config
  n'est jamais réécrite** (v6.0.b)

## Cron (rappel)

```cron
0 6 * * 1 opencode run --port 4096 --agent weekly-advisor --model <votre-modèle> --dir /chemin/du/kit "Lance la revue hebdomadaire" >> /var/log/weekly-advisor.log 2>&1
```

Rien d'autre sur PATH : le plugin résout python et `harness-eval`. Le rapport final
`<output_dir>/weekly-report-<date>.md` est le **signal** du cron (alerte si absent).
Détails et heartbeat recommandé : [`INSTALL.md`](INSTALL.md) §2.7.

## Structure

```
.opencode/
├── agents/weekly-advisor/weekly-advisor.md   ← orchestration : ordre figé, invariants, bash: deny
├── skills/weekly-*/SKILL.md                  ← 5 étapes qualitatives (chargées à la demande)
├── commands/weekly-review.md / weekly-report.md
└── plugins/
    ├── weekly-advisor.ts                     ← plugin enveloppe : 13 tools weekly_* (chemins dérivés)
    └── weekly-advisor-engine/                ← moteur Python (package, config, tests — 177 tests)
```

## Documentation

| Document | Contenu |
|---|---|
| [`opencode-weekly-advisor`](opencode-weekly-advisor) | La spec complète (8 parties) — le contrat du pipeline : orchestration, télémétrie, veille, audit, drafting, lint, insights, rapport |
| [`INSTALL.md`](INSTALL.md) | Installation pas à pas : prérequis, config, validation, cron, mise à jour, dépannage |
| [`README.md`](README.md) | Ce fichier — vue d'ensemble |

## Limites & compatibilité

- **Schéma SQLite d'opencode** : le moteur lit directement les tables internes
  (`session_v2`/`part` ou `session`/`event`), avec pins de schéma vérifiés au run
  (tables/colonnes requises, compteur de migrations). Ce n'est **pas une API publique** :
  une mise à jour majeure d'opencode peut rompre la collecte télémétrique jusqu'à la mise
  à jour du kit — `weekly_doctor` diagnostique base, config et binaires.
- **Veille (étape 2)** : dépend d'APIs publiques (npm, GitHub, registre MCP) — rate-limits
  possibles, warnings par source tolérés, le run continue.
- **`harness-eval` épinglé** (7.9.0) : toute dérive de version est signalée par le doctor.

## Développement

```sh
# le projet uv (pyproject + uv.lock) vit dans le dossier moteur
cd .opencode/plugins/weekly-advisor-engine
uv run pytest -q             # 171 tests
uv run ruff check .          # lint
uv run ruff format --check . # format
```

CI GitHub Actions (`.github/workflows/ci.yml`) : install lockfile `--frozen`, lint,
format, 171 tests, packaging et syntaxe du plugin TS — vérifiés sur chaque push/PR.
