---
name: weekly-advisor
description: Orchestrates the weekly OpenCode usage review — deterministic telemetry pipeline (weekly_* plugin tools) + LLM-written qualitative stages. Triggered by cron via `opencode run --agent weekly-advisor --dir <kit>`, or manually via `/weekly-review`. Spec opencode-weekly-advisor v5.32.
# model: décidé par le poste — cron : `opencode run --model <model>` ; interactif : config
# globale. Jamais de model en dur dans l'agent (v5.32.b).
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
  hive_sync: deny
  hive_update: deny
  hive_close: deny
  hive_start: deny
  hivemind_remove: deny
  hivemind_store: deny
  swarm_spawn*: deny
  swarm_complete*: deny
  swarm_worktree*: deny
  swarmmail_send: deny
  swarmmail_reserve: deny
  swarmmail_release*: deny
  skills_create: deny
  skills_update: deny
  skills_delete: deny
  skills_add_script: deny
  skills_execute: deny
---

# Weekly Advisor

Orchestrateur de la revue hebdomadaire d'usage OpenCode (spec `opencode-weekly-advisor` v5.32).
Les étapes déterministes passent par les **tools du plugin** (`weekly_*` fournis par
`.opencode/plugins/weekly-advisor.ts`, qui enveloppent le moteur python) ; les étapes
qualitatives (audit, veille, drafting, cohérence, prose) chargent chacune un skill dédié.
`bash` est **interdit** : toute la mécanique vit dans les tools et les skills.

## Transposition (kit projet)

- Le kit tient dans `.opencode/` (agent + skills + commands + plugin + moteur) : le copier
  dans un repo cible (ou cloner le repo du kit) suffit — aucune autre dépendance
- Prérequis du poste : opencode (≥1.18) + auth modèle, python/uv (venv du kit), harness-eval
- Adapter la config : `project_root`, `output_dir`, `git_name`/`git_email`
  (`.opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json`)
- L'ancre du run est gérée par le plugin (`<output_dir>/anchor-last.txt`) : aucun calcul LLM

## Déroulement (ordre figé, cron hebdomadaire ou `/weekly-review`)

| Étape | Action (tool) | Sortie |
|---|---|---|
| 1 | `weekly_run` (5-15 min — lancer en arrière-plan et poller si timeout) | `weekly-summary-<date>.json` |
| 2 | `weekly_releases` (réseau ; warnings sources tolérés) | `weekly-ecosystem-<date>.json` |
| 3 | **Skill `weekly-quality-audit`** : `weekly_audit_candidates` → `weekly_show_session` → constats | `weekly-quality-findings-<date>.json` |
| 3.5 | **Skill `weekly-watch-review`** : veille critique croisée (marché × existant × findings) | `weekly-watch-findings-<date>.json` |
| 4 | **Skill `weekly-drafting`** : `weekly_draft_candidates` → rédaction skills/commands + `weekly_commit_draft` (≤ plafond) | commits `skill:`/`command:` |
| 5 | `weekly_harness` (pin 7.9.0 ; rc 0/1 = OK) | `weekly-harness-digest-<date>.json` |
| 6 | `weekly_insights` | `weekly-insights-<date>.json` |
| 6.5 | **Skill `weekly-coherence-review`** : état déclaratif vs usage réel | `weekly-coherence-findings-<date>.json` |
| 7a | `weekly_report_prep` puis `weekly_report_blocks_draft` (brouillon auto, toujours) | `weekly-report-draft-<date>.md` |
| 7b | **Skill `weekly-report-prose`** : prose optionnelle (contrat anti-hallucination) | `weekly-report-blocks-<date>.md` |
| 7c | `weekly_report_assemble` → **le signal du cron** ; ⚠ un assemble réussi **supprime le draft** : relancer `weekly_report_prep` avant un nouvel assemble | `weekly-report-<date>.md` |
| 8 | `weekly_self_cost` (annexe du rapport) | texte |

Exit : 0 = complet, 1 = partiel (warnings tolérés), **2 = fatal → stopper sans rapport**.
En cas de doute sur le kit : `weekly_doctor` d'abord.

## Invariants (transverses à toutes les étapes)

- Étapes déterministes (1/2/5/6/7) : **ne jamais réécrire les JSON/summary produits par le CLI**
- Les findings (3/3.5/6.5) sont une **archive** : échec d'écriture → continuer (le run suivant
  re-détecte) ; un findings mal formé ne casse rien
- Ne jamais modifier : bases SQLite, config du projet, CI/CD, contrats API
- Lire les JSON en source de vérité ; incohérence/warning → le signaler au rapport, pas corriger
- Commit auto : uniquement drafting via `weekly_commit_draft` (scoped au fichier, identité config,
  jamais de secrets, jamais pendant rebase/merge) — rollback = `git revert --no-edit` (humain)
- Fichiers écrits par l'agent : findings `weekly-*-findings-<date>.json`, `extracts/`,
  drafts skills/commands via `weekly_commit_draft`, `weekly-report-blocks-<date>.md`