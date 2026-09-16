---
description: "Lance la revue hebdo complète — orchestration parallèle en waves de subagents, même chaîne que le cron, spec v6.1. Use when the full weekly review has to be triggered."
agent: weekly-advisor
metadata:
  authored_by: opencode-weekly-advisor
  authored_at: "2026-09-12T00:00:00Z"
  origin: weekly-background
  write_context: "cost guardrails + HTML deliverable gate + bounded context"
  confidence: medium
  source_sessions: ["ses_f784c329effeIaiD4j27H1aZ8x", "ses_f8838dd93ffeHLiRc64XE1yIsz", "ses_f6ecd75aeffe8Be388kKb4djkQ"]
  overlaps_with: []
  target_agents: []
  last_verified_at: null
  verification: none
  usage: { last_loaded: null, load_count: 0 }
  ttl_policy: decay
# model: décidé par le poste (--model <votre-modèle>) — sinon défaut de la config (l'agent n'impose plus de modèle)
---

# Revue hebdomadaire

Lance la revue hebdomadaire complète de l'usage OpenCode (spec `opencode-weekly-advisor` v6.1).

Quand utiliser : déclenchement manuel de la même chaîne que le cron — orchestration par
waves de subagents, télémétrie, veille, audit, curation, rapport.

La procédure de référence — DAG et ordre des waves, étapes par wave, fenêtre du run,
garde-fous de délégation, contrat final JOIN / RC, invariants — est l'agent `weekly-advisor`
(`.opencode/agents/weekly-advisor/weekly-advisor.md`). Ne pas la dupliquer ici : ce command
ne fait que déclencher l'agent, qui reste la source de vérité unique.

## Déroulement

Ordre figé des outils d'étapes (machine-vérifié contre le tableau de l'agent par
`scripts/check-flow-docs.mjs`, surface 6). La sémantique de chaque étape vit dans l'agent.

1. `weekly_doctor` — gate de démarrage
2. `weekly_run` — télémétrie et activation du run
3. `weekly_releases` — veille écosystème
4. `weekly_watch_distill` — distillation des fiches candidates
5. `weekly_watch_context` — inventaire worktree et crosswalk
6. `weekly_watch_validate` — validation des findings de veille
7. `weekly_audit_candidates` — sélection des sessions à auditer
8. `weekly_show_session` — transcription bornée d'une session
9. `weekly_harness` — digest harness-eval
10. `weekly_harness_remediate` — gate de remédiation
11. `weekly_draft_candidates` — candidats au drafting
12. `weekly_commit_draft` — commit auto-rédigé (seul commiteur)
13. `weekly_insights` — deltas et maintenance
14. `weekly_skill_curate` — curation (dry-run par défaut)
15. `weekly_report_prep` — préparation du brouillon de rapport
16. `weekly_report_blocks_draft` — blocs auto
17. `weekly_report_assemble` — assemblage du rapport
18. `weekly_self_cost` — coût de la fenêtre

## Règles

- Source de vérité unique : l'agent `.opencode/agents/weekly-advisor/weekly-advisor.md`.
  Ne rien dupliquer au-delà de la liste d'ordre ci-dessus.
- Dernière ligne de réponse : `WEEKLY_REVIEW_RC=<rc_final>`.
