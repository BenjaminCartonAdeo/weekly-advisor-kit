---
description: "Lance la revue hebdomadaire d'usage OpenCode (même chaîne que le cron) — orchestration parallèle en waves de subagents, spec v6.1."
agent: weekly-advisor
# model: décidé par le poste (--model <votre-modèle>) — sinon défaut de la config (l'agent n'impose plus de modèle)
---

# Revue hebdomadaire

Procédure canonique : agent `weekly-advisor` (`.opencode/agents/weekly-advisor/weekly-advisor.md`) — DAG, waves, contrats JOIN/RC, F6, fail-soft. Ce fichier est un wrapper thin, il ne duplique pas ces sections.

## Déroulement

Ordre figé des tools (miroir du tableau de l'agent — contrat surface 6, ne pas réordonner) : `weekly_doctor`, `weekly_run`, `weekly_releases`, `weekly_watch_distill`,
`weekly_watch_context`, `weekly_watch_validate`, `weekly_audit_candidates`, `weekly_show_session`, `weekly_harness`,
`weekly_harness_remediate`, `weekly_draft_candidates`, `weekly_commit_draft`, `weekly_insights`, `weekly_skill_curate`,
`weekly_report_prep`, `weekly_report_blocks_draft`, `weekly_report_assemble`, `weekly_self_cost`.

## Règles (rappel, détails dans l'agent)

- Coordinateur seul autorisé `task` ; plafonds : 3 workers T/V/H, puis K ≤ `audit_max_sessions` workers A, puis 3 workers D/I/C ; 10 min/worker, 1 passage/étape, ≤3 tours diagnostic.
- F6 dispatch + contrats envelopes + RC JOIN : voir agent (single-source). Échec tool → warning + continuer, sauf rc=2 fatal → STOP sans rapport.
- Fenêtre via override `lookback_days` (`N semaines` → N×7, `mois/30j` → 30, défaut config 7j) ; ancre glissante, rejou historique = `anchor` explicite. Config JSON jamais réécrite.

## Sortie

Terminer par le rapport HTML (`<project_root>/reports/html/weekly-report-latest.html`, puis archive `runs/current/weekly-report-<date>.md`), les alertes les plus sévères, et la dernière ligne `WEEKLY_REVIEW_RC=<rc_final>` (== `summary.exit` == `END ... exit=`).
