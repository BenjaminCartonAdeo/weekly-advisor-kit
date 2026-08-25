# Design — Orchestration parallèle du run hebdomadaire (waves de subagents)

Date : 2026-08-25 · Statut : approuvé (design), implémentation à planifier
Périmètre : orchestration agent/commande du kit weekly-advisor. **Moteur Python et plugin TS inchangés.**

---

## 1. Contexte & objectif

Le run complet dure aujourd'hui 10-30 min (annoncé README), dominé par l'étape 1 (télémétrie,
5-15 min) exécutée strictement en séquence avant tout le reste, alors que les branches
veille (V) et harnais (H) n'en dépendent pas.

Objectif : **run complet < 10 min** en parallélisant les branches indépendantes via des
subagents, sans toucher au moteur déterministe ni aux contrats de sortie.

Décisions actées :

1. La cible est le run entier (< 10 min), pas une seule étape.
2. L'invariant « jamais de dispatch en subagent » est levé explicitement.
3. Approche retenue : **orchestration DAG par subagents** (option A).
   Rejets : parallélisme interne moteur Python (follow-up possible si l'étape 1 reste
   limitante) ; méta-tool déterministe `weekly_pipeline` (YAGNI).

## 2. Architecture — waves

La session principale devient un **coordinateur léger** : gate, dispatch, join, tail.

```
/weekly-review (agent weekly-advisor = orchestrateur + gestionnaire de contexte)
│
├─ Étape 0 doctor (gate ; rc=2 → STOP sans rapport)          [session principale]
│
├─ WAVE 1 — 3 subagents `task` lancés en parallèle (un message, trois appels)
│   ├─ T : weekly_run → poll achèvement → skill weekly-quality-audit   (findings audit)
│   ├─ V : weekly_releases → watch_distill → watch_context
│   │      → skill weekly-watch-review → watch_validate
│   └─ H : weekly_harness → skill harness-remediation (+ harness_remediate)
│
├─ JOIN — fusion des résultats + codes sortie                 [session principale]
│
├─ WAVE 2 (même mécanisme, activée par défaut)
│   ├─ D : skill weekly-drafting (étape 4, seul worker autorisé à committer)
│   ├─ I : weekly_insights (étape 6)
│   └─ C : skill weekly-coherence-review (étape 6.5)
│
└─ TAIL — 7a report_prep + blocks_draft → 7b prose → 7c assemble
          → 8 self_cost                                      [session principale]
```

Raisons du découpage :

- **T/V/H disjoints** : fichiers de sortie distincts, aucune lecture croisée (validé sur la
  spec fonctionnelle §cycle de vie). Leur parallélisme ne peut pas créer d'interblocage.
- **Tail en session principale** : prose + assemble synthétisent toutes les branches ;
  le contexte croisé est nécessaire et c'est le livrable final.
- **Wave 2 optionnelle** : désactivable par flag si l'on veut limiter le fan-out ; par défaut
  activée car D/I/C sont mutuellement indépendants une fois wave 1 jointe.

Gain attendu : max(T, V, H) + wave 2 + tail ≈ **8-12 min** contre 10-30.

## 3. Composants

```
.opencode/agents/weekly-advisor/
├── weekly-advisor.md          → ORCHESTRATEUR + GESTIONNAIRE DE CONTEXTE
└── weekly-advisor-worker.md   → worker subagent (nouveau, mode subagent)
```

### 3.1 Orchestrateur (`weekly-advisor.md`, réécrit)

Inspiré des patterns context-manager (briefing minimal, synthèse, source de vérité unique) :

1. **Briefing packages** — chaque worker reçoit un paquet minimal-complet : steps ordonnés de
   sa branche, chemin du répertoire de run + overrides de fenêtre, invariants applicables à
   sa branche uniquement (jamais le document entier), contrat de retour obligatoire.
2. **Synthèse au join** — fusion des trois contrats JSON en un « état du run » narratif court
   (< 500 tokens) : statut par branche, warnings agrégés, fatalités éventuelles. Cette
   synthèse alimente wave 2 et le tail — jamais les sorties brutes des workers.
3. **Source de vérité unique** — les findings et JSONs sur disque restent la seule archive ;
   l'orchestrateur ne duplique jamais leur contenu dans son contexte ni dans la synthèse
   (pointeurs + statuts seulement).
4. **Dépendances & gating** — seul l'orchestrateur porte la connaissance du DAG (waves,
   attente V/H sur le summary de T) ; un worker ignore l'existence des autres branches.
5. **Alerte compaction** — si un worker renvoie au-delà du contrat (sortie verbeuse),
   l'orchestrateur tronque au contrat, note une violation et continue en fail-soft.

Permissions orchestrator : `task: allow` s'ajoute à l'existant (edit allow, bash deny,
read/glob/grep allow, skill allow, webfetch deny).

### 3.2 Worker (`weekly-advisor-worker.md`)

Agent mode `subagent`. Permissions : edit allow, bash DENY, read/glob/grep allow, skill allow,
webfetch deny (le réseau passe par les tools moteur, jamais par webfetch). Un worker n'exécute
que sa branche : il ne connaît ni les autres branches ni la logique de merge.

## 4. Contrat de retour worker

Retour structuré **obligatoire**, dernière sortie du worker :

```json
{
  "branch": "V",
  "rc": 0,
  "steps_done": ["releases", "distill", "context", "watch-review", "validate"],
  "warnings": ["..."],
  "artifacts": ["weekly-watch-findings-2026-08-25.json"],
  "elapsed_s": 412
}
```

- `rc` : même sémantique que le moteur — 0 complet, 1 partiel (warnings tolérés),
  2 fatalité bloquante.
- Tout contenu hors contrat est ignoré/tronqué par l'orchestrateur.

## 5. Attente du répertoire de run

Aujourd'hui `weekly_run` (étape 1, branche T) annonce le répertoire `<output_dir>/runs/<date>-<uuid8>/`.
En parallèle, V et H démarrent avant son achèvement.

Mécanisme retenu : **les workers V/H attendent avant leur premier write** que `runs/current/`
expose le summary de T (poll read/glob, plafond 10 min ; dépassement → warning fail-soft,
la branche tente quand même ses steps en écriture différée si possible).

Alternative propre si le polling s'avère fragile en validation : micro-subcommand moteur
`ensure_run_dir` (création/alias anticipé du répertoire de run, sans télémétrie). À trancher
au plan avec un test réel ; aucun changement moteur par défaut.

## 6. Merge des codes sortie (JOIN)

- **Un seul rc=2 parmi les workers (ou crash sans résultat sur step critique) → STOP sans
  rapport.** Le rapport absent reste le signal du cron ; sémantique inchangée.
- Sinon : warnings des trois branches agrégés et passés au tail → ils alimentent le rapport
  comme aujourd'hui (succès partiel préservé, exit 1).
- Worker silencieux ou timeout → traité rc=1 + warning, le run continue
  (fail-soft, cohérent avec « l'échec d'un tool n'arrête pas le run »).

## 7. Instrumentation

- Chaque worker retourne `elapsed_s` + timings par step dans son contrat.
- Au join, l'orchestrateur écrit `weekly-timings-<date>.json` :
  `{branch: {step: ms}}` + durées wave/tail. Nouvel artefact écrit par l'agent —
  la liste fermée des fichiers agent-writable est étendue en conséquence.
- Base factuelle pour mesurer le gain vs baseline et arbitrer le follow-up B
  (parallélisme interne de l'étape 1) si elle reste le mur.

## 8. Garde-fous (invariants préservés)

- Ne jamais réécrire les JSON produits par le CLI (inchangé ; timings = fichier nouveau).
- Périmètre lecture/écriture = worktree (workers inclus, prompts paramétrés avec ce chemin).
- Exit 2 réservé aux fatalités moteur ; échec non fatal → exit 1 avec rapport.
- Findings = archive ; fiches blocked-security jamais soumises au LLM.
- Ancre gérée par le plugin (`<output_dir>/anchor-last.txt`) ; fenêtre en override de run.
- Commits drafting : seul le worker D committe, périmètre worktree `.opencode/`,
  un commit par écriture, gate de portabilité skill-verify inchangée
  (error→refus, warning→note).
- Cron/headless : point d'entrée identique ; WEEKLY_NO_BROWSER respecté au tail.

## 9. Tests, docs, validation

- Gate G1 (`scripts/check-flow-docs.mjs`) : contrats flow mis à jour (DAG en waves documenté).
- Docs : ARCHITECTURE.md (section orchestration), README (timing annoncé ~8-12 min),
  INSTALL (note cron inchangée).
- Validation : checklist manuelle d'un run complet — merge rc (0/1/2), artefacts disjoints,
  attente V/H sur summary T, présence et plausibilité de `weekly-timings-<date>.json`,
  rapport final identique en structure.
- Aucun test Python nouveau (moteur intact).

## 10. Risques connus

- Fan-out LLM simultané (3 workers) : pics de quota possibles ; mitigation = wave 2
  désactivable, workers indépendants donc reprise branche par branche possible.
- Sortie worker hors contrat : couvert par troncature orchestrateur (§3.1.5) + fail-soft (§6).
- Polling run-dir fragile : alternative moteur prévue (§5), décision au plan après test réel.
