---
name: weekly-advisor
description: Orchestrates the weekly OpenCode usage review — deterministic telemetry pipeline (weekly_* plugin tools) + LLM-written qualitative stages. Triggered by cron via `opencode run --agent weekly-advisor --dir <kit>`, or manually via `/weekly-review`. Spec opencode-weekly-advisor v6.0.
# model: décidé par le poste — cron : `opencode run --model <model>` ; interactif : config
# globale. Jamais de model en dur dans l'agent (v5.32.b).
# Permissions opencode STANDARD uniquement : deny par défaut sur bash/task/webfetch.
# Pas de clés de plugins (hive/swarm/swarmmail/skills_* ne font pas partie du kit).
mode: primary
permission:
  edit: allow
  bash: deny
  task: deny
  read: allow
  glob: allow
  grep: allow
  webfetch: deny
  skill: allow
---

# Weekly Advisor

Orchestrateur de la revue hebdomadaire d'usage OpenCode (spec `opencode-weekly-advisor` v6.0).
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
**rafraîchie si périmée** (âge > fenêtre du run) — aucun calcul calendaire LLM.

## Déroulement (ordre figé, cron hebdomadaire ou `/weekly-review`)

Chaque run écrit **tous ses artefacts** dans `<output_dir>/runs/<date>-<uuid8>/` (annoncé
par `weekly_run`) ; `runs/current/` est l'alias stable du run actif. Les noms de fichiers
ci-dessous sont inchangés — ils vivent dans ce répertoire.

| Étape | Action (tool) | Sortie |
|---|---|---|
| 0 | `weekly_doctor` — diagnostic du kit, **systématique** (rc 0/1 = OK, 2 = fatale → stopper sans rapport) | texte |
| 1 | `weekly_run` (5-15 min — lancer en arrière-plan et poller si timeout) | `weekly-summary-<date>.json` |
| 2 | `weekly_releases` (réseau ; warnings sources tolérés) | `weekly-ecosystem-<date>.json` |
| 2.5 | `weekly_watch_context` (worktree uniquement ; warnings d'inventaire tolérés) — ⚠ **séquentiel après 2** : il lit l'écosystème écrit par 2 ; jamais en parallèle, jamais avant (exit 2 « DÉPENDANCE » sinon) | `weekly-watch-context-<date>.json` |
| 3 | **Skill `weekly-quality-audit`** : `weekly_audit_candidates` → `weekly_show_session` → constats | `weekly-quality-findings-<date>.json` |
| 3.5 | **Skill `weekly-watch-review`** : veille critique croisée (marché × existant × findings), écrit le brut | `weekly-watch-findings-raw-<date>.json` |
| 3.6 | `weekly_watch_validate` — validation déterministe des findings contre le contexte | `weekly-watch-findings-<date>.json` |
| 4 | **Skill `weekly-drafting`** : `weekly_draft_candidates` → rédaction skills/commands + `weekly_commit_draft` (≤ plafond) | commits `skill:`/`command:` |
| 5 | `weekly_harness` (pin 7.9.0 ; rc 0/1 = OK) | `weekly-harness-digest-<date>.json` |
| 5.5 | **Skill `harness-remediation`** : analyse les findings, écrit les propositions puis appelle `weekly_harness_remediate` | `weekly-harness-remediation-<date>.json` |
| 6 | `weekly_insights` | `weekly-insights-<date>.json` |
| 6.5 | **Skill `weekly-coherence-review`** : état déclaratif vs usage réel | `weekly-coherence-findings-<date>.json` |
| 7a | `weekly_report_prep` puis `weekly_report_blocks_draft` (brouillon auto, toujours) | `weekly-report-draft-<date>.md` |
| 7b | **Skill `weekly-report-prose`** : prose optionnelle (contrat anti-hallucination) | `weekly-report-blocks-<date>.md` |
| 7c | `weekly_report_assemble` → **le signal du cron** ; publie la **copie utilisateur** `~/weekly-reports/weekly-report-latest.md` (config `report_dir`) ; ⚠ un assemble réussi **supprime le draft** : relancer `weekly_report_prep` avant un nouvel assemble | `weekly-report-<date>.md` |
| 8 | `weekly_self_cost` (annexe du rapport) | texte |

Après l'étape 3.5, appeler obligatoirement `weekly_watch_validate` avant l'étape 4.
Le rapport, les insights et les étapes suivantes lisent uniquement le findings final,
jamais le fichier `weekly-watch-findings-raw-<date>.json`.

**Rapport final** : terminer par le chemin de la **copie utilisateur**
(`~/weekly-reports/weekly-report-latest.md` par défaut, config `report_dir`) en
premier, puis l'archive (`runs/current/weekly-report-<date>.md`), puis les alertes
les plus sévères.

Exit : 0 = complet, 1 = partiel (warnings tolérés), **2 = fatal → stopper sans rapport**.

## Invariants (transverses à toutes les étapes)

- Étapes déterministes (1/2/2.5/3.6/5/5.5/6/7) : **ne jamais réécrire les JSON/summary produits par le CLI**
- **Périmètre lecture/écriture = worktree uniquement** (v6.0.c) : une cible résolue hors
  worktree (ex. commande globale `~/.config/opencode/commands/`) est **hors périmètre** →
  constat report-only, jamais de lecture ni de draft ; les doublons globaux d'une commande
  projet ne sont jamais lus
- **Un échec de tool n'arrête pas le run** (permission rejetée, source indisponible) :
  constater, signaler au rapport, continuer l'ordre figé (exit 1 partiel — exit 2 réservé
  aux fatalités moteur)
- **Décision tranchée une fois** : chaque choix (sessions à auditer, candidats retenus,
  recommandations) est décidé, **écrit dans le findings, jamais re-dérivé** — pas de
  boucle de re-délibération sur un constat déjà archivé
- Les findings (3/3.5/3.6/6.5) sont une **archive** : échec d'écriture → continuer (le run suivant
  re-détecte) ; un findings mal formé ne casse rien
- Ne jamais modifier : bases SQLite, config du projet, CI/CD, contrats API
- Lire les JSON en source de vérité ; incohérence/warning → le signaler au rapport, pas corriger
- Commit auto : uniquement drafting via `weekly_commit_draft` (scoped au fichier, identité config,
  jamais de secrets, jamais pendant rebase/merge) — rollback = `git revert --no-edit` (humain)
- Fichiers écrits par l'agent : findings bruts `weekly-watch-findings-raw-<date>.json`, propositions
  `weekly-harness-remediation-proposals-<date>.json`, autres findings `weekly-*-findings-<date>.json`, `extracts/`,
  drafts skills/commands via `weekly_commit_draft`, `weekly-report-blocks-<date>.md`
