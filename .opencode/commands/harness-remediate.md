---
description: "Traite le dernier digest harness-eval via la gate déterministe — propositions bornées, aucune application aveugle. Use when a harness digest has to be processed."
---

# Harness Remediate — weekly-advisor

Traite le digest `harness-eval` du run actif via le skill `harness-remediation` (source
des règles et schémas) puis la gate déterministe du plugin weekly-advisor. Écrit des
propositions bornées ; n'applique que les corrections autorisées par la gate — jamais
d'application aveugle.

## Quand utiliser

- Après un `weekly_harness` (digest produit) pour transformer les findings en décisions
  explicites (`apply` / `propose` / `manual` / `blocked`).
- Hors du run `/weekly-review` complet, pour ne rejouer que l'étape 5.5 sur un digest déjà présent.

## Procédure

1. **Résoudre le digest du run actif** : `<output_dir>/runs/current/weekly-harness-digest-<date>.json`
   (répertoire du run actif — `runs/current` est l'alias stable ; jamais un digest d'un
   autre run, jamais reconstruit depuis un message de scanner).
2. **Charger le skill `harness-remediation`** et écrire les propositions bornées dans
   `<output_dir>/runs/current/weekly-harness-remediation-proposals-<date>.json`
   (obligatoire, schema-valid, avant tout appel de la gate — voir skill pour le détail
   du format et la recovery bornée en cas de digest manquant/tronqué).
3. **Appeler la sous-commande `harness-remediate` du plugin** (`proposal_file`, `mode:
   "apply"`) — c'est la gate déterministe, pas la proposition LLM, qui décide au final
   ce qui est réellement appliqué (règle autorisée, remplacement unique, ≤ 1 fichier).
4. Le tool écrit `<output_dir>/runs/current/weekly-harness-remediation-<date>.json`.

## Sortie attendue

- Chemin du digest traité + date du run
- Pour chaque finding : décision (`applied` / `proposed` / `manual` / `blocked` /
  `rolled_back`) et sa justification
- Post-check pour chaque correction réellement appliquée
- Dernière ligne : `HARNESS_REMEDIATE_RC=<0|1|2>` (0 toutes décisions traitées sans
  blocage, 1 au moins une décision en `manual`/`blocked`/`propose`, 2 digest ou
  proposition absent/invalide après la recovery bornée du skill)

## Garde-fous

- Jamais d'application automatique d'une règle `security/*`, même à confiance élevée —
  toujours `manual`/`propose` (voir skill partagé `weekly-safety-guardrails`).
- Jamais d'édition directe hors de la gate : seules les propositions validées sont
  appliquées ; jamais le moteur `weekly-advisor-engine/`, `.opencode/opencode.json`, une
  base SQLite, la CI, ou une cible hors worktree.
- Aucun commit automatique — les corrections appliquées sont listées pour revue humaine.
- Digest ou proposition manquant/tronqué : une seule recovery bornée (`max_retry=1`),
  puis `manual`/`blocked` — jamais de proposition inventée à partir du seul message du
  scanner.
