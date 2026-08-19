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
gérée par le plugin via `<output_dir>/anchor-last.txt`, aucun calcul manuel :

0. `weekly_doctor` — diagnostic du kit (2 = fatal → stopper)
1. `weekly_run` — télémétrie
2. `weekly_releases` — veille écosystème
2.5. `weekly_watch_context` — inventaire worktree + crosswalk marché/existant
   ⚠ **Séquentiel obligatoire** : 2.5 lit l'écosystème de 2 — ne jamais lancer
   `weekly_releases` et `weekly_watch_context` en parallèle, ni 2.5 avant 2
   (exit 2 « DÉPENDANCE » sinon)
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

- **Exécution directe obligatoire** : la revue s'exécute dans la session courante, dans
  l'ordre figé ci-dessus. **Jamais de dispatch en subagent** (tool `task`) pour lancer la
  revue — le weekly-advisor est déjà l'orchestrateur ; une tentative de dispatch via
  `task` gaspillait ~3 tours avant l'exécution directe lors du run du 2026-08-16
- Exit 2 (fatal) à une étape → stopper sans rapport
- Ne jamais réécrire les JSON produits par le CLI
- Ne pas auditer au-delà de `audit_max_sessions` ; ne pas écrire plus de
  `max_candidates_per_run` drafts
- Terminer par : le chemin du rapport final (**copie utilisateur**
  `~/weekly-reports/weekly-report-latest.md` en premier, puis l'archive
  `runs/current/weekly-report-<date>.md`) et les alertes les plus sévères
- Le rapport est publié automatiquement vers `~/weekly-reports/` (config
  `report_dir` pour changer l'endroit) — ne pas déplacer/copier le rapport ailleurs

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

## Garde-fous de coût

- **JAMAIS modifier la config moteur** (`weekly-telemetry-config.json`) — les
  overrides de fenêtre (lookback_days, anchor) se passent en paramètres des tools
  (`weekly_run`, `weekly_releases`) via les paramètres `lookback_days` et `anchor`
  (voir tableau « Fenêtre du run » ci-dessus)
- **Budget d'itérations** : si un tool échoue (plugin bug, ReferenceError), signaler
  au rapport et passer à l'étape suivante — ne pas passer plus de 3 tours à
  diagnostiquer un échec tool
- **Plugin stale** : si un `ReferenceError` mentionne une variable non définie dans le
  plugin, c'est un module chargé au boot avec du code stale — le fix disque ne s'applique
  pas avant redémarrage. Constater, signaler au rapport, proposer le restart, **stopper
  le diagnostic** (ne pas tenter d'éditer/recharger le plugin mid-session)
- **Mode audit** : quand un choix est ambigu (ex. fenêtre multi-semaines), poser UNE
  seule question à l'utilisateur avec des options, ne pas explorer le code du plugin
  pour deviner la sémantique
