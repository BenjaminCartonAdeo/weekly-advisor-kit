# weekly-advisor-kit

Revue hebdomadaire automatisée de vos agents de code : analyse de la télémétrie locale, veille écosystème, audit des sessions coûteuses et rapport HTML interactif, avec un moteur 100 % déterministe, zéro LLM pour les chiffres.

[![CI](https://github.com/BenjaminCartonAdeo/weekly-advisor-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/BenjaminCartonAdeo/weekly-advisor-kit/actions/workflows/ci.yml)
[![tests 842](https://img.shields.io/badge/tests-842-brightgreen)](.opencode/plugins/weekly-advisor-engine)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Schéma d'architecture : [`doc/diagrams/architecture.html`](doc/diagrams/architecture.html).

## Ce que fait le kit

Chaque lundi, le kit produit un rapport unique qui répond à vos questions : combien vous avez dépensé, ce qui a coûté cher et pourquoi, quels skills et commands sont inutilisés ou redondants, quoi surveiller dans l'écosystème, et quelles corrections peuvent être auto-rédigées (proposées, jamais imposées).

Le cœur est 100 % déterministe : le moteur Python lit directement la télémétrie locale des harnais actifs (base SQLite OpenCode, transcripts JSONL Claude Code, sessions Copilot VS Code) et produit des JSON reproductibles, sans SDK ni serveur. Le LLM n'intervient que sur les étapes qualitatives, encadrées par des skills dédiés et des contrats anti-hallucination. Le pipeline complet (8 étapes, de la télémétrie au rapport final) est décrit dans la [spécification](doc/spec/README.md).

## Fonctionnalités clés

| Fonctionnalité | Ce que vous obtenez |
|---|---|
| Multi-harnais | Télémétrie OpenCode, Claude Code et Copilot VS Code via `session_sources`, extensible par source ; **Codex n'est jamais un provider de télémétrie** — cible de drafting seule (`.agents/`) |
| Chiffres déterministes | Moteur Python pur, zéro LLM sur les données : coûts, tokens, cache, outliers, prompts répétés par fingerprint (O(n)) |
| Classification déterministe | Intent, spec-driven, relecture production (gap > 30 s), maturité de prompt (grades A–F) par session, sans LLM ; alimente l'audit |
| Règles d'audit déclaratives | Règles `.md` versionnées + DSL `scan/match/aggregate/check`, tests embarqués, playground `debug-rule` en lecture seule |
| Coûts estimés | Estimation par session avec surcharge `cost_rate_usd_per_mtok` par source, alertes budget semaine/mois |
| Audit qualité | Sessions candidates auditées par skill dédié, constats archivés avec baseline pour mesurer la dérive |
| Veille marché | Distillation hebdomadaire (~30 fiches scorées) confrontée à votre environnement, avec mémoire inter-run |
| Auto-drafting mono-cible | Drafts skills/commands ciblés vers le harnais du projet, gate de portabilité avant commit (erreur → refus) |
| Curation WAVE 2.5 | Manifeste déterministe des actions de curation/TTL en **dry-run** ; gate politique : `apply=true` n'exécute que les **archives** (déplacement idempotent, jamais de suppression), les autres actions restent des propositions |
| Gouvernance observation-only | Dérive d'architecture/configuration **observée** (projection watch-context + finding insights `architecture-drift`) : jamais bloquante, ne déclenche ni curation ni application |
| Rapport web final | HTML autonome (dashboard KPI, filtres, dark mode) ouvert automatiquement en fin de run, plus archive MD |

## Installation

Prérequis : `uv`, Python ≥ 3.11, `opencode` V1 ≥ 1.18.29 (ou V2 `2.*`) avec un modèle authentifié (`opencode auth login`), `harness-eval` ≥ 7.9.0 pour l'étape 5. Détails et alternatives : [`INSTALL.md`](INSTALL.md).

```sh
git clone https://github.com/BenjaminCartonAdeo/weekly-advisor-kit.git && cd weekly-advisor-kit
uv sync --project .opencode/plugins/weekly-advisor-engine --extra dev
```

> **Piège connu** : `--extra dev` est obligatoire. Sans lui, `pytest` et `ruff` ne sont pas installés et la validation locale échoue.

## Compatibilité OpenCode V1/V2

Le kit tourne sur les deux lignes d'opencode :

| Ligne | Versions supportées | Contrainte |
|---|---|---|
| V1 | `>= 1.18.29 < 2.0.0` | déclarée dans `.opencode/package.json` (`@opencode-ai/plugin`) ; le lockfile résout `1.18.32` |
| V2 | `2.*` (alpha) | aucun SDK V2 en dépendance — l'adaptateur V2 déclare ses types de contexte **structurellement** |

Le plancher V1 existe pour une raison : le point d'entrée dual repose sur le
contrat de module **objet** `{ id, server, setup }` du loader d'opencode,
supporté à partir de la 1.18.29. Les versions `1.18.25` à `1.18.28` ne sont
**pas** supportées par ce kit.

**Adaptateurs paresseux et version-spécifiques.** `.opencode/plugins/weekly-advisor.ts`
expose exactement `{ id: "weekly-advisor", server, setup }`, sans aucune logique
d'outil ni import d'exécution du SDK. `server` charge par `import()` dynamique
`weekly-advisor/adapters/v1.ts` — le seul module avec un import d'exécution de
`@opencode-ai/plugin` — et `setup` charge `weekly-advisor/adapters/v2.ts`
(types V2 structurels, aucun import SDK). Le cœur neutre (`types.ts`, `paths.ts`,
`preflight.ts`, `runtime.ts`, `tool-registry.ts`) porte toute la logique et reste
la source de vérité unique des 19 outils / 18 sous-commandes CLI. Seul
l'adaptateur correspondant à l'hôte qui charge le plugin est jamais importé :
aucune résolution croisée V1/V2, et un échec d'import **remonte** — il n'existe
aucun repli silencieux.

**Différences de comportement V1 ↔ V2** (ce que les opérateurs doivent savoir) :

| Aspect | V1 | V2 |
|---|---|---|
| Hooks | `command.execute.before` (filtré sur `weekly-review`) + `chat.message` (agent `weekly-advisor`) | pas d'équivalent du hook pré-commande V1 ; le pré-flight passe par `ctx.session.hook("prompt")` filtré strictement sur `event.prompt.agents` contenant `weekly-advisor` |
| Pré-flight | fail-closed, message gelé `weekly_preflight rc=3 — …` | identique (même texte gelé) |
| Résultat d'outil | `string` | `{ content }` |
| Schéma d'outil | builders SDK `tool.schema.*` | JSON Schema dans `input` (`object` / `properties` / `required` / `additionalProperties: false`) |
| Chargement | dynamique depuis `server` | dynamique depuis `setup` |

**Smoke harness.** `scripts/smoke-dual-runtime.mjs` valide les deux runtimes
sans hôte : env `OPENCODE_V1_BIN`, `OPENCODE_V2_BIN`, `V1_EXPECTED_VERSION`,
`V2_EXPECTED_VERSION`, `WEEKLY_KIT_ROOT` ; flags `--strict` (défaut) /
`--non-strict` / `--quiet`. Codes de sortie : `0` tout passe, `1` un check
échoue en strict, `3` erreur de configuration/environnement — en strict, le
harness refuse de démarrer (exit 3) si un binaire manque, si les deux binaires
sont identiques, ou si une version attendue manque. En `--non-strict`, un check
impossible imprime un verdict `SKIPPED` explicite et ne produit **jamais** de
`PASSED` silencieux. Rapport écrit dans `reports/smoke-dual-runtime-report.json`.

Le CI (`.github/workflows/ci.yml`) exécute toujours le self-test `--non-strict`
(il ne peut pas échouer et ne prouve rien sur de vrais binaires V1/V2) ; l'étape
stricte de compatibilité est **opt-in** — elle ne tourne que lorsque les
binaires sont provisionnés, et la garde vit dans le shell (`shell: bash`) car un
`if:` d'étape GitHub Actions ne voit pas l'`env:` d'étape. **Le CI ne prouve
donc pas la compatibilité V1/V2.** État de validation réelle : un run strict
avec de vrais binaires a été exécuté le 2026-09-26 (V1 `1.19.5`, V2 `2.4.1`) —
identification des binaires, point d'entrée et contrainte de package passés ;
le check registre a alors rapporté un comptage (0, 0) et le run est sorti en
`1`, pas en `0`. Le registre charge aujourd'hui bien 19/18 via le même loader
Node 24 — relancez le smoke strict pour un verdict à jour.

**Distribution.** Local-only : le plugin vit dans `.opencode/plugins/` et est
auto-découvert par opencode — pas de publication npm (`private: true`), pas de
dépendance npm V2 au runtime (l'adaptateur V2 est structurel), et aucun Bun
requis au runtime.

## Quickstart

1. **Adaptez la configuration** : `project_root` et `output_dir` (chemins absolus) dans [`.opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json`](.opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json).
2. **Vérifiez le poste** : `opencode run --agent weekly-advisor "Exécute weekly_doctor et donne son verdict"`. Sortie attendue : un verdict base, config et binaires, sans erreur.
3. **Lancez la revue** : `opencode run --agent weekly-advisor "Lance la revue hebdomadaire"`. Comptez environ 8 à 12 minutes (orchestration parallèle en waves) ; à la fin le rapport HTML s'ouvre dans votre navigateur.

Résultat visible : `reports/html/weekly-report-latest.html` (rapport HTML autonome) et `reports/runs/current/weekly-report-<date>.md` (archive MD).

## Usage

**Revue manuelle complète** : après le quickstart, relancez la commande de l'étape 3 chaque fois que vous voulez un état frais. Le run est orchestré en **waves parallèles de subagents** (branches télémétrie, veille et harnais exécutées en parallèle, puis drafting/insights/cohérence), écrit un JSON daté par étape dans le répertoire du run, puis assemble le rapport. Un récapitulatif des durées par branche est écrit dans `weekly-timings-<date>.json`. Un run partiel renvoie le code 1 (warnings tolérés), un run fatal le code 2, sans rapport.

**Planning hebdomadaire (cron)** :

```cron
0 6 * * 1 opencode run --port 4096 --agent weekly-advisor --model <votre-modèle> --dir /chemin/du/kit "Lance la revue hebdomadaire" >> /var/log/weekly-advisor.log 2>&1
```

Le rapport final est le signal du cron : s'il est absent, quelque chose s'est mal passé. Pour un cron headless, désactivez l'ouverture navigateur avec `WEEKLY_NO_BROWSER=1` ou `open_browser: false` dans la config. Détails et heartbeat recommandé : [`INSTALL.md`](INSTALL.md) §2.7.

## Configuration

Les clés principales vivent dans [`weekly-telemetry-config.json`](.opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json) :

| Clé | Rôle | Défaut |
|---|---|---|
| `session_sources` | Sources de télémétrie actives (opencode, claude-code, copilot-cli…) | opencode |
| `draft_targets` | Harnais cible du drafting (auto par marqueurs projet, ou liste) | auto |
| `output_dir` | Répertoire des runs et archives | `~/opencode-weekly-reviews` |
| `cost_rate_usd_per_mtok` | Taux de coût par source (surcharge) | par modèle |
| `lookback_days` | Fenêtre analysée par run | 7 |

Le détail des clés, des seuils et des invariants est dans la [spécification](doc/spec/README.md) et [`INSTALL.md`](INSTALL.md) §2.3. En interne, la configuration est exposée en **vues groupées** (sources, stockage, coûts, curation) — vues en lecture seule **rétro-compatibles** : le fichier JSON garde ses clés plates historiques, aucune migration requise.

### Contrat d'exécution et provenance

Chaque artefact JSON est transporté par fichier dans `runs/current/` (jamais par
`argv` ou par duplication du payload dans le contexte agent). Les artefacts portent
les champs de provenance applicables : `anchor`, `generated_at`, `source`/`sources`,
`run_dir` et, pour les synthèses, `artifact_inputs` (présence et pointeur de chaque
entrée). Le fichier daté est la source de vérité ; le résumé ne conserve que des
pointeurs et statuts.

Le dispatch est borné : le coordinateur seul délègue, au plus 3 workers T/V/H,
`K ≤ audit_max_sessions` workers d'audit, puis 3 workers D/I/C ; aucun fan-out de
worker. Chaque worker dispose de 10 minutes, d'un passage par étape et de 3 tours
de diagnostic maximum. Une sortie vide n'autorise qu'une retry, puis `rc=1`.

Règles bloquantes exactes : `rc=2` d'un worker (ou crash critique), absence de la
skill primaire F6 (`skills_loaded.ok=false` sur A/D/C), ou gate non exécutable
(timeout/crash/sortie illisible) stoppe le run sans rapport. Timeout/silence de
worker et warnings ordinaires restent `rc=1` fail-soft. Les statuts observables
sont `missing`, `truncated`, `timeout` et le statut original `worker_status` ; ils
restent dans `weekly-timings-<date>.json`.

### Sources Copilot (CLI)

Désactivées par défaut (défaut opencode seul, rétrocompatible). Chemin
auto-détecté : CLI `~/.copilot` (Linux) / `%USERPROFILE%\.copilot` (Windows,
surcharge `COPILOT_CONFIG_DIR`). Exemple `session_sources` (opencode +
CLI avec vrais tokens estimés à 2,5 $/Mtok, recherche FTS) : [`INSTALL.md`](INSTALL.md)
§2.3.1.

## Contributing

Contributions bienvenues, en particulier : nouveaux providers de harnais, règles de portabilité, diagrammes et corrections de documentation.

Validation locale (depuis le dossier moteur `.opencode/plugins/weekly-advisor-engine`) :

```sh
uv run python -m pytest -q    # 842 tests
uv run ruff check .           # lint
uv run ruff format --check .  # format
```

**Source de vérité des tests :** le résultat de l'exécution `pytest` ci-dessus
(toutes les tests passent) fait foi. Une collecte séparée (`uv run python -m
pytest --collect-only -q`) est un **diagnostic explicite** uniquement : elle aide à
expliquer une différence de décompte, mais ne remplace pas l'exécution des tests.
Un écart entre le nombre affiché ici et le nombre collecté est **warning-only** ;
il ne bloque pas le run. Les contrats de gate (contrats de flux, lint/format et
gates de portabilité) restent bloquants lorsqu'ils échouent.

Gate docs ↔ code (depuis la racine du repo, node requis) :

```sh
node scripts/check-flow-docs.mjs   # G1 : comptes de tests cohérents + contrats de flux
```

Le CI (`.github/workflows/ci.yml`) répète lint, format, 842 tests et packaging sur Ubuntu et Windows, puis exécute les tests de contrat node (Ubuntu) et la gate G1. Commits en [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`…). La spécification vit dans [`doc/spec/`](doc/spec/README.md), l'architecture dans [`doc/architecture/`](doc/architecture/README.md), l'installation pas à pas dans [`INSTALL.md`](INSTALL.md).

## Documentation

| Document | Contenu |
|---|---|
| [`doc/spec/`](doc/spec/README.md) | Spécification fonctionnelle : le contrat complet du pipeline (8 chapitres) |
| [`doc/architecture/`](doc/architecture/README.md) | Architecture de l'implémentation de référence + schémas JSON |
| [`INSTALL.md`](INSTALL.md) | Installation pas à pas, cron, mise à jour, dépannage |
| [`INSTALL_PROMPT.md`](INSTALL_PROMPT.md) | Installation pilotée par agent (à coller dans une session OpenCode) |
| [`doc/diagrams/`](doc/diagrams/) | Schémas d'architecture et séquences |

## Limites

- Le moteur lit les tables internes de la base SQLite d'opencode : ce n'est pas une API publique, une mise à jour majeure peut rompre la collecte jusqu'à la mise à jour du kit. `weekly_doctor` diagnostique.
- La veille dépend d'APIs publiques (npm, GitHub, registre MCP) : rate-limits possibles, warnings tolérés, le run continue.

## Licence

[MIT](LICENSE) © Benjamin CARTON
