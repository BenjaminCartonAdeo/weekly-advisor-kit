---
description: Analyse le dernier digest harness-eval et traite ses propositions via la gate déterministe.
agent: harness-remediator
---

Charge le skill `harness-remediation`, traite le digest harness daté de la dernière
revue et écris le résultat dans `reports/current/weekly-harness-remediation-<date>.json` (répertoire du run actif).
N'applique que les corrections autorisées par les gates déterministes ; explique
clairement toutes les corrections proposées ou bloquées.
