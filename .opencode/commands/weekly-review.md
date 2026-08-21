---
description: "Lance la revue hebdomadaire d'usage OpenCode (même chaîne que le cron) — pipeline weekly-advisor complet, spec v6.0."
agent: weekly-advisor
# model: décidé par le poste (--model <votre-modèle>) — sinon défaut de la config (l'agent n'impose plus de modèle)
---

# Revue hebdomadaire

Lance la revue hebdomadaire complète de l'usage OpenCode, dans l'ordre figé de la
spec `opencode-weekly-advisor` v6.0 : télémétrie, veille, audit qualitatif, veille
critique, drafting, lint harness, insights, cohérence, rapport final.

La procédure de référence (tableau des étapes : tools, sorties, détails) est
l'agent `weekly-advisor` (`.opencode/agents/weekly-advisor/weekly-advisor.md`).
L'ancre est gérée par le plugin via `<output_dir>/anchor-last.txt` — aucun calcul
manuel.

## Déroulement

Ordre figé, exécution directe dans la session courante — jamais de dispatch en
subagent (tool `task`) : le weekly-advisor est déjà l'orchestrateur.

0. `weekly_doctor` — diagnostic du kit (2 = fatal → stopper sans rapport)
1. `weekly_run` — télémétrie (5-15 min : lancer en arrière-plan et poller si timeout)
2. `weekly_releases` — veille écosystème
2.5. `weekly_watch_context` — inventaire worktree ; ⚠ **séquentiel après 2** (jamais
    en parallèle, jamais avant — exit 2 « DÉPENDANCE »)
3. Audit qualitatif (skill `weekly-quality-audit`) — `weekly_audit_candidates` + `weekly_show_session`
3.5. Veille critique (skill `weekly-watch-review`) → brut `weekly-watch-findings-raw-<date>.json`
3.6. `weekly_watch_validate` — validation déterministe, obligatoire avant l'étape 4
4. Auto-drafting (skill `weekly-drafting`) — `weekly_draft_candidates` + `weekly_commit_draft`
5. `weekly_harness` — lint `.opencode/` (rc 0/1 = OK)
5.5. Remédiation harness (skill `harness-remediation`) — propositions puis `weekly_harness_remediate`
6. `weekly_insights` — deltas, alertes, maintenance
6.5. Cohérence environnement (skill `weekly-coherence-review`)
7. Rapport : `weekly_report_prep` → `weekly_report_blocks_draft` → prose (skill
   `weekly-report-prose`) → `weekly_report_assemble` → `weekly-report-<date>.md`
8. `weekly_self_cost` — coût du run

## Règles

- Exit 2 (fatal) à une étape → stopper sans rapport ; un échec de tool n'arrête pas
  le run (constater, signaler au rapport, continuer — exit 1 partiel)
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