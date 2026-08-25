# Architecture du kit — weekly-advisor-kit

Vue d'ensemble technique du kit : moteur, multi-harnais, coûts, drafting,
lint et remédiation. Le contrat détaillé reste la spec
[`spec-opencode-weekly-advisor`](spec-opencode-weekly-advisor) ; ce document décrit
l'architecture telle qu'implémentée et les invariants à préserver.

## Périmètre

| Document | Rôle |
|---|---|
| `README.md` | Usage, étapes du pipeline, quickstart |
| `INSTALL.md` / `INSTALL_PROMPT.md` | Installation pas à pas (humain / agent) |
| `ARCHITECTURE.md` | Structure technique et invariants (ce document) |
| `spec-opencode-weekly-advisor` | Spécification fonctionnelle, source de vérité contractuelle |

## Moteur et orchestration

- Moteur Python déterministe `weekly-telemetry-aggregator` dans
  `.opencode/plugins/weekly-advisor-engine/`, zéro LLM sur les étapes chiffrées.
- Plugin enveloppe OpenCode : 17 tools `weekly_*` (`weekly_run`, `weekly_harness`,
  `weekly_doctor`, `weekly_commit_draft`, etc.) qui pilotent le moteur.
- Sorties JSON reproductibles dans `<output_dir>/runs/<date>-<uuid8>/`, alias
  stable `runs/current/`. Codes de sortie : `0` complet, `1` partiel, `2` fatal.
- L'exécution du run est **orchestrée en waves parallèles de subagents** (spec v6.1,
  design `doc/2026-08-25-parallel-orchestration-design.md`) : la session principale
  devient un coordinateur léger (gate, dispatch, join, tail). Le moteur Python et le
  plugin enveloppe restent inchangés — seule la couche d'orchestration agent évolue.

## Orchestration du run — DAG en waves

La session principale (`/weekly-review`, agent `weekly-advisor`) agit comme un
**coordinateur léger** : gate, dispatch en parallèle, join, tail. Elle n'exécute plus
elle-même les étapes longues — chaque branche indépendante est confiée à un
**worker subagent unique**.

```
/weekly-review (agent weekly-advisor = coordinateur + gestionnaire de contexte)
│
├─ Étape 0 doctor (gate ; rc=2 → STOP sans rapport)          [session principale]
│
├─ WAVE 1 — 3 subagents lancés en parallèle
│   ├─ T : weekly_run → poll achèvement → skill weekly-quality-audit
│   ├─ V : weekly_releases → watch_distill → watch_context
│   │      → skill weekly-watch-review → watch_validate
│   └─ H : weekly_harness → skill harness-remediation
│
├─ JOIN — fusion des résultats + codes sortie                 [session principale]
│
├─ WAVE 2 — activée par défaut (optionnelle)
│   ├─ D : skill weekly-drafting (seul worker autorisé à committer)
│   ├─ I : weekly_insights
│   └─ C : skill weekly-coherence-review
│
└─ TAIL — report_prep → blocks_draft → prose → assemble → self_cost
                                                             [session principale]
```

- **Rôle du coordinateur** : chaque worker reçoit un **briefing minimal-complet**
  (steps ordonnés de sa branche, chemin du répertoire de run, invariants de branche,
  contrat de retour JSON obligatoire). Au join, l'orchestrateur fusionne les contrats
  en une synthèse narrative courte qui seule alimente la wave 2 et le tail — les JSON
  sur disque restent la source de vérité unique.
- **Worker subagent unique** : un subagent `weekly-advisor-worker` (mode subagent)
  n'exécute que sa branche ; il ne connaît ni les autres branches ni la logique de
  merge. Les dépendances séquentielles internes à une branche (ex. V : 2.2 → 2.5)
  restent gérées par le worker lui-même.
- **Attente V/H sur le summary de T** : avant leur premier write, les workers V et H
  attendent que `runs/current/` expose `weekly-summary-<date>.json` (poll read/glob,
  **plafond 10 min** ; dépassement → warning fail-soft, la branche tente quand même
  en écriture différée si possible).
- **Merge des codes sortie (JOIN)** : un seul rc=2 parmi les workers (ou crash sans
  résultat sur étape critique) → **STOP sans rapport** — le rapport absent reste le
  signal du cron. Sinon, les warnings des branches sont agrégés et passés au tail :
  succès partiel préservé, exit 1. Worker silencieux ou timeout → rc=1 + warning,
  le run continue (fail-soft).
- **Instrumentation** : chaque worker retourne `elapsed_s` et des timings par step
  dans son contrat ; au join, l'orchestrateur écrit **`weekly-timings-<date>.json`**
  (`{branch: {step: ms}}` + durées wave/tail). Nouvel artefact écrit par l'agent, la
  liste des fichiers agent-writable est étendue en conséquence.

## Veille écosystème (étapes 2 → 3.6)

Chaîne déterministe de la **branche V (wave 1)**, encadrant l'unique passe LLM de la
branche (3.5) — exécutée par le worker V, qui gère seul l'ordre séquentiel interne :

- **Étape 2 — `releases`** : collecte réseau (npm, topics, registre MCP, RSS,
  repos/listes/radar suivis) → `weekly-ecosystem-<date>.json`.
- **Étape 2.2 — `watch-distill`** (séquentiel après 2) : fusion multi-sources,
  screening sécurité local (blocked/suspicious/clean), scoring déterministe,
  quotas `{new, improvable, resurfaced}` → top-N fiches dans
  `watch-candidates-<date>.json`. Les items **blocked ne sont jamais soumis au
  LLM** : ils vivent dans une annexe séparée du snapshot. Mémoire inter-run
  `<output_dir>/watch-memory.jsonl` (`watch_distill.memory_file`) : statuts des
  semaines passées, dédup et résurrection bornée. Écosystème absent ou étape
  désactivée (`watch_distill.enabled=false`) → exit 2, le flux aval retombe sur
  l'écosystème complet (fallback legacy de la skill 3.5).
- **Étape 2.5 — `watch-context`** : crosswalk marché/existant ; consomme les
  candidats s'ils existent et produit alors
  `watch-candidates-enriched-<date>.json` (fiches × état local + bande résiduelle).
- **Étape 3.5 — skill `weekly-watch-review`** (LLM) : lit les fiches enrichies +
  digest mémoire + findings coûteux ; filet B phase 0 conditionnelle (fiches <
  `min_candidates` ET résiduel non vide) ; écrit le brut
  `weekly-watch-findings-raw-<date>.json`, limité aux catégories
  `install-new` / `improve-existing` / `ignore`.
- **Étape 3.6 — `watch-validate`** : validation déterministe du brut — coercitions
  d'état (`absent`→install-new, `declared`→verify-existing, `observed`→improve-existing,
  `unknown`→verify-existing), coercition de
  cible locale hors inventaire → `install-new` (racine projet = config
  `project_root`), fiche `suspicious` sans mention de risque citable → sévérité
  `high` ; writer mémoire post-validation ; annexe sécurité recopiée depuis le
  snapshot candidats (clé top-level réservée, inusurpable par le brut).

## Multi-harnais : providers de sessions

Le kit lit les sessions de plusieurs harnais (OpenCode, Claude Code, Copilot
VS Code) via une couche d'abstraction :

- **Protocol `SessionProvider`** (`providers/base.py`) : expose l'attribut
  `harness` et 11 méthodes (liste de sessions, agrégats, transcript, `close()`).
  Chaque session exposée porte un **id canonique namespacé** `<harness>:<id>`
  (`opencode:`, `claude-code:`, `copilot-vscode:`) et le tag `harness`.
- **Registry auto-discovery** (`providers/registry.py`) : scan `pkgutil` de
  `providers/implementations/` ; chaque module expose `PROVIDER_TYPE: str` et
  une factory `build_provider(source_cfg, cfg) -> SessionProvider | None`.
  Type inconnu, source indisponible ou échec d'initialisation →
  avertissement + skip, jamais de crash (fail-soft).
- **Implementations** :
  - `opencode.py` — base SQLite locale d'OpenCode.
  - `claude_code.py` — transcripts JSONL `~/.claude/projects/<cwd-mungé>/<sessionId>.jsonl`
    (un fichier = une session ; munging du répertoire projet : séparateurs/points → tirets).
  - `copilot_vscode.py` — source Copilot VS Code.
- **`build_providers(cfg)`** construit les providers des sources actives de
  `cfg.session_sources` et les fusionne. Si aucune source n'est active, repli
  rétrocompatible sur la base OpenCode locale via `detect_db` (contrat
  `DataSourceError` conservé, utilisé par `self-cost`).
- **Dédoublonnage multi-sources** : ids canoniques dédoublonnés entre sources
  du même harnais — première entrée `session_sources` gagne, doublons ignorés
  avec `UserWarning` récap tracée dans `summary.warnings`.

## Coûts estimés multi-harnais

- Champ first-class top-level `WeeklySummary.cost_estimates: dict[str, float] | None`,
  calculé pour les sessions sans coût enregistré (taux × tokens). Clé absente
  du payload si `None` ; lecture rétro-compatible d'un ancien artefact où le
  champ vivait sous `summary.selection` conservée.
- Taux par harnais, en USD par million de tokens : défaut `5.0`, OpenCode `9.0`,
  Copilot VS Code `2.5`.
- Surcharge par source : clé extra `cost_rate_usd_per_mtok` dans l'entrée
  `session_sources` (valeur numérique ; illisible → défaut conservé).

## Doctor

- Itère génériquement sur les providers, plus de chemin OpenCode codé en dur.
- Affiche par source : `[name] OK (chemin, migrations=n)`.
- `exit 2` si aucune source de sessions n'est disponible.
- Affiche aussi les cibles de drafting (voir mono-cible) et la surface de
  remédiation 5.5.

## Mono-cible : cibles de drafting

Le drafting (étape 4) cible un harnais unique, pas seulement `.opencode/` :

- Marqueurs détectés à la racine du projet : `.claude/`, `.opencode/`,
  `.github/prompts/` + `.github/skills/`, `.agents/`.
- Priorité par défaut : claude > opencode > copilot > codex.
- Défaut sans marqueur : opencode + warning.
- Override par config (`draft_targets.mode = override`, `targets=[...]`).
- `targets=[]` → legacy, toutes cibles.

## Étape 5 : lint harness

- **Projection étendue** : les fichiers `.opencode/` (et cibles du harnais
  détecté) sont copiés dans une projection temporaire allowlistée avant
  `harness-eval` : zéro symlink, « réel-gagne » en cas de collision, orphelins
  tracés dans `harness_include.unscoped_files`.
- **Baseline findings** : `weekly-harness-baseline.json` à la racine de
  `output_dir`, créée au premier run (`status=created`), relue telle quelle
  aux runs suivants (`reused`), jamais réécrite.
- Matrice de décision 5.5 `resolve_remediation_surface` (`harness_scope.py`) :
  `projection` (cible `.opencode/` seule), `portability` (autre harnais, mapping
  requis), `combined`.

## Qualité et portabilité

- **`portability.yaml` kit-shipped** : règles de mapping custom, placées sous
  `custom/portability/*` (préfixe `custom/` obligatoire). Couverture
  skills-only de `harness-eval` ≥ 7.10.1.
- **Gate `skill-verify` dans `weekly_commit_draft`** : exécutée avec cwd =
  worktree ; findings lus depuis le JSON (`findings[].details[]`) ;
  `error` → refus du commit (fix manuel), `warning` → note, binaire
  `harness-eval` absent → gate ignorée avec note ⚠.

## Invariants

- Le moteur reste 100 % déterministe ; le LLM n'intervient que sur les étapes
  qualitatives (skills, prose).
- Fail-soft : une source de sessions ne casse jamais le run.
- Baseline findings jamais réécrite ; remédiation jamais appliquée sans gate.
- Commits d'auto-drafting : un commit par écriture, scoped à `.opencode/` du
  worktree.
- Orchestration en waves : l'invariant « jamais de dispatch en subagent » est levé
  (spec v6.1) ; le merge rc au join reste le seul point de décision d'arrêt, et le
  moteur/plugin restent inchangés (la parallélisation ne touche que l'agent).