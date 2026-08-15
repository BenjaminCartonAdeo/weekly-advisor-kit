---
description: "Résume le dernier rapport hebdomadaire du pipeline weekly-advisor (reports/weekly-report-*.md le plus récent) — synthèse, alertes, constats, actions."
---

# Dernier rapport hebdomadaire

Trouve le rapport le plus récent dans `weekly-advisor/reports/weekly-report-*.md`
(glob, tri par date décroissante) et résume-le pour une lecture rapide.

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