---
name: weekly-safety-guardrails
description: Garde-fous partagés (refus external-directory, environment-change, retry borné, IDs sécurité bloquants) — jamais chargé seul.
metadata:
  authored_by: opencode-weekly-advisor
  skill_class: shared-partial
---

# Weekly Safety Guardrails — partagé (single-source)

Règles de sécurité et de recovery communes aux branches T/V/H/A/D/I/C. Les skills de branche y renvoient au lieu de dupliquer le bloc.

## Refus external-directory / hors worktree (report-only, nonblocking)

Toute demande de permission **external-directory** ou toute cible out-of-tree produit uniquement :

```json
{ "status": "report-only", "report_only": true, "category": "external-permission-refusal" }
```

Ne pas lire, écrire, déplacer, escalader ou convertir en fatalité. Reprendre seulement dans le worktree autorisé. Le refus hors worktree reste `rc: 0` ; une permission refusée **dans** le worktree reste comptable (`rc: 1` + warning).

## Environment-change (report-only)

Constat dont la cible est hors `project_root` ou non écrasable → `environment-change`, report-only. Rien n'est écrit dans le projet courant, pas de draft, pas de lecture hors périmètre.

## Bounded recovery (anti-boucle)

Entrée manquante, tronquée ou invalide → **one bounded retry** (`max_retry=1`, relecture ciblée des seules entrées disponibles), puis signaler l'échec (`blocked`/`manual` + warning) sans respawn loop, hang, attente indéfinie, ni finding/proposal inventé. Sans source lisible, aucun artefact n'est écrit.

## IDs sécurité bloquants (non négociable)

`mcp-tool-poisoning`, `unbounded-delegation`, `memory-write-unscoped` (et `security/*` en remédiation) : arrêt de l'action concernée + signalement au coordinateur. Aucune auto-correction, écriture ou délégation implicite. En harness, jamais d'apply auto sur `security/*` même avec `confidence=high`.
