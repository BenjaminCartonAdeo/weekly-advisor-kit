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
- **Plugin d'intégration** : `.opencode/plugins/weekly-advisor.ts` (point
  d'entrée) expose 21 tools `weekly_*`, déclarés dans
  `weekly-advisor/tool-registry.ts`. Dix-neuf pilotent le moteur via la CLI du
  paquet (`uv run python -m weekly_telemetry_aggregator <sous-commande>`) ;
  `weekly_preflight` et `weekly_report_contract` sont des gates locales sans
  appel CLI. La CLI compte en plus `debug-rule`, le playground read-only non
  exposé comme tool.
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
| Divers | `self-cost`, `doctor`, `debug-rule` |

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
copilot-cli, `.agents` → codex), avec priorité
claude-code > opencode > copilot-cli > codex, sinon défaut OpenCode +
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

## Classification déterministe et règles déclaratives

`classifiers.py` (P6) classe chaque session **sans LLM** : intent
(priorité planning > debug > review > explore > implementation), session
spec-driven (preuves regex : chemin `spec|prd|plan…`, mots modaux, listes
markdown, `/plan`), relecture production (gap > 30 s après un edit/write,
`% relu`, `review-unmeasurable:<harness>` si timestamps indisponibles — jamais
0 mensonger) et maturité de prompt (5 dimensions, grades A≥80 / B≥65 / C≥50 /
D≥40 / F<40). Le bloc **additif** `session_classifications` de
`weekly-summary` (schema_version inchangé) alimente `audit-candidates`
(priorités `code-non-relu`, `maturity-F`, `non-spec` coûteuse — plafond
`audit_max_sessions` inchangé) et les catégories du skill
`weekly-quality-audit` (`non-spec-driven`, `code-non-relu`,
`low-maturity-prompt`).

Le moteur de règles déclaratives (P1) vit dans `rules/` (règles `.md`
versionnées : frontmatter YAML + sections Description / When Triggered / How
to Improve / Examples + blocs `` ```detect `` / `` ```test `` exécutés par
pytest), `rule_loader.py` (mini-YAML déterministe maison, `extends` par
préfixe `+` avec fusion clé à clé) et `rule_pipeline.py` (DSL
`scan/match/aggregate/check` évalué par **AST whitelisté, sans `eval`**,
placeholders `{{count}}`/`{{pct}}`/`{{extra.*}}`/`{{thresholds.*}}`, findings
uniformes triés par id, tests de règles embarqués via `run_rule_test`).

**Consommation en production.** Les règles ne sont pas seulement rejouables à la
demande : elles sont évaluées à chaque run par l'étape `insights`. Il n'y a ni
nouvelle sous-commande ni nouvelle étape de pipeline — `compute()` reste pur,
`run()` possède l'E/S — et tout le câblage tient dans
`_production_rule_results(...)` (`insights.py`), qui renvoie
`(alerts, maintenance_findings, extra)`. Ordre des opérations : `build_context(...)`
→ fusion de `extra["ignored"]` depuis `cfg.ignored_findings` →
`load_rules(overrides=...)` via `_rule_set` (mémoïsé par lot de seuils) →
`evaluate_rules(...)` → routage de chaque finding selon son `sink`. Trois
détecteurs Python ont été supprimés au profit de `rules/*.md` :
`_daily_spike_alerts`, `_agent_loop_findings`, `_architecture_drift_finding`.

`rule_context.py` est le seul pont entre les artefacts du run et le moteur : il
construit le contexte d'évaluation et expose **six scopes, tous des listes** —
`user_prompt_repeats`, `session_classifications`, `findings`
(`weekly-quality-findings-<date>.json` ; absent ou malformé ⇒ `[]`, jamais une
exception), `tool_argument_loops` (le dict `tool_argument_fingerprints` imbriqué,
aplati en une ligne par outil portant `tool`, `repeats` = plus gros bucket et
`task_threshold`, 3 pour `task` / 8 pour les autres par défaut),
`daily_spikes` (jours à coût positif de `daily_totals`, scorés contre la baseline
des runs récents via un `raw_z` **non borné** — le plafond est appliqué en aval)
et `architecture_drift` (toujours exactement une ligne, jamais une liste vide :
`changed_fields`, `drift_runs`, `drift_threshold`, `observation`).

Les quatre règles qui portent un `sink` explicite :

| Règle | Scope | Déclencheur | Sévérité | Sink |
| ----- | ----- | ----------- | -------- | ---- |
| `agent-loop` | `tool_argument_loops` | `repeats >= task_threshold` pour `task`, `>= loop_min_repeats` sinon | medium | findings |
| `daily-spike` | `daily_spikes` | `raw_z >= z_min` | medium | alerts |
| `architecture-drift` | `architecture_drift` | `drift_runs >= drift_threshold` et `changed_fields` non vide | low | findings |
| `prompt-loop` | `user_prompt_repeats` | `count >= prompt_min_repeats` | low | findings |

`prompt-loop` est un ajout sans équivalent Python — aucun détecteur de prompts
utilisateur répétés n'existait. Les trois autres règles livrées
(`code-non-relu`, `prompt-maturity-f`, `anti-learning`) visent les scopes
`session_classifications` et `findings` et retombent sur le sink par défaut.
`sink: findings` (défaut) append le finding à `maintenance.findings` ; `sink: alerts`
le rend dans la forme d'alerte historique. Le rendu est déclaré par la règle
elle-même dans son bloc frontmatter `alert:` (`signal`/`threshold`/`cap`/`rows`/
`row_signal`/`row_day`, vocabulaire fermé), publié dans `details["alert"]` ; le nom
d'alerte affiché est l'id de la règle souligné, jamais un alias Python maintenu à
part. Pour `daily-spike`, un finding produit **une alerte par jour qui spike**,
chacune avec son propre `day`, son propre `observed` et sa propre note, sémantique
du détecteur supprimé.

Les seuils publics ne sont pas câblés de la même façon. Celui qui lit
`thresholds.*` atteint sa règle par `load_rules(overrides=...)`, qui fusionne
profondément sur le `thresholds` de la règle après résolution de `extends` :
`loop_min_repeats`, `loop_task_min_repeats`, `architecture_drift_runs` et
`daily_spike_z_min` passent tous par là, le `.md` restant propriétaire des
défauts. `prompt-loop.prompt_min_repeats` n'a en revanche **aucun** bouton
public : son seuil vient du seul `rules/prompt-loop.md`, le réglage d'agrégat
`user_prompt_repeat_min` ne servant qu'à grouper les prompts à destination du
résumé sans jamais atteindre l'évaluation. `cfg.ignored_findings` est honoré
sous ses deux formes habituelles (`"category:target"` ou `"target"` nu), mais
avec une granularité différente : le filtre s'applique par règle, avant
évaluation — ignorer une cible d'une règle désactive la règle entière, alors que
le filtrage Python par entrée ne retirait que l'entrée correspondante.

Ce qui reste délibérément en Python, c'est le cœur numérique : le z-score robuste
(médiane + MAD) est inexprimable dans le DSL, qui n'a ni médiane ni MAD.
`rule_context.py` réimporte donc `_robust_z_scores`, `_architecture_observation`
et `_architecture_drift` depuis `insights`, et les trois constantes de seuil sont
réexportées de part et d'autre. Seule la *politique* a migré vers les `.md`, les
*statistiques* non.

Un angle mort assumé : une règle qui référence une clé `extra` absente lève —
`extra.*` n'a donc rien à faire dans `match`/`check`. L'échec reste **isolé par
règle** : `evaluate_rules` capture toute exception, émet un unique constat
`rule-error` (sévérité `low`) et poursuit l'évaluation des autres règles ;
l'étape `insights` ne s'arrête pas.

`debug-rule` (P2) est un playground **lecture seule** sur le run actif
(`resolve_active_run_dir`) : `evaluate <rule-id|expression>`, `fields`
(catalogue typé + arités DSL), `distributions [filtre]` (top valeurs,
min/max, sample size) ; rc 2 si le résumé est absent/invalide ou l'entrée
inconnue. Il reste le chemin d'exploration : la production évalue les mêmes
règles via `_production_rule_results`, pas via `debug-rule`.

## Qualité, tests et CI

- `pytest` (moteur) est la source de vérité ; `--collect-only` est réservé au
  diagnostic borné. Les comptes de tests affichés dans README/INSTALL sont
  vérifiés par `scripts/check-flow-docs.mjs` (avertissement, jamais bloquant).
- `ruff check` + `ruff format --check` sur le moteur.
- Tests de contrat Node (`scripts/tests/*.test.mjs`) : surface CLI ↔ plugin,
  ordre du pipeline, invariance de l'ancre, contrats de documents. Ils tournent
  en CI sur Linux (POSIX) et localement avec `node --test scripts/tests/*.test.mjs`.
- `check-flow-docs.mjs` vérifie 9 surfaces de cohérence docs ↔ code.

## Gouvernance

Le produit est **observation-only** : il documente, alerte et propose, mais
n'applique jamais de changement non validé. Les règles bloquantes de sécurité
et leurs paraphrases proviennent d'une **source unique** (`security_rules.py`),
partagée par le rendu Markdown et le rendu HTML. La dérive d'architecture est
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
- Diagrammes (HTML source, exports SVG/PNG) : [`doc/diagrams/`](../diagrams/) —
  architecture, classes, séquences `weekly-run`, `doctor`, `commit-draft` (chaque `*.html` a son `*.svg` et `*.png` générés à l'identique, voir `doc/diagrams/README.md`).
