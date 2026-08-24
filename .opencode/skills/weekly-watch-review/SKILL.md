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

Tous les artefacts du run vivent dans le répertoire du run actif : **utiliser le
chemin absolu retourné par le tool précédent** ; jamais de Glob depuis la racine
sur l'arbre `reports/`.

## Entrées

- `weekly-ecosystem-<date>.json` — **TOUS** les items, pas seulement les premiers
- `weekly-watch-context-<date>.json` — inventaire déterministe du worktree et crosswalk
  `existing_state`/`match` pour chaque item de marché (produit par
  la sous-commande `watch-context` du plugin)
- L'environnement existant : le contexte déterministe d'abord, puis le catalogue
  skills/commands/agents (`scan_skill_catalog` du summary + `ls .opencode/{skills,commands,agents}`)
  uniquement pour compléter une information manquante
- `weekly-quality-findings-<date>.json` (sessions coûteuses du run) et les alertes

## Sortie de l'étape

Le skill écrit uniquement le brouillon LLM dans `weekly-watch-findings-raw-<date>.json`,
à côté des artefacts du run actif (répertoire du chemin absolu retourné par le
tool précédent).

Il ne écrit jamais directement `weekly-watch-findings-<date>.json` : ce fichier est
produit ensuite par la sous-commande `watch-validate` du plugin weekly-advisor, qui
applique les garde-fous déterministes.

Pour chaque recommandation marché (`adopt`, `improve-existing` ou `token-saver`), le
finding brut doit contenir un `subject` avec au moins un des champs suivants :

```jsonc
{
  "subject": {
    "name": "@vendor/plugin",
    "npm_package": "@vendor/plugin",
    "repo_url": "https://github.com/vendor/plugin"
  }
}
```

Le sujet doit être pris dans `weekly-watch-context.market_matches`, jamais inventé.
`capability_state=covered` interdit de présenter l'élément comme une nouveauté ;
`capability_state=unknown` autorise uniquement une comparaison argumentée, pas une
preuve d'absence.

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

Le validator peut transformer un finding en `verify-existing` ou `improve-existing` lorsque
le sujet est déjà déclaré ou observé. Un finding `adopt` n'est valide que lorsque
`existing_state` vaut `absent` dans le contexte déterministe.

Le champ `existing_state` du contexte est une garde déterministe : un item `declared`
est déjà déclaré dans le plugin config du projet et ne doit pas être proposé comme
`adopt`; `observed` désigne un fichier/skill/command/agent déjà présent dans le
worktree. `absent` et `unknown` restent des entrées à examiner, jamais des preuves
d'installation.

Le fichier final peut également porter `repair-existing` lorsqu'un état défaillant est
fourni par un contexte runtime. Le brut LLM reste limité aux quatre catégories ci-dessus.

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
      "subject": {
        "name": "nom canonique",
        "npm_package": "package npm ou null",
        "repo_url": "URL canonique ou null"
      },
      "recommendation_type": "watch-adopt | watch-improve | watch-token-saver | watch-ignore",
      "impact_order_of_magnitude": "small | medium | large"
    }
  ]
}
```

## Sécurité (non négociable)

- **Jamais d'installation automatique d'outils externes** — les candidats sont remontés,
  l'humain décide
- Le contexte déterministe est la source de vérité pour l'existant ; ne jamais déduire
  l'absence d'un plugin à partir du seul texte d'une description
- Dédupliquer par repo GitHub source ; privilégier les entrées « Official »/« Claimed »
  et les repos actifs
- Les `adopt`/`improve-existing` restent des candidats à revoir — l'écriture d'outils
  externes n'est pas automatisée en v1
