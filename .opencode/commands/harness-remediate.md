---
description: "Traite le dernier digest harness-eval via la gate déterministe — propositions bornées, aucune application aveugle. Use when a harness digest has to be processed."
---

Charge le skill `harness-remediation` (source des règles et schémas), traite le digest
harness daté de la dernière revue et écrit le résultat dans
`<output_dir>/runs/current/weekly-harness-remediation-<date>.json` (répertoire du run actif).
N'applique que les corrections autorisées par les gates déterministes ; explique
clairement toutes les corrections proposées ou bloquées.
