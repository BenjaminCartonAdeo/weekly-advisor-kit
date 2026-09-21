---
description: "Audit d'un run cron weekly-advisor — inventaire logs/rapports, recoupement provenance/commit/artefacts et matrice warnings, sans recoller de JSON volumineux."
---

# Cron Audit — weekly-advisor

Enchaîne en une passe l'audit d'un run cron `weekly-advisor` : inventaire logs et rapports, recoupement provenance/commit/artefacts, et matrice des warnings — même flux que la commande de collecte hebdomadaire et les gates du kit, sans recoller de payload JSON volumineux.

## Quand utiliser

- Après un run cron `weekly-advisor` (ou `weekly-review`) pour vérifier que le rapport reflète bien le code courant.
- Quand un log signale `exit=0` mais masque des erreurs de compatibilité, HTML manquant ou `rc` incohérent.
- Pour produire une matrice warnings/remédiation avant un fix.

## Comment invoquer

- **OpenCode** : `/cron-audit` ou `/cron-audit <run_dir>` — la commande résout `<output_dir>/runs/current/` puis fallback sur la dernière archive `reports/runs/<date>-*/`.
- **Claude Code / Codex / Cursor** : même fichier sous `.opencode/commands/cron-audit.md` (ou miroir `.claude/commands/` si harnais multiple) — invoquer `cron-audit` depuis la palette commandes.
- Variable : `/home/benjamin/log/weekly` peut porter un chemin de run (`reports/runs/2026-09-09-*/`) ou un filtre (`--since 7d`).

## Procédure

1. **Inventaire run/log** (sans lecture intégrale) :
   - Lister `<output_dir>/runs/current/` et `reports/runs/` (glob, tri décroissant).
   - Lister les logs cron (`reports/logs/` ou `~/log/weekly/` si configuré) — `glob` par pattern `weekly-advisor-*.log`.
   - Extraire par grep ciblé : `ERROR|WARN|FAILED|STEP|exit|rc=|provenance|commit|HTML` puis lectures par plages `offset/limit` autour des phases en échec.

2. **Recoupement provenance & gates** :
   - Lire `weekly-summary-<date>.json` → `run_provenance` (repository_path, branch, commit_sha, working_tree_dirty, pipeline_version).
   - Comparer au `HEAD` courant (`git rev-parse HEAD`, `git diff --stat`) — noter divergence `built_at_commit` vs `HEAD`.
   - Vérifier artefacts requis : `weekly-report-<date>.md`, `reports/html/weekly-report-latest.html`, `weekly-timings-<date>.json`, `weekly-harness-digest-<date>.json` — absent/vide → warning bloquant.

3. **Matrice warnings et plan** :
   - Agréger `warnings` du summary, `findings` de `weekly-quality-findings-<date>.json`, `weekly-watch-findings-<date>.json`, `weekly-harness-digest-<date>.json`.
   - Classer par sévérité (`high` > `medium` > `low`) et par catégorie (`security/mcp-tool-poisoning`, `unbounded-delegation`, `memory-write-unscoped`, `budget`, `provenance-drift`).
   - Produire matrice : `| source | règle | observé | seuil | action |` + plan remédiation (fichier/ligne, gate à corriger).

## Sortie attendue

- Chemin du run audité + date
- Statut provenance : `OK` ou `drift (built_at vs HEAD, N commits de retard)`
- Matrice warnings (au plus 20 lignes, une ligne par finding)
- Plan remédiation en 3–5 items actionnables
- Dernière ligne : `CRON_AUDIT_RC=<0|1|2>` (0 clean, 1 warnings, 2 drift/artefact manquant bloquant)

## Points d'attention

- Sources closes : seuls le run audité et ses JSON `weekly-*.json` — jamais de mémoire inter-run non vérifiée.
- Ne jamais transporter de gros JSON via argv : passer par fichier temporaire si besoin.
- Ne jamais inventer un finding, un commit ou un chemin hors worktree — hors périmètre → constat `environment-change` report-only.
- Budget d'itérations : au plus 3 tours de diagnostic par finding ; au-delà, consigner et continuer.

## Garde-fous

- `rc != 0` reste en échec même avec sortie partielle — conserver `rc` agrégé.
- Findings sécurité `mcp-tool-poisoning`, `unbounded-delegation`, `memory-write-unscoped` : bloquants, pas d'auto-correction.
- Chemin hors worktree (`external-directory`) → record `{status:"report-only", report_only:true, category:"external-permission-refusal"}` — ne pas lire/écrire hors projet.
