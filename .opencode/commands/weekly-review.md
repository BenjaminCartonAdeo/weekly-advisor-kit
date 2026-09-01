---
description: "Lance la revue hebdomadaire d'usage OpenCode (même chaîne que le cron) — orchestration parallèle en waves de subagents, spec v6.1."
agent: weekly-advisor
# model: décidé par le poste (--model <votre-modèle>) — sinon défaut de la config (l'agent n'impose plus de modèle)
---

# Revue hebdomadaire

Lance la revue hebdomadaire complète de l'usage OpenCode, dans l'ordre figé de la
spec `opencode-weekly-advisor` v6.1 : orchestration par waves parallèles de subagents,
télémétrie, veille, audit qualitatif, veille critique, drafting, lint harness, insights,
cohérence, rapport final.

La procédure de référence (tableau des étapes : tools, sorties, détails, architecture DAG) est
l'agent `weekly-advisor` (`.opencode/agents/weekly-advisor/weekly-advisor.md`).
L'ancre est gérée par le plugin via `<output_dir>/anchor-last.txt` — aucun calcul manuel.

## Déroulement

Orchestration par waves (design doc §2). La session principale agit comme coordinateur léger :
gate (étape 0), **vérif dispatch F6** (agent worker + skills primaires avant spawn), dispatch WAVE 1 en parallèle (3 subagents de l'agent `weekly-advisor-worker`), JOIN (synthèse), WAVE 2 optionnelle, **WAVE 2.5 curation [REQUIRED] (dry-run par défaut, après la jointure de WAVE 2 car consomme les findings de cohérence)**, TAIL.
Ordre figé des étapes (tokens d'outils) : `weekly_doctor`, `weekly_run`, `weekly_releases`, `weekly_watch_distill`,
`weekly_watch_context`, `weekly_watch_validate`, `weekly_audit_candidates`, `weekly_show_session`, `weekly_harness`,
`weekly_harness_remediate`, `weekly_draft_candidates`, `weekly_commit_draft`, `weekly_insights`, `weekly_skill_curate`,
`weekly_report_prep`, `weekly_report_blocks_draft`, `weekly_report_assemble`, `weekly_self_cost`.

## Règles

- Exit 2 (fatal) à une étape → stopper sans rapport ; un échec de tool n'arrête pas
  le run (constater, signaler au rapport, continuer — exit 1 partiel)
- **Vérif dispatch F6** : avant tout spawn de worker, vérifier l'agent
  `weekly-advisor-worker` (absent → STOP rc=2) et les skills primaires de branche
  (`weekly-drafting` D, `weekly-coherence-review` C, `weekly-quality-audit` A — absente
  → STOP rc=2, pas de rapport) ; skills secondaires (`weekly-watch-review` V,
  `harness-remediation` H) absentes → warning `skill-missing:<name>`, branche en dégradé
  (rc=1, run continue). Le worker retourne `skills_loaded` dans son contrat ; l'orchestrateur
  agrège au JOIN. Aucune écriture ni déplacement dans `.opencode/skills/`.
- Ne jamais réécrire les JSON produits par le CLI ; ne jamais modifier la config
  moteur (`weekly-telemetry-config.json`) — overrides en paramètres des tools
- Ne pas auditer au-delà de `audit_max_sessions` ; ne pas écrire plus de
  `max_candidates_per_run` drafts
- Terminer par : le chemin du rapport final (**rapport HTML**
  `<project_root>/reports/html/weekly-report-latest.html` en premier, puis l'archive
  `runs/current/weekly-report-<date>.md`) et les alertes les plus sévères
- Garde-fous de coût : max 3 tours pour diagnostiquer un échec tool ; plugin stale
  (ReferenceError) → constater, signaler, proposer le restart, **stopper le
  diagnostic** ; choix ambigu (ex. fenêtre multi-semaines) → poser UNE seule
  question, ne pas explorer le code du plugin

`weekly_skill_curate` forme WAVE 2.5 : après la jointure de WAVE 2 et avant le tail, elle
produit `skill-curate-<date>.json` en **dry-run/no-apply par défaut**. Aucune archive,
fusion, suppression ou édition sans validation humaine explicite suivie de `apply=true`.

## Fenêtre du run

La fenêtre se déduit du prompt et se passe en **override de run** (paramètre
`lookback_days` sur `weekly_run` et `weekly_releases`) — la config JSON n'est
jamais réécrite. **Ancre glissante (v6.0.n)** : sans override `anchor`, l'ancre
est rafraîchie chaque jour (conservée dans la même journée pour la stabilité
intra-run) — la fenêtre couvre donc toujours `[aujourd'hui - lookback, maintenant]`.
Rejouer une fenêtre historique = passer `anchor` explicitement (ex. la date du
run à rejouer).

| Prompt utilisateur | `lookback_days` à passer |
|---|---|
| « N semaines » (1, 2, 3…) | `N × 7` (ex. « 3 semaines » → `21`) |
| « le mois dernier » / « 30 jours » | `30` |
| Autre ou absent | **défaut** : ne rien passer (config `lookback_days` s'applique, 7 j) — poser une question si ambigu |
