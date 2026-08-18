---
description: "Lance la revue hebdomadaire d'usage OpenCode (même chaîne que le cron) — pipeline weekly-advisor complet, spec v6.0."
agent: weekly-advisor
# model: décidé par le poste (--model <votre-modèle>) — sinon défaut de la config (l'agent n'impose plus de modèle)
---

# Revue hebdomadaire

Lance la revue hebdomadaire complète de l'usage OpenCode, dans l'ordre figé de la
spec `opencode-weekly-advisor` v6.0 : télémétrie, veille, audit qualitatif, veille
critique, drafting, lint harness, insights, cohérence, rapport final.

## Déroulement

Les étapes déterministes passent par les tools du plugin (`weekly_*`) — l'ancre est
gérée par le plugin via `<output_dir>/anchor-last.txt` (créée / rafraîchie si périmée),
aucun calcul manuel. La fenêtre peut se déduire du prompt (« N semaines » → `N×7`) et
se passer en override `lookback_days` sur `weekly_run`/`weekly_releases` (v6.0.b) :

0. `weekly_doctor` — diagnostic du kit (2 = fatal → stopper)
1. `weekly_run` — télémétrie
2. `weekly_releases` — veille écosystème
2.5. `weekly_watch_context` — inventaire worktree + crosswalk marché/existant
3. Audit qualitatif (skill `weekly-quality-audit`) — `weekly_audit_candidates` + `weekly_show_session`
3.5. Veille critique (skill `weekly-watch-review`) → `weekly-watch-findings-raw-<date>.json`
3.6. `weekly_watch_validate` — validation déterministe du finding contre le contexte
4. Auto-drafting (skill `weekly-drafting`) — `weekly_draft_candidates` + `weekly_commit_draft`
5. `weekly_harness` — lint `.opencode/`
5.5. Remédiation harness (skill `harness-remediation`) — propositions puis `weekly_harness_remediate`
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
