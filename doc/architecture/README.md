# Architecture — weekly-advisor-kit

Ce document décrit l'architecture **telle qu'implémentée** dans ce dépôt. C'est
une implémentation possible du contrat fonctionnel défini dans
[`doc/spec/`](../spec/README.md) : chaque choix technique décrit ici (langage,
moteur, découpage en modules, format des artefacts) est réversible, alors que le
contrat fonctionnel ne l'est pas. Une reconstruction sur une architecture
totalement différente reste conforme tant que la spécification est respectée.

## Vue d'ensemble

- **Moteur déterministe** : paquet Python `weekly-telemetry-aggregator`
  (`.opencode/plugins/weekly-advisor-engine/`). Il agrège les données de
  télémétrie, produit les artefacts JSON, le rapport Markdown/HTML et les codes
  retour 0 (complet) / 1 (partiel) / 2 (bloquant).
- **Plugin d'intégration** : `.opencode/plugins/weekly-advisor.ts` expose
  19 tools `weekly_*`. Dix-huit pilotent le moteur via la CLI du paquet
  (`uv run python -m weekly_telemetry_aggregator <sous-commande>`) ;
  `weekly_preflight` est une gate locale sans sous-commande moteur.
- **Orchestrateur** : l'agent `.opencode/agents/weekly-advisor/weekly-advisor.md`
  et la commande `/weekly-review` exécutent le pipeline complet sous forme de
  vagues de workers parallèles (DAG) puis assemblent le rapport.

## Composants

### CLI du moteur

Sous-commandes (table déclarative `_SUBCOMMANDS` dans
`weekly_telemetry_aggregator/cli.py`) :

| Étape | Sous-commandes |
| ----- | -------------- |
| Collecte | `run` |
| Veille | `releases`, `watch-distill`, `watch-context`, `watch-validate` |
| Audit | `audit-candidates`, `show-session` |
| Contrôle déclaratif / remédiation | `harness`, `harness-remediate` |
| Insights / curation | `insights`, `skill-curate` |
| Drafting | `draft-candidates`, `commit-draft` |
| Rapport | `report-prep`, `report-blocks-draft`, `report-assemble` |
| Divers | `self-cost`, `doctor` |

Les handlers référencés par la table sont résolus à l'exécution
(`globals()[func.__name__]`), ce qui préserve la substitution des handlers dans
les tests.

### Configuration

Configuration JSON unique (`weekly-telemetry-config.json`) lue par `config.py`.
Les vues historiques plates (`sources`, `storage`, `cost`, `curation`) restent
acceptées : `config.py` projette les deux formes vers un modèle interne (dataclasses)
sans jamais réécrire le fichier. La sentinelle `/path/to/...` non substituée rend
le préflight bloquant.

### Artefacts et runs

Chaque exécution écrit dans `<output_dir>/runs/<date>-<uuid8>/` et l'alias
stable `runs/current` pointe vers le run actif. Les artefacts JSON portent
`schema_version` ; leurs structures de tête sont documentées dans
[`schemas/`](schemas/README.md). Le rapport final est archivé dans le run dir et
recopié dans `reports/html/` (page interactive + historique daté + `latest`).

### Code retour

`0` complet, `1` partiel (avertissements, sources dégradées, audit sans
findings, commit de drafting en échec…), `2` bloquant (collecte défaillante,
diagnostic, contrôle déclaratif en erreur). En pipeline orchestré, la valeur
finale est propagée en trois endroits identiques : `summary.exit`,
`WEEKLY_REVIEW_RC` et la ligne `END … exit=` du tail.

## Orchestration (DAG)

Commande `/weekly-review` (coordinateur unique) :

1. **Gate doctor** : `weekly_doctor` rc 2 → arrêt immédiat.
2. **WAVE 1** (parallèle) :
   - **T** : `run` (collecte) puis `weekly-quality-audit` (audit des sessions).
   - **V** : `releases` → `watch-distill` → `watch-context` → `weekly-watch-review`
     → `watch-validate`.
   - **H** : `harness` → `harness-remediate`.
3. **JOIN** : fusion des rc (un rc=2 → arrêt sans rapport) ; le JOIN lit les
   artefacts produits, et n'utilise `weekly-summary-*.json` que pour la
   provenance/timings.
4. **WAVE 2** (parallèle) : **D** drafting (`draft-candidates`, `commit-draft`),
   **I** insights (`insights`), **C** cohérence (`weekly-coherence-review`).
5. **WAVE 2.5** : curation `skill-curate` (dry-run requis ; apply uniquement
   après validation humaine).
6. **TAIL** : `report-prep` → `report-blocks-draft` → prose → `report-assemble`
   → `self-cost`.

Règles des workers : briefing minimal-complet, un worker par sous-tâche, attente
bornée des branches V/H (poll du résumé, plafond 10 min), retry unique,
`weekly-timings-<date>.json` pour les durées, provenance
`start_time`/`elapsed_s`/`branch`/`run_dir`/`artifacts`.

## Veille écosystème

- Étape 2 : `releases` interroge les sources (dépôts suivis, listes de curation,
  pages web surveillées, sujets GitHub, flux RSS) → `weekly-ecosystem-<date>.json`.
- Étape 2.2 : `watch-distill` fusionne et filtre (score, screening sécurité,
  quotas) → `watch-candidates-<date>.json` + mémoire
  `<output_dir>/watch-memory.jsonl`. Exit 2 = étape désactivée ou écosystème
  absent → dégradation attendue, le flux aval retombe sur l'écosystème complet.
- Étape 2.5 : `watch-context` croise les fiches avec l'inventaire réel du
  worktree → `watch-candidates-enriched-<date>.json` (+ observations
  d'architecture, lecture seule).
- Étape 3.5 : le skill `weekly-watch-review` écrit les findings **bruts**
  `weekly-watch-findings-raw-<date>.json`.
- Étape 3.6 : `watch-validate` applique les coercitions d'état et la mémoire →
  `weekly-watch-findings-<date>.json` (annexe sécurité incluse).

## Providers multi-harnais

Le moteur lit les sessions de plusieurs harnais via un Protocol
`SessionProvider` (identifiant de harnais + méthodes de lecture). Le registre
(`providers/registry.py`) câble explicitement les implémentations
(`opencode.py` — base SQLite ; `claude_code.py` — transcripts JSONL ;
`copilot_vscode.py` — stockage de chat) et `build_providers` est fail-soft :
une source indisponible est ignorée avec un avertissement. Les identifiants de
session sont canoniques et namespacés (`<harnais>:<id>`) ; la déduplication
entre sources d'un même harnais applique « première source gagne ». Codex n'est
pas une source de télémétrie : c'est uniquement une cible de drafting.

## Coûts

Le coût facturé lu dans les données est la référence : il n'est jamais recalculé.
Quand un harnais n'enregistre pas de coût (Claude Code), un coût **estimé**
(tokens × taux, 5.0 $/Mtok par défaut, 9.0 pour OpenCode, 2.5 pour Copilot,
override `cost_rate_usd_per_mtok`) est produit dans `cost_estimates`, distinct et
jamais additionné au coût facturé.

## Diagnostic et mono-cible

`doctor` itère les providers résolus et retourne rc 2 si aucune source n'est
disponible. Le drafting est mono-cible : le harnais cible est résolu par override
de configuration, sinon par marqueurs du worktree
(`.claude` → claude-code, `.opencode` → opencode, `.github/prompts|skills` →
copilot-vscode, `.agents` → codex), avec priorité
claude-code > opencode > copilot-vscode > codex, sinon défaut OpenCode +
avertissement.

## Contrôle déclaratif et remédiation

`harness` exécute le scanner déclaratif en profils `strict` (politique seule) ou
`advisory` (politique + documentation, défaut), sur une projection temporaire à
base de copies réelles (zéro lien symbolique). La baseline
(`weekly-harness-baseline.json`) est créée au premier run puis réutilisée — elle
n'est réécrite que si les règles changent. La surface de remédiation est
résolue par `resolve_remediation_surface` (projection, portabilité ou combinée
selon les harnais présents). `harness-remediate` n'applique une proposition que
si la gate déterministe l'autorise (confiance élevée, règle explicitement
autorisée, remplacement unique, ≤ 1 fichier par run) ; les règles `security/*`
restent toujours bloquées.

## Qualité, tests et CI

- `pytest` (moteur) est la source de vérité ; `--collect-only` est réservé au
  diagnostic borné. Les comptes de tests affichés dans README/INSTALL sont
  vérifiés par `scripts/check-flow-docs.mjs` (avertissement, jamais bloquant).
- `ruff check` + `ruff format --check` sur le moteur.
- Tests de contrat Node (`scripts/tests/*.test.mjs`) : surface CLI ↔ plugin,
  ordre du pipeline, invariance de l'ancre, contrats de documents. Ils tournent
  en CI sur Linux (POSIX) et localement avec `node --test scripts/tests/*.test.mjs`.
- `check-flow-docs.mjs` vérifie 7 surfaces de cohérence docs ↔ code.

## Gouvernance

Le produit est **observation-only** : il documente, alerte et propose, mais
n'applique jamais de changement non validé. La dérive d'architecture est
signalée en finding `observation_only` avec action `recalibrate`, jamais en
alerte bloquante. La curation (`skill-curate`) est en dry-run par défaut ; une
exécution `apply` exige une validation humaine explicite, et la politique par
défaut n'autorise que l'archivage (jamais la suppression), avec protection
stricte des skills `origin=user`.

## Invariants

- Déterminisme à ancre et entrées identiques (tri stable, arrondis à 6 décimales).
- Fail-soft par défaut, partiel rc 1, fatal rc 2 : jamais d'écriture partielle
  silencieuse.
- Baseline capturée-jamais-réécrite ; mémoire veille append-only.
- Conversation des artefacts par `schema_version` ; artefacts écrits de façon
  atomique.
- Un commit par écriture de skill/command, ajout scopé au chemin cible.
- La fusion des vagues ne s'arrête que sur rc 2 ; les autres codes sont propagés
  au rapport.

## Annexes

- [Schémas des artefacts JSON](schemas/README.md)
- Diagrammes (HTML, source unique) : [`doc/diagrams/`](../diagrams/) —
  architecture, classes, séquences `weekly-run`, `doctor`, `commit-draft`.
