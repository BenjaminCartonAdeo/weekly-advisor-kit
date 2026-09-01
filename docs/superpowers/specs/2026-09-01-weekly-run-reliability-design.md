# Design — Fiabilité du run hebdomadaire (correctifs approuvés)

Date : 2026-09-01
Statut : design validé — phase documentation (aucun code modifié)
Sources : `doc/audit-2026-08-27.md`, log `~/log/weekly/weekly-advisor-2026-09-01T11-36-01+02-00.log` (exit=3), agents `weekly-advisor.md` / `weekly-advisor-worker.md`, commande `weekly-review.md`, plugin `weekly-advisor.ts`, moteur `weekly_telemetry_aggregator` (v0.4.1).

---

## 1. Contexte — correctifs approuvés

Six correctifs issus de l'audit du run hebdomadaire, validés pour implémentation :

| # | Correctif | Origine (constat) |
|---|---|---|
| F1 | Launcher / worktree pre-check | Incident 15:47 (audit §3.2) : lancement manuel hors worktree → run à vide ; log 09-01 : exit=3 après boot agent. Le pre-check vit dans l'agent, pas dans le launcher. |
| F2 | WAVE 2.5 manifest / détail rapport | `report_assemble` ne gate que l'**absence** de `skill-curate-<date>.json` (P0) ; le détail du manifest (dry-run/apply, décisions, statuts) n'est jamais rendu. |
| F3 | Graphify out-of-band explicite | `graphify_summary.py` existe (lecture seule, n'invoque jamais Graphify) mais rien dans le pipeline ne le consomme ; l'état du graphe n'est pas rapporté. |
| F4 | Cohérence JSON→rapport parity | La prose cite `[F]/[M]/[A]` ; aucune balise `[C:]` ni rendu déterministe des décisions `archive\|merge\|pin\|reference` de `weekly-coherence-findings-<date>.json`. |
| F5 | Détection agent-loop | Audit §3.3 : re-spawns silencieux (`task_result` vide) en boucle, coût doublé. `insights.py` mentionne le motif dans des chaînes de recommandation, sans règle de détection. |
| F6 | Fiabilité chargement skill worker | Les branches worker chargent des skills (A/V/H/D/C) sans pre-flight ; une skill absente dégrade la branche sans signal clair dans le contrat. |

## 2. Goal

Rendre le run hebdomadaire **fiable et observable** sur six axes :

1. **F1** — Échouer vite et clairement avant le boot agent quand le worktree/moteur est mal résolu ; résoudre le worktree du kit depuis une source déterministe (emplacement du plugin), pas depuis le cwd de lancement.
2. **F2** — Rendre le manifest de curation (WAVE 2.5) détaillé (`mode`, statut par décision) et le **rendre dans le rapport** (markdown + HTML) de façon déterministe.
3. **F3** — Rapporter explicitement l'état Graphify (fraîcheur, projection) en **out-of-band** : lecture seule des artefacts existants, jamais d'invocation Graphify, coût ~0.
4. **F4** — Garantir la **parité** entre `weekly-coherence-findings-<date>.json` et le rapport : rendu déterministe de la section cohérence + balise `[C:]` validée + gate de couverture des décisions `archive/merge` et `severity: high`.
5. **F5** — Détecter les **boucles d'agent** (re-spawns `task` à résultat vide/identique, relectures répétées) par une règle déterministe dans insights, et borner les re-spawns côté orchestration (mitigation process, outil `task` upstream non modifiable).
6. **F6** — Fiabiliser le **chargement des skills worker** : pre-flight par branche, `skills_loaded` dans le contrat, mapping rc explicite, vérification orchestrateur avant dispatch.

Critère de succès : chaque correctif produit un artefact déterministe lisible dans le rapport du run suivant ; aucun échec silencieux des six familles dans les 3 runs suivant le déploiement.

## 3. Non-goals

- **Pas d'exécution Graphify** pendant le run hebdomadaire (ni CLI, ni skill, ni `graphify update`) — F3 est lecture seule d'artefacts existants.
- **Pas d'application automatique** de la curation : `apply=true` reste une décision humaine explicite ; F2 ne change que le contenu du manifest et son rendu, jamais la gate dry-run.
- **Pas de déplacement/archivage/fusion de skills** dans ce design — les décisions `archive\|merge` restent émises (cohérence) et proposées (curation dry-run) ; aucune opération fichiers.
- **Pas de modification de l'outil `task`** (upstream, hors périmètre) — F5 est détection + mitigation process.
- **Pas de changement** : bases SQLite, config CI/CD, règles `.harness-eval/`, contrats API, schémas de config existants (ajouts additifs uniquement).
- **Pas de nouvelle dépendance** multi-projet ; stdlib + modules moteur existants.
- **Pas de re-LLM** au merge/rendu : toutes les nouvelles sections rapport sont déterministes.

## 4. Architecture / data flow

### 4.1 Chaîne cible

```
lancement (cron --dir Adeo | /weekly-review)
  │
  ▼
[LAUNCHER] weekly-advisor.ts (plugin)
  ├─ worktree = WEEKLY_KIT_ROOT ?? racine dérivée du chemin du plugin ?? ctx.worktree/directory
  ├─ preflight F1 : weekly_preflight tool (resolveEngine + glob moteur + config)
  │     └─ échec → STOP message exact, rc=3, AUCUN boot agent
  ▼
[ORCHESTRATEUR] weekly-advisor.md
  ├─ Étape 0 : weekly_preflight (backstop launcher) + anti-re-run + doctor
  ├─ WAVE 1 : T seul (run) → V+H parallèles (attente runs/current)
  ├─ WAVE 1.5 : K workers A + garde re-spawn F5 (max 2 spawns/session)
  ├─ JOIN
  ├─ WAVE 2 : D / I / C — dispatch conditionné par vérif skill F6
  ├─ WAVE 2.5 : weekly_skill_curate → skill-curate-<date>.json v2 (F2)
  ├─ TAIL :
  │    ├─ weekly_graphify_state (F3) → weekly-graphify-state-<date>.json
  │    ├─ weekly_report_prep → draft
  │    ├─ report_assemble :
  │    │    ├─ rendu déterministe section Curation (F2, depuis skill-curate JSON)
  │    │    ├─ rendu déterministe section Graphify (F3, depuis graphify-state JSON)
  │    │    ├─ rendu déterministe section Cohérence (F4, depuis coherence-findings JSON)
  │    │    └─ gates parity F4 ([F:] high existant + [C:] archive/merge + [C:] high)
  │    └─ HTML (html_report.py : 3 nouvelles sections)
  └─ exit 0/1/2 ; rc=3 réservé au preflight launcher (pas de rapport)
```

### 4.2 Flux de données (artefacts)

| Correctif | Entrée | Sortie | Consommateur |
|---|---|---|---|
| F1 | plugin path / env / ctx | `weekly_preflight` → `{worktree, engine_ok, python_ok, config_ok, py_count}` | agent Étape 0, launcher |
| F2 | `weekly-coherence-findings-<date>.json` + catalog | `skill-curate-<date>.json` v2 (`mode`, `status` par décision) | `report_assemble`, HTML |
| F3 | `graphify-out/graph.json`, `graphify-out/GRAPH_REPORT.md`, HEAD du worktree | `weekly-graphify-state-<date>.json` (`status: ok\|unavailable`, `stale`, `summary`) | `report_assemble`, HTML |
| F4 | `weekly-coherence-findings-<date>.json` | section Cohérence rendue + balises `[C:]` | rapport MD/HTML, gate parity |
| F5 | télémétrie sessions (tool_calls, invocations `task`) | findings `agent-loop` dans `weekly-insights-<date>.json` | rapport, annexe |
| F6 | contrat worker | `skills_loaded` dans le contrat ; synthèse JOIN | orchestrateur, rapport annexe |

### 4.3 Résolution du worktree (F1, détail)

Ordre de résolution dans le plugin (remplace `ctx.worktree ?? ctx.directory` seul, L342) :

1. `process.env.WEEKLY_KIT_ROOT` si défini (override explicite, aligné sur `WEEKLY_PYTHON`).
2. Racine dérivée du **chemin du module plugin** : `dirname(plugin) /../..` → `<worktree>/.opencode/plugins/weekly-advisor.ts` ⇒ worktree = `<worktree>`. Source de vérité : le kit est toujours déployé dans `<worktree>/.opencode/` (layout v6.0.b).
3. Fallback `ctx.worktree ?? ctx.directory` (comportement actuel).

Le preflight (`weekly_preflight`) vérifie ensuite, depuis ce worktree résolu :
- moteur `resolveEngine()` (dir + python + config) — déjà throw-able, rendu en retour structuré ;
- `glob .opencode/plugins/weekly-advisor-engine/**/*.py` → `py_count` ;
- config `weekly-telemetry-config.json` lisible → `config_ok`.

Échec → message exact conservé : « worktree Adeo requis — relancer avec `--dir /home/benjamin/Dev/Adeo` (ou via le cron) », rc=3, **avant tout travail de run** et sans boot agent. Le pre-check Étape 0 de l'agent (glob) reste en place comme filet (défense en profondeur, comportement inchangé).

### 4.4 Manifest curation v2 (F2, schéma additif)

`curation.py` enrichit `skill-curate-<date>.json` sans casser les lecteurs actuels :

```json
{
  "schema_version": 2,
  "mode": "dry-run",
  "generated_at": "2026-09-01T11:36:31Z",
  "anchor": "2026-09-01T11:36:01Z",
  "summary": { "total": 3, "by_action": {"archive": 2, "merge": 1}, "skipped": 1 },
  "decisions": [
    {
      "skill_id": "skill_ab12cd34",
      "action": "archive",
      "target_skill_id": "skill_ab12cd34",
      "reason": "coherence finding tag_action=archive",
      "status": "proposed"
    }
  ],
  "skipped": [
    { "skill_id": "skill_user01", "reason": "origin=user (protection R4)" }
  ]
}
```

`status` ∈ `proposed | applied | skipped_origin_user | skipped_ttl | skipped_no_target`. En `dry-run`, tout `status=proposed` ; en `apply`, les décisions effectivement appliquées passent `applied`, les protégées `skipped_origin_user`. Champs existants (`decisions[].skill_id|action|target_skill_id|reason`) conservés.

### 4.5 État Graphify (F3, schéma)

`weekly-graphify-state-<date>.json` :

```json
{
  "schema_version": 1,
  "status": "ok",
  "graph_path": "<worktree>/graphify-out/graph.json",
  "report_md_path": "<worktree>/graphify-out/GRAPH_REPORT.md",
  "report_md_mtime": "2026-08-31T15:15:01Z",
  "built_at_commit": "eb6a1a2",
  "head_commit": "eb6a1a2",
  "stale": false,
  "summary": { "node_count": 412, "edge_count": 903, "source_file_count": 58, "filtered": {...} }
}
```

`status: unavailable` + `reason` si `graph.json` absent, illisible ou mal formé (jamais de crash run). `stale = built_at_commit != head_commit` (diff Git du worktree, `git rev-parse HEAD`). Nouvelle sous-commande déterministe `graphify-state` (cli/main.py) réutilisant `graphify_summary.load_and_summarize`.

## 5. Composants

| Composant | Fichier | Modification |
|---|---|---|
| Launcher (plugin) | `.opencode/plugins/weekly-advisor.ts` | Résolution worktree §4.3 ; tool `weekly_preflight` ; log racine résolue à l'init |
| Commande | `.opencode/commands/weekly-review.md` | Étape 0 : appeler `weekly_preflight` avant déroulement ; rc=3 documenté |
| Orchestrateur | `.opencode/agents/weekly-advisor/weekly-advisor.md` | Étape 0 renforcée (preflight tool) ; garde re-spawn F5 (WAVE 1.5, max 2 spawns/session) ; vérif agent worker avant dispatch (F6) ; étape TAIL + `weekly_graphify_state` ; synthèse JOIN avec `skills_loaded` agrégés |
| Worker | `.opencode/agents/weekly-advisor/weekly-advisor-worker.md` | Pre-flight skills par branche (F6) ; contrat + `skills_loaded` ; mapping rc |
| Moteur CLI | `main.py`, `cli.py` | Sous-commande `graphify-state` (F3) |
| Curation | `curation.py` | Manifest v2 additif (F2) |
| Insights | `insights.py` | Règle `agent-loop` (F5) |
| Rapport | `report.py` | Rendu sections Curation (F2) / Graphify (F3) / Cohérence (F4) ; gates parity `[C:]` |
| HTML | `html_report.py` | 3 nouvelles sections |
| Télémétrie | `sqlite_reader.py` / `aggregator.py` | Extraction minimalement étendue pour F5 : comptage par (tool, fingerprint d'args) pour `task` et lectures répétées (additif, les `tool_calls` nom-level existants conservés) |
| Skill prose | `weekly-report-prose/SKILL.md` | Documenter balise `[C:category]` + contrat parity (mise à jour au moment de l'implémentation) |
| Config | `weekly-telemetry-config.json` | Clés additives : `insights.loop_min_repeats` (défaut 8), `insights.loop_task_min_repeats` (défaut 3), `graphify_out` chemin (défaut `<project_root>/graphify-out`) |

## 6. Error handling

| Scénario | Réaction | rc | continue |
|---|---|---|---|
| F1 : preflight échoue (moteur/python/config absent, 0 `.py`) | STOP avant boot agent, message exact | 3 | non (pas de rapport) |
| F1 : preflight OK mais run déjà terminé pour l'ancre | Garde anti-re-run existante | 0 | non (short-circuit) |
| F2 : manifest absent + signaux curation dans coherence | Gate P0 existante conservée (warning + rc=1) | 1 | oui |
| F2 : manifest présent mais `mode: dry-run` | Rendu section avec badge dry-run ; aucune mutation | 0/1 | oui |
| F3 : `graph.json` absent/illisible/malformé | `weekly-graphify-state` `status: unavailable` + raison ; section rapport « non disponible » | 1 (warning annexe) | oui |
| F3 : graphe stale (`built_at_commit != HEAD`) | Warning annexe « graphe obsolète — relancer graphify hors run » ; jamais d'invocation | 1 | oui |
| F4 : décision `archive/merge` ou finding `high` non couvert par le rendu/prose | Gate parity : violation + warning annexe | 1 | oui (rapport sorti) |
| F5 : boucle détectée (repeats ≥ seuil) | Finding `agent-loop` (severity par coût) ; annexe rapport ; orchestrateur borne les re-spawns (max 2/session) | 0/1 | oui |
| F6 : skill primaire de branche absente (D drafting, C coherence, A audit) | `skills_loaded: ok=false` ; contrat rc=2 (fatal branche → STOP orchestrateur, pas de rapport) | 2 | non |
| F6 : skill secondaire absente (V watch-review, H remediation) | `skills_loaded: ok=false` ; rc=1 + warning `skill-missing:<name>` ; branche tente en dégradé | 1 | oui |
| F6 : agent worker absent au dispatch | STOP avant WAVE 1 (message clair) | 2 | non |

Règle centrale inchangée : rc=2 réservé aux fatalités (moteur, permission élevée, skill primaire absente) ; tout le reste rc=1 + warning + continuer.

## 7. Tests

Fichiers : `tests/test_preflight.py` (nouveau), `tests/test_graphify_state.py` (nouveau), extensions `test_curation.py`, `test_report.py`, `test_html_report.py`, `test_insights.py`.

| Test | Correctif | Assertion clé |
|---|---|---|
| `test_preflight_resolves_worktree_from_plugin_path` | F1 | Racine dérivée du chemin plugin = `<worktree>` même si cwd ≠ worktree |
| `test_preflight_fails_fast_engine_missing` | F1 | `py_count == 0` ⇒ `engine_ok=false`, message exact, aucun side-effect |
| `test_curation_manifest_v2_mode_and_status` | F2 | `mode: dry-run` → tout `status: proposed` ; `apply` → `applied`/`skipped_origin_user` ; champs v1 conservés |
| `test_report_renders_curation_detail` | F2 | Section Curation rendue depuis manifest (badge mode, comptes par action, statuts) |
| `test_graphify_state_unavailable` | F3 | Artifact absent/illisible → `status: unavailable`, run non crashé |
| `test_graphify_state_stale_flag` | F3 | `built_at_commit != HEAD` ⇒ `stale: true` |
| `test_report_renders_graphify_section` | F3 | Section Graphify avec stale/unavailable rendue |
| `test_report_coherence_parity_high_and_archive` | F4 | Décision `archive`/`merge` ou finding `high` non couvert → violation ; couvert → OK |
| `test_prose_c_tag_validation` | F4 | Balise `[C:category]` inexistante dans le JSON → rejet ; existante → accepté |
| `test_insights_loop_detection_task_empty` | F5 | ≥ `loop_task_min_repeats` invocations `task` à résultat vide/identique → finding `agent-loop` |
| `test_insights_loop_detection_read_repeats` | F5 | Nom-level count ≥ `loop_min_repeats` → finding `agent-loop` (fallback sans args) |
| `test_worker_skill_preflight_primary_missing` | F6 | Skill primaire absente → contrat `skills_loaded: ok=false`, rc=2 |
| `test_worker_skill_preflight_secondary_missing` | F6 | Skill secondaire absente → rc=1 + warning `skill-missing` |
| `test_orchestrator_blocks_dispatch_without_worker_agent` | F6 | Agent worker absent → STOP, rc=2 |

Fixtures : `graphify-out/graph.json` minimal + `GRAPH_REPORT.md`, manifest curation v2, coherence findings avec décisions, télémétrie avec invocations `task` répétées. Tests moteur en pytest (conventions existantes, venv du kit) ; tests plugin TS si le harnais le permet (sinon logique extraite testable en pur).

## 8. Rollout / rollback

**Ordre de déploiement** (chaque commit réversible, comportement additif) :

1. Moteur : `graphify-state` (F3), manifest v2 (F2), règle `agent-loop` (F5), rendus + gates rapport (F2/F3/F4), extraction télémétrie (F5). → tests verts (594 existants + nouveaux).
2. Plugin/commande : résolution worktree + `weekly_preflight` (F1). → tests plugin.
3. Agents/skills : orchestrateur (preflight, garde re-spawn, vérif dispatch), worker (pre-flight skills, contrat), `weekly-report-prose` (balise `[C:]`). → gates portabilité harness-eval (skills) avant commit.
4. Config : clés additives `insights.loop_*`, `graphify_out`.
5. Sync kit → Adeo (processus existant, `cmp` des fichiers agents/skills identiques, cf. audit-2026-08-27 §6).

**Rollback** : `git revert --no-edit` (humain) par commit ; les ajouts sont additifs (schémas v2 rétro-compatibles, sections rapport optionnelles) → un revert partiel ne casse pas le run. L'absence d'un artefact (manifest, graphify-state, coherence) produit un rendu « non disponible » ou le gate P0 existant, jamais un crash.

**Séquence de validation post-déploiement** : 1 run complet (exit=0/1) + vérification des 3 nouvelles sections du rapport + absence de warnings F5/F6 dans l'annexe.

## 9. Contraintes explicites

- **Aucune consommation runtime Graphify** : le run hebdomadaire n'invoque ni le CLI ni la skill Graphify, n'écrit jamais `graphify-out/` ; F3 = projection lecture seule d'artefacts existants. La régénération du graphe reste une action out-of-band (humain/autre process).
- **Aucun apply automatique** : la curation reste `dry-run` par défaut ; `apply=true` uniquement après validation humaine explicite ; F2 ne modifie ni la gate ni la politique de protection `origin=user` (R4).
- **Aucun déplacement de skills** : ce design ne déplace, n'archive, ne fusionne ni ne supprime aucun fichier skill ; les décisions restent émises (cohérence) et proposées (curation dry-run).
- **Additif strict** : aucun champ/schéma existant modifié en place ; les nouveaux champs sont optionnels pour les lecteurs actuels.
- **Déterministe** : toutes les nouvelles étapes et sections rapport sont sans LLM (sauf la prose optionnelle `[C:]` qui reste validée par le code).
- **Périmètre lecture/écriture** : worktree uniquement, inchangé (v6.0.c).
- **Phase courante** : documentation seule — ce fichier est le contrat ; l'implémentation (code, tests, docs skill, config) fait l'objet de commits ultérieurs via `writing-plans`.