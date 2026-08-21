---
description: "Résume le dernier rapport hebdomadaire du pipeline weekly-advisor (rapport HTML <project_root>/reports/html/weekly-report-latest.html, sinon l'archive runs/*/ la plus récente) — synthèse, alertes, constats, actions."
---

# Dernier rapport hebdomadaire

Trouve le rapport le plus récent — en priorité le **rapport HTML autonome**
`<project_root>/reports/html/weekly-report-latest.html` (config `html_report_dir`
du moteur, `.opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json`),
sinon la dernière des archives `runs/*/weekly-report-*.md` (glob, tri par date
décroissante, `legacy/` inclus) — et résume-le pour une lecture rapide.

## Sortie attendue

1. **Chemin du rapport** + date du run
2. **Synthèse** : coût, tokens, cache hit, sessions comptées
3. **Alertes** : liste avec sévérité, règle, seuil et observé
4. **Constats qualitatifs** : les findings high/medium (catégories, une ligne chacun)
5. **Actions du pipeline** : commits auto-rédigés, drafts créés, findings de veille
6. **Cohérence** : tags action (delete/shrink/merge/recalibrate/reference) les plus
   prioritaires
7. Si le rapport n'existe pas (aucun run) : le dire explicitement, ne rien inventer

## Règles

- Sources closes : seuls le rapport et les JSON du même jour sont lus — jamais de mémoire
- Les nombres proviennent du rapport (rendu par le template) : les citer tels quels,
  sans les recalculer
- Si le rapport date de plus de 10 jours : le mentionner (« rapport périmé de N jours »)
- Ne pas confondre rapport HTML et archive : le HTML (`weekly-report-latest.html`) est le
  signal canonique du dernier run complet ; les archives `runs/<date>-<uuid8>/` sont
  l'historique