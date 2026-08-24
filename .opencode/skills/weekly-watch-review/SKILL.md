---
name: weekly-watch-review
description: Veille critique hebdomadaire (étape 3.5 du weekly-advisor) — croiser les fiches candidates distillées avec l'état réel du projet et les findings coûteux, recommandations orientées action.
metadata:
  authored_by: opencode-weekly-advisor
  skill_class: pipeline-step
---

# Weekly Watch Review — étape 3.5

La distillation (étape 2.2, déterministe) réduit l'écosystème complet à ~30 fiches
scorées et filtrées sécurité. La valeur est ici : le **regard critique** qui croise
ces fiches avec l'état réel du projet et les problèmes mesurés du run.

Tous les artefacts du run vivent dans le répertoire du run actif : **utiliser le
chemin absolu retourné par le tool précédent** ; jamais de Glob depuis la racine
sur l'arbre `reports/`.

## 1. Entrées

| Fichier | Taille | Rôle |
|---|---|---|
| `watch-candidates-enriched-<date>.json` | ~20 KB | fiches candidates × état local (`existing_state`, `market_match`, `local_relevance_hints`) + bande `residual` sous cutoff |
| `watch-memory-digest-<date>.json` | ~3 KB | mémoire inter-run bornée : `recently_ignored [{id, week, note}]` (≤20), `previously_recommended []` (≤30), `recurrents []` (occurrences ≥3) |
| `weekly-quality-findings-<date>.json` | variable | sessions coûteuses du run (+ alertes insights) pour croiser coût mesuré ↔ recommandation |

Une fiche candidate porte exactement : `id, name, sources[], score {total, breakdown},
security {verdict, reason}, summary (≤200 car.), signature {version, published_at},
existing_state ∈ {absent, declared, observed, unknown}, market_match,
local_relevance_hints[]`. Les items bloqués sécurité ne sont **jamais** dans ce
fichier (garde amont — annexe du rapport final seulement).

### Fallback legacy (repli documenté)

Si `watch-candidates-enriched-<date>.json` est absent ou que son `mode` ≠ `enriched`
(distill en échec ou snapshot invalide), revenir au comportement v6.1 : lire
`weekly-ecosystem-<date>.json` (**TOUS** les items) + `weekly-watch-context-<date>.json`
et poursuivre aux sections 3 à 5. Ne jamais reconstruire soi-même les fiches.

## 2. Phase 0 conditionnelle — filet B

Déclenchée seulement si **les deux** conditions sont vraies :

- nombre de fiches < `min_candidates` (config `watch_distill.min_candidates`, défaut 20) ;
- `"residual"` non vide dans l'enriched (entrées compactes `{id, name, description, score_total}`, plafonnées à 50).

Dans ce cas, trier les entrées résiduelles sur cette forme ultra-compacte et garder
**≤ 10 ids pertinents**. Consigner la décision en tête du findings raw :

```jsonc
{
  "filet": {
    "kept": ["npm:foo-bar", "gh:vendor/baz"],
    "dropped_reasons": { "url:qux": "hors périmètre", "npm:noisy": "déjà ignoré semaine 34" }
  }
}
```

Règles du filet :

- un finding peut alors porter un id `kept` du residual comme subject — valide car
  l'item est présent dans `market_matches` du contexte, la validation 3.6 passe ;
- aucun re-calcul ni re-appel des étapes amont (distill, watch-context) : le filet
  est un triage local, lecture seule ;
- le filet n'ajoute que des sujets possibles, il n'en retire jamais.

Sinon (fiches ≥ seuil, ou residual vide) : phase 0 sautée, zéro token, aucun champ `filet`.

## 3. Catégories de recommandation

| Catégorie | Condition | Sens |
|---|---|---|
| `install-new` | uniquement `existing_state=absent` | nouveauté pertinente à installer (ex-catégorie `adopt`) |
| `improve-existing` | doit nommer une cible locale via `target_local` | l'offre marché améliore ou remplace une capacité locale existante ; privilégiée quand `local_relevance_hints` non vide |
| `ignore` | bruit | serveurs MCP abandonnés, listes mortes — pour mémoire |

Chaque finding porte **obligatoirement** `"token_impact": "high|medium|low"` —
l'estimation de gain de tokens remplace l'ex-catégorie `token-saver` (justifier
`high` par un croisement explicite avec les findings coûteux).

Gardes déterministes à respecter :

- `existing_state=declared` : déjà déclaré dans la config plugin du projet → jamais `install-new` ;
- `observed` : déjà présent dans le worktree → `improve-existing` si pertinent ;
- `capability_state=covered` : capacité déjà couverte localement → ne jamais présenter comme nouveauté ;
- `unknown`/`absent` : à examiner, jamais une preuve d'installation ou d'absence.

Mémoire inter-run : un id présent dans `recently_ignored` sans fait nouveau → `ignore` ;
un id dans `previously_recommended` ou `recurrents` exige une justification renforcée, sinon `ignore`.

## 4. Schéma du fichier findings raw

```jsonc
{
  "schema_version": 1,
  "filet": { "kept": [], "dropped_reasons": {} },
  "findings": [
    {
      "category": "install-new",
      "severity": "medium",
      "token_impact": "high",
      "target_local": null,
      "description": "une phrase",
      "evidence_summary": "le LIEN avec l'existant ou le finding coûteux (≤200 car., paraphrase)",
      "recommendation": "action concrète",
      "subject": {
        "name": "@vendor/plugin",
        "npm_package": "@vendor/plugin",
        "repo_url": "https://github.com/vendor/plugin"
      },
      "recommendation_type": "watch-install-new | watch-improve-existing | watch-ignore",
      "impact_order_of_magnitude": "small | medium | large"
    }
  ]
}
```

- `subject` : identique v6.1 — copié depuis la fiche ; pour un id `kept` du residual,
  reprendre `name` de l'entrée residual et compléter `npm_package`/`repo_url` depuis
  l'entrée correspondante de `weekly-watch-context-<date>.json` (`market_matches`).
  **Jamais inventé.**
- `target_local` : nom d'un item existant de l'inventaire local (skill/command/agent/
  plugin — visible dans `local_relevance_hints` ou le contexte). Obligatoire en
  `improve-existing`, `null` sinon. Une cible absente de l'inventaire sera coercie
  en `install-new` par la validation 3.6.
- fiche `security.verdict=suspicious` → mention de risque **OBLIGATOIRE** dans
  `evidence_summary` (citer `security.reason`).
- La validation 3.6 peut transformer un finding (`verify-existing`, `improve-existing`,
  `install-new`, `repair-existing`) selon l'état prouvé par le contexte ; le brut LLM
  reste limité aux trois catégories ci-dessus.

## 5. Objectif

Croiser **problèmes mesurés** × **fiches marché** × **existant local** pour proposer
des recommandations qui réduisent la consommation de tokens ou améliorent l'expérience.
Ne pas se limiter au diff : un outil du marché MIEUX fait que l'existant n'est pas
« nouveau » — c'est un `improve-existing`, à repérer quand même.

## 6. Sortie de l'étape

Le skill écrit uniquement le brouillon LLM dans `weekly-watch-findings-raw-<date>.json`,
à côté des artefacts du run actif (répertoire du chemin absolu retourné par le
tool précédent).

Il n'écrit jamais directement `weekly-watch-findings-<date>.json` : ce fichier est
produit ensuite par la sous-commande `watch-validate` du plugin weekly-advisor, qui
applique les garde-fous déterministes (coercitions, writer mémoire, annexe sécurité).

## 7. Sécurité (non négociable)

- **Jamais d'installation automatique d'outils externes** — les candidats sont remontés,
  l'humain décide
- Les champs `subject.name`/`npm_package`/`repo_url` sont pris des fiches ou du contexte,
  **jamais inventés**
- Le contexte déterministe est la source de vérité pour l'existant ; ne jamais déduire
  l'absence d'un plugin à partir du seul texte d'une description
- Dédupliquer par repo GitHub source ; privilégier les entrées « Official »/« Claimed »
  et les repos actifs
- Les items bloqués sécurité sont exclus amont (annexe rapport seulement) — ne pas
  chercher à les réintroduire ; une fiche `suspicious` garde sa mention de risque
- Les `install-new`/`improve-existing` restent des candidats à revoir — l'écriture
  d'outils externes n'est pas automatisée
