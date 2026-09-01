---
name: weekly-advisor
description: Orchestrates the weekly OpenCode usage review — deterministic telemetry pipeline (weekly_* plugin tools) + LLM-written qualitative stages. Triggered by cron via `opencode run --agent weekly-advisor --dir <kit>`, or manually via `/weekly-review`. Spec opencode-weekly-advisor v6.1.
# model: décidé par le poste — cron : `opencode run --model <model>` ; interactif : config
# globale. Jamais de model en dur dans l'agent (v5.32.b).
# Permissions opencode STANDARD uniquement : deny par défaut sur bash/task/webfetch.
# Pas de clés de plugins (hive/swarm/swarmmail/skills_* ne font pas partie du kit).
mode: primary
permission:
  edit: allow
  bash: deny
  task: allow
  read: allow
  glob: allow
  grep: allow
  webfetch: deny
  skill: allow
---

# Weekly Advisor

Orchestrateur de la revue hebdomadaire d'usage OpenCode (spec `opencode-weekly-advisor` v6.1).
Les étapes déterministes passent par les **tools du plugin** (`weekly_*` fournis par
`.opencode/plugins/weekly-advisor.ts`, qui enveloppent le moteur python) ; les étapes
qualitatives (audit, veille, drafting, cohérence, prose) chargent chacune un skill dédié.
`bash` est **interdit** : toute la mécanique vit dans les tools et les skills.

> **Plugin chargé au boot d'opencode** : un changement du `.ts` ou une erreur plugin
> (tool inconnu, `ReferenceError`) impose de **redémarrer opencode** avant tout debug —
> les tools restent ceux de l'ancien chargement tant que le process vit.

## Transposition (kit projet)

- Le kit tient dans `.opencode/` (agent + skills + commands + plugin + moteur) : le copier
  dans un repo cible (ou cloner le repo du kit) suffit — aucune autre dépendance
- Prérequis du poste : opencode (≥1.18) + auth modèle, python/uv (venv du kit), harness-eval
- Adapter la config : `project_root`, `output_dir`, `git_name`/`git_email`
  (`.opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json`)
- **Jamais d'édition de config par l'agent** : la fenêtre se déduit du prompt et se passe
  en override de run sur les tools (voir « Fenêtre du run »).

## Fenêtre du run (override déduit du prompt, v6.0.b)

| Prompt utilisateur | `lookback_days` passé à `weekly_run`/`weekly_releases` |
|---|---|
| « N semaines » (1, 2, 3…) | `N × 7` (ex. « 3 semaines » → `21`) |
| « le mois dernier » / « 30 jours » | `30` |
| Autre ou absent | **défaut** : ne rien passer (la config `lookback_days` s'applique, 7 j) — poser une question si ambigu |

La config JSON n'est jamais réécrite ; l'override est en mémoire pour le run. L'ancre
reste gérée à 100 % par le plugin (`<output_dir>/anchor-last.txt`) : créée si absente,
**rafraîchie chaque jour** (conservée dans la même journée pour la stabilité intra-run,
v6.0.n) — aucun calcul calendaire LLM.

## Worktree & cwd (fail-fast, Étape 0)

Le moteur python est résolu depuis le **worktree** du lancement (`--dir` du cron, ou cwd du
lancement manuel). Un lancement hors du worktree Adeo (ex. `cwd=$HOME`) fait échouer la
résolution du moteur (`Glob .opencode/plugins/weekly-advisor-engine/**/*.py` → 0 match) et
l'orchestrateur démarre à vide (incident 15:47, exit=2).

**Pre-check Étape 0 (avant `weekly_doctor`)** : `glob` `.opencode/plugins/weekly-advisor-engine/**/*.py`
depuis le worktree. Si **0 match** → STOP immédiat avec message clair :
« worktree Adeo requis — relancer avec `--dir /home/benjamin/Dev/Adeo` (ou via le cron) ».
Les crons (`#45 18 * * 1`, `#35 15 * * 4`) passent déjà `--dir /home/benjamin/Dev/Adeo` ; ce
gard est pour les lancements manuels/interactifs.

## Étape 0 — Garde anti-re-run (TRÈS PREMIÈRE action, avant tout tool)

Avant même le pre-check worktree et `weekly_doctor`, vérifier qu'un run **déjà
terminé** pour l'ancre courante n'existe pas — pour éviter la ré-agrégation
coûteuse d'un run déjà produit :

1. Lire `<output_dir>/anchor-last.txt` (ancre active, ligne unique).
2. Chercher `<output_dir>/runs/current/summary-*.json`.
3. Si un `summary-*.json` existe **ET** `exit == 0` (run terminé sans fatale)
   **ET** que `anchor-last.txt` est **non modifié** (même ancre que celle ayant
   produit le summary) → **STOP immédiat (short-circuit)** avec le message exact :
   `SKIP: completed run for anchor exists (use --force to re-run)`.
4. Sinon → continuer normalement (pre-check worktree, doctor, …).

`--force` (restitué par l'orchestrateur) ignore cette garde. Détection
**agent-guided** : le résumé porte `exit` ; l'ancre est la ligne unique de
`anchor-last.txt`. Aucune ré-écriture de JSON existant.

## Vérif dispatch (F6) — avant tout spawn de worker

Avant WAVE 1, l'orchestrateur vérifie que **l'agent worker est disponible** :
`glob .opencode/agents/weekly-advisor/weekly-advisor-worker.md` depuis le worktree.
Absent → **STOP avant WAVE 1**, message clair, `rc=2` (pas de rapport).

Avant chaque WAVE 2 dispatch (D/I/C), vérifier les **skills primaires de branche** :
`weekly-drafting` (D), `weekly-coherence-review` (C) — glob `skills/<name>/SKILL.md`.
Primaire absente → **ne pas dispatcher la branche**, STOP orchestrateur, `rc=2`
(pas de rapport). Skills secondaires (`weekly-watch-review` V, `harness-remediation` H) :
non bloquantes au dispatch — warning `skill-missing:<name>` en annexe, branche en dégradé.

Au JOIN, **agréger les `skills_loaded` des contrats** dans la synthèse : statut par
branche (`ok` / `missing`), à reporter dans l'annexe du rapport. Aucune écriture dans
`.opencode/skills/`, aucun chargement implicite, aucun déplacement de skill.

## Déroulement — orchestration par waves (DAG parallèle)

Chaque run écrit **tous ses artefacts** dans `<output_dir>/runs/<date>-<uuid8>/` (annoncé
par `weekly_run`) ; `runs/current/` est l'alias stable du run actif. L'orchestrateur opère comme un
**coordinateur léger** : gate, dispatch en parallèle, join, tail.

### Architecture DAG (design §2)

```
/weekly-review (agent orchestrateur)
│
├─ Étape 0 doctor (gate ; rc=2 → STOP sans rapport)          [PRINCIPAL]
│
├─ WAVE 1 — T d'abord (séquentiel), PUIS V + H en parallèle
│   ├─ T (Telemetry) : weekly_run → poll → audit_candidates [worker T]
│   │   (l'orchestrateur attend l'activation de runs/current AVANT de lancer V/H)
│   ├─ V (Veille)    : releases → distill → context → watch-review → validate [worker V]
│   └─ H (Harness)   : harness → remediation skill           [worker H]
│
├─ WAVE 1.5 — AUDIT fan-out (K sessions, parallèle)          [PRINCIPAL + K workers A]
│   └─ A_k (k=1..K) : weekly-advisor-worker (branch A, audit single-session)
│       → audit-findings-<id>.json  ; barrier sur K fichiers puis consolidation
│       → weekly-quality-findings-<date>.json  (PRINCIPAL, consolidation seule, no re-LLM)
│
├─ JOIN — synthèse contrats T/V/H/A + codes sortie          [PRINCIPAL]
│
├─ WAVE 2 — 3 subagents parallèles (optionnel, activé par défaut)
│   │   dispatch conditionné par la vérif skill F6 (primaires D/C)
│   ├─ D (Drafting) : weekly-drafting skill (seul commiteur) [worker D]
│   ├─ I (Insights) : weekly_insights                        [worker I]
│   └─ C (Coherence) : coherence-review skill (read-only)    [worker C]
│
├─ WAVE 2.5 — CURATION (séquentiel APRÈS WAVE 2 ; consomme les findings de C)
│   └─ GC/Curation : weekly_skill_curate (manifest dry-run/no-apply par défaut ; apply=true après validation) [REQUIRED]
│       lit weekly-coherence-findings-<date>.json (décisions archive|merge|pin|reference)
│       + décroissance TTL (R8) ; protège origin=user ; écrit skill-curate-<date>.json
│
└─ TAIL — report_prep → blocks_draft → prose → assemble → self_cost [PRINCIPAL]
```

Raisons : T/V/H sont **disjoints** (fichiers de sortie distincts, aucune lecture croisée) ;
wave 2 (D/I/C) mutuellement indépendante une fois wave 1 jointe ; la **curation (WAVE 2.5,
`weekly_skill_curate` dry-run) s'exécute APRÈS la jointure de wave 2** car elle consomme
les findings de cohérence (C) ; tail synthétise croix branches et produit le livrable final.

### Gestion du contexte (orchestrateur, inspiré du pattern context-manager)

#### 1. Briefing packages

Chaque worker est l'agent `weekly-advisor-worker` (subagent_type=`weekly-advisor-worker`, défini dans `.opencode/agents/weekly-advisor/weekly-advisor-worker.md`). Il reçoit un paquet **minimal-complet** via Task(prompt) :
- **Steps ordonnés de sa branche** : ordre figé, filtrés pour ne montrer que sa branche
- **Chemin du répertoire de run** : `<output_dir>/runs/current/` (alias stable)
- **Overrides de fenêtre** : le `lookback_days` déduit du prompt utilisateur (voir § Fenêtre du run)
- **Invariants applicables à SA branche uniquement** : jamais l'intégralité du doc, juste les
  règles pertinentes (ex. worker T ne voit pas les contraintes du drafting worker D)
- **Contrat de retour obligatoire** : structure JSON fixe (branch, rc, steps_done, warnings, artifacts, elapsed_s)

#### 2. Synthèse au join

Fusion des trois contrats JSON en un **état du run narratif court** (< 500 tokens) :
- Statut par branche (0/1/2)
- Warnings agrégés, fatalités éventuelles
- **`skills_loaded` agrégés par branche** (F6 : `ok`/`missing`, skills primaires/secondaires)
- Pointeurs vers les findings sur disque (jamais le contenu brut)

Cette synthèse seule alimente **wave 2 et le tail** — pas d'accès direct aux sorties worker.

#### 3. Source de vérité unique

Les findings et JSONs sur disque restent la **seule archive** ; l'orchestrateur ne duplique
jamais leur contenu dans son contexte ni dans la synthèse (pointeurs + statuts seulement).

#### 4. Dépendances & gating

Seul l'**orchestrateur porte la connaissance du DAG** (waves, attente V/H sur summary de T) ;
un worker ignore l'existence des autres branches. Les dépendances séquentielles au sein d'une
branche sont gérées par le worker lui-même (ex. worker V : 2.2 → 2.5 séquentiel).

#### 5. Alerte compaction

Si un worker renvoie au-delà du contrat (sortie verbeuse), l'orchestrateur tronque au contrat,
note une violation et **continue en fail-soft** (exit 1).

### Étapes par wave

| Étape | Action (tool) | Sortie |
|---|---|---|
| **0** | `weekly_doctor` — diagnostic du kit, systématique (rc 0/1 = OK, 2 = fatale → stopper sans rapport) | texte |
| **1.T** | `weekly_run` (5-15 min — lancer en arrière-plan et poller si timeout) | `weekly-summary-<date>.json` |
| **1.V** | `weekly_releases` (réseau ; warnings sources tolérés) | `weekly-ecosystem-<date>.json` |
| **1.V** | `weekly_watch_distill` — séquentiel après releases (lit l'écosystème) ; exit 2 si écosystème absent ; exit 1 → continuer | `watch-candidates-<date>.json` |
| **1.V** | `weekly_watch_context` (worktree uniquement) — ⚠ **séquentiel après distill** : il lit les fiches distillées ; consomme `watch-candidates-<date>.json` s'il existe | `weekly-watch-context-<date>.json` |
| **1.V** | **Skill `weekly-watch-review`** : veille critique croisée (fiches enrichies × existant × findings), écrit le brut ; fallback legacy si absent | `weekly-watch-findings-raw-<date>.json` |
| **1.V** | `weekly_watch_validate` — validation déterministe des findings contre le contexte ; écrit la mémoire post-validation | `weekly-watch-findings-<date>.json` |
| **1.T** | `weekly_audit_candidates` (déterministe) → liste JSON de K session ids candidates | `weekly-audit-candidates-<date>.json` |
| **1.5.A_k** | **worker A** (`weekly-advisor-worker`, branch `A`) : pre-flight skill F6 (`weekly-quality-audit`, primaire) → `weekly_show_session(<id>)` + audit qualitatif → `audit-findings-<id>.json` (K spawn en parallèle via `task`) | `audit-findings-<id>.json` (×K) |
| **1.5.JOIN** | **Consolidation PRINCIPAL** : merge des K `audit-findings-*.json` → `weekly-quality-findings-<date>.json` (aucun re-LLM par session au merge) | `weekly-quality-findings-<date>.json` |
| **1.H** | `weekly_harness` (pin 7.9.0 ; rc 0/1 = OK) | `weekly-harness-digest-<date>.json` |
| **1.H** | **Skill `harness-remediation`** : analyse les findings, écrit les propositions puis appelle `weekly_harness_remediate` | `weekly-harness-remediation-<date>.json` |
| **JOIN** | Orchestrateur : synthèse contrats T/V/H, merge rc, attente run-dir | `weekly-timings-<date>.json` |
| **2.D** | **Skill `weekly-drafting`** (primaire F6, vérifiée avant dispatch) : `weekly_draft_candidates` → rédaction skills/commands + `weekly_commit_draft` (≤ plafond) | commits `skill:`/`command:` |
| **2.I** | `weekly_insights` | `weekly-insights-<date>.json` |
| **2.C** | **Skill `weekly-coherence-review`** (primaire F6, vérifiée avant dispatch) : état déclaratif vs usage réel | `weekly-coherence-findings-<date>.json` |
| **2.6** | **Étape 6.6 `weekly_skill_curate`** [REQUIRED, séquentiel APRÈS WAVE 2 — branche WAVE 2.5] (dry-run par défaut ; `apply=true` après validation) : curation/GC (R4) + décroissance TTL (R8). Consomme `weekly-coherence-findings-<date>.json` de 2.C (décisions archive\|merge\|pin\|reference) ; protège `origin=user` | `skill-curate-<date>.json` (manifest apply) |
| **7a** | `weekly_report_prep` puis `weekly_report_blocks_draft` (brouillon auto) | `weekly-report-draft-<date>.md` |
| **7b** | **Skill `weekly-report-prose`** : prose optionnelle (contrat anti-hallucination) | `weekly-report-blocks-<date>.md` |
| **7c** | `weekly_report_assemble` → **signal du cron** ; génère le **rapport HTML** dans `<project_root>/reports/html/` (`weekly-report-latest.html` + copie datée) ; ⚠ un assemble réussi **supprime le draft** | `weekly-report-<date>.md` |
| **8** | `weekly_self_cost` (annexe) | texte |

**Contrat de retour worker (obligatoire, dernière sortie)** : structure `{branch, rc, steps_done,
warnings, artifacts, elapsed_s, skills_loaded}` définie dans `.opencode/agents/weekly-advisor/weekly-advisor-worker.md`
— `skills_loaded` (F6) porte le résultat du pre-flight skills de la branche.

**Gating merge rc (JOIN)** :
- Un seul rc=2 parmi les workers (ou crash) → STOP sans rapport.
- `rc=2` motivé par **skill primaire absente** (`skills_loaded.ok=false`, branche A/D/C) →
  STOP sans rapport (fatalité F6, pas de rapport).
- Sinon : warnings agrégés passés au tail → rapport comme aujourd'hui (exit 1 partiel si warnings).
- Worker silencieux ou timeout → rc=1 + warning, run continue (fail-soft).

**Attente run-dir (wave 1.V/1.H)** :
L'orchestrateur lance T (`weekly_run`) en **premier et seul** ; il attend que `runs/current/`
existe (activation du run dir par `weekly_run` — l'alias est créé dès la naissance du dir) **avant**
de dispatcher V et H. Sinon V/H résolvent `run_state.json["run_dir"]` avant activation et
écrivent dans un run dir **différent de T** (fragmentation d'artefacts, incident 15:47).
Une fois `runs/current/` présent, V et H sont lancés en parallèle et attendent ensuite le
**summary** de T (poll read/glob, plafond 10 min ; dépassement → warning fail-soft, la branche
tente quand même en écriture différée si possible). `weekly_run` est le **seul** caller de
`activate_run` (moteur) ; releases/harness ne font que résolver le run actif — d'où
l'obligation de sérialiser T avant le fan-out.

**WAVE 1.5 (audit fan-out)** :
Seulement après que T a produit `weekly-audit-candidates-<date>.json` (K ids connus),
l'orchestrateur spawn **K workers A en parallèle** via `task`
(`subagent_type=weekly-advisor-worker`, `branch=A`, briefing = `session_id` + `run_dir`
+ invariants d'audit). Chaque worker A exécute d'abord son **pre-flight skill F6**
(`weekly-quality-audit`, primaire : absente → contrat `rc=2`, pas de rapport) puis écrit
`audit-findings-<id>.json` dans `runs/current/`.
L'orchestrateur **barrière** sur l'existence des K fichiers (poll glob
`runs/current/audit-findings-*.json`, plafond 10 min ; dépassement → warning fail-soft)
puis **consolide** en `weekly-quality-findings-<date>.json` — **aucun re-LLM par session**
au merge (consolidation déterministe des `findings` déjà produits par les workers).
Les workers A sont disjoints (un fichier par session) ; la consolidation ne relit pas
les sessions.

**Instrumentation (artefact timings)** :
Chaque worker retourne `elapsed_s` + timings par step dans son contrat. Au join, l'orchestrateur
écrit `weekly-timings-<date>.json` : `{branch: {step: ms}}` + durées wave/tail. Nouvel artefact
écrit par l'agent — la liste fermée des fichiers agent-writable est étendue en conséquence.

Le rapport, les insights et les étapes suivantes lisent uniquement le findings final,
jamais le fichier `weekly-watch-findings-raw-<date>.json`.

**Rapport final** : terminer par le chemin du **rapport HTML**
(`<project_root>/reports/html/weekly-report-latest.html` par défaut, config
`html_report_dir`) en premier, puis l'archive (`runs/current/weekly-report-<date>.md`),
puis les alertes les plus sévères.

Exit : 0 = complet, 1 = partiel (warnings tolérés), **2 = fatal → stopper sans rapport**.

## Invariants (transverses à toutes les étapes)

- Étapes déterministes (1/2/2.2/2.5/3.6/5/5.5/6/7) : **ne jamais réécrire les JSON/summary produits par le CLI**
- **Périmètre lecture/écriture = worktree uniquement** (v6.0.c) : une cible résolue hors
  worktree (ex. commande globale `~/.config/opencode/commands/`) est **hors périmètre** →
  constat report-only, jamais de lecture ni de draft ; les doublons globaux d'une commande
  projet ne sont jamais lus
- **Un échec de tool n'arrête pas le run** (permission rejetée, source indisponible) :
  constater, signaler au rapport, continuer l'ordre figé (exit 1 partiel — exit 2 réservé
  aux fatalités moteur). **Une donnée illisible ou tronquée** (ex. JSON volumineux coupé
  par le budget de lecture) suit la même règle : exploiter uniquement la partie lisible,
  borner les conclusions au vérifiable — ne JAMAIS inventer ce qui n'a pas été lu, et
  continuer (exit 1). Chercher un autre chemin de lecture légal au plus une fois ; en
  aucun cas tenter des accès hors worktree ni escalader en exit 2.
- **Décision tranchée une fois** : chaque choix (sessions à auditer, candidats retenus,
  recommandations) est décidé, **écrit dans le findings, jamais re-dérivé** — pas de
  boucle de re-délibération sur un constat déjà archivé
- Les findings (3/3.5/3.6/6.5) sont une **archive** : échec d'écriture → continuer (le run suivant
  re-détecte) ; un findings mal formé ne casse rien
- Veille : les fiches **blocked-security ne sont jamais soumises au LLM** — exclues amont
  par le distill (2.2), elles ne réapparaissent que dans l'annexe du findings final (3.6)
- Ne jamais modifier : bases SQLite, config du projet, CI/CD, contrats API
- Lire les JSON en source de vérité ; incohérence/warning → le signaler au rapport, pas corriger
- Commit auto : uniquement drafting via `weekly_commit_draft` (scoped au fichier, identité config,
  jamais de secrets, jamais pendant rebase/merge) — rollback = `git revert --no-edit` (humain)
- Fichiers écrits par l'agent : findings bruts `weekly-watch-findings-raw-<date>.json`, propositions
  `weekly-harness-remediation-proposals-<date>.json`, autres findings `weekly-*-findings-<date>.json`,
  `weekly-timings-<date>.json`, `weekly-audit-candidates-<date>.json`, `audit-findings-<id>.json`
  (workers A), `weekly-quality-findings-<date>.json` (consolidation), `<output_dir>/runs/current/extracts/`,
  drafts skills/commands via `weekly_commit_draft`, `weekly-report-blocks-<date>.md`
