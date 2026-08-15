---
description: "Lance la revue hebdomadaire d'usage OpenCode (même chaîne que le cron) — pipeline weekly-advisor complet, spec v5.32."
agent: weekly-advisor
# model: ex. opencode/deepseek-v4-flash-free — sinon défaut de la config du poste (l'agent n'impose plus de modèle)
---

# Revue hebdomadaire

Lance la revue hebdomadaire complète de l'usage OpenCode, dans l'ordre figé de la
spec `opencode-weekly-advisor` v5.32 : télémétrie, veille, audit qualitatif, veille
critique, drafting, lint harness, insights, cohérence, rapport final.

## Déroulement

Les étapes déterministes passent par les tools du plugin (`weekly_*`) — l'ancre est
gérée par le plugin via `<output_dir>/anchor-last.txt`, aucun calcul manuel :

1. `weekly_run` — télémétrie
2. `weekly_releases` — veille écosystème
3. Audit qualitatif (skill `weekly-quality-audit`) — `weekly_audit_candidates` + `weekly_show_session`
3.5. Veille critique (skill `weekly-watch-review`)
4. Auto-drafting (skill `weekly-drafting`) — `weekly_draft_candidates` + `weekly_commit_draft`
5. `weekly_harness` — lint `.opencode/`
6. `weekly_insights` — deltas, alertes, maintenance
6.5. Cohérence environnement (skill `weekly-coherence-review`)
7. Rapport : `weekly_report_prep` → `weekly_report_blocks_draft` → prose (skill
   `weekly-report-prose`) → `weekly_report_assemble` → `weekly-report-<date>.md`
8. `weekly_self_cost` — coût du run

## Règles

- Exit 2 (fatal) à une étape → stopper sans rapport
- Ne jamais réécrire les JSON produits par le CLI
- Ne pas auditer au-delà de `audit_max_sessions` ; ne pas écrire plus de
  `max_candidates_per_run` drafts
- Terminer par : le chemin du rapport final et les alertes les plus sévères