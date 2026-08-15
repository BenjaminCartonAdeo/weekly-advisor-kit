---
name: weekly-watch-review
description: Veille critique hebdomadaire (étape 3.5 du weekly-advisor) — croiser l'offre du marché avec l'environnement existant et les findings coûteux, recommandations orientées action.
metadata:
  authored_by: opencode-weekly-advisor
  skill_class: pipeline-step
---

# Weekly Watch Review — étape 3.5

La collecte (`releases`, déterministe) est une matière première. La valeur est ici :
le **regard critique** qui croise l'offre du marché avec les problèmes mesurés du run.

## Entrées

- `weekly-ecosystem-<date>.json` — **TOUS** les items, pas seulement les premiers
- L'environnement existant : catalogue skills/commands/agents (`scan_skill_catalog` du
  summary + `ls .opencode/{skills,commands,agents}` + config `watch`)
- `weekly-quality-findings-<date>.json` (sessions coûteuses du run) et les alertes

## Objectif

Croiser **problèmes mesurés** (findings P3, alertes insights) × **offre du marché** (collecte)
× **existant** (catalogue) pour proposer des améliorations qui réduisent la consommation de
tokens ou améliorent l'expérience. Ne pas se limiter au diff : un outil du marché MIEUX fait
que l'existant n'est pas « nouveau » — il faut le repérer quand même.

## Catégories de recommandation

| Catégorie | Sens |
|---|---|
| `adopt` | Nouveauté pertinente à adopter (skill/plugin/command du marché) |
| `improve-existing` | L'existant (skill/command/agent) est dépassé par une offre → améliorer ou remplacer |
| `token-saver` | Outil qui réduirait la conso des sessions de type X (croiser avec les findings coûteux) |
| `ignore` | Bruit (serveurs MCP abandonnés, listes mortes) — pour mémoire |

## Schéma du fichier findings

```jsonc
{
  "schema_version": 1,
  "findings": [
    {
      "category": "token-saver",
      "severity": "medium",
      "description": "une phrase",
      "evidence_summary": "le LIEN avec l'existant ou le finding coûteux (≤ 200 car., paraphrase)",
      "recommendation": "action concrète",
      "recommendation_type": "watch-adopt | watch-improve | watch-token-saver | watch-ignore",
      "impact_order_of_magnitude": "small | medium | large"
    }
  ]
}
```

## Sécurité (non négociable)

- **Jamais d'installation automatique d'outils externes** — les candidats sont remontés,
  l'humain décide
- Dédupliquer par repo GitHub source ; privilégier les entrées « Official »/« Claimed »
  et les repos actifs
- Les `adopt`/`improve-existing` restent des candidats à revoir — l'écriture d'outils
  externes n'est pas automatisée en v1