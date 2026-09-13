# Schémas des artefacts JSON

Ces fichiers documentent la structure de tête des artefacts produits par le
moteur. Ils sont **documentaires** : ils décrivent les clés principales
observées sur des artefacts réels d'un run complet, avec
`additionalProperties: true` — les clés imbriquées non listées restent permises
et ne cassent aucune validation.

Chaque artefact porte un champ entier `schema_version` en tête. La compatibilité
entre versions se lit dans l'historique git ; ces schémas décrivent la version
courante.

| Schéma | Producteur | Étape |
| ------ | ---------- | ----- |
| [weekly-summary](weekly-summary.schema.json) | `run` | 1 |
| [weekly-ecosystem](weekly-ecosystem.schema.json) | `releases` | 2 |
| [watch-candidates (enrichis)](watch-candidates.schema.json) | `watch-distill` puis `watch-context` | 2.2 / 2.5 |
| [watch-memory-digest](watch-memory-digest.schema.json) | `watch-distill` | 2.2 |
| [weekly-watch-findings](weekly-watch-findings.schema.json) | `watch-validate` (bruts : `weekly-watch-review`) | 3.5 / 3.6 |
| [weekly-audit-candidates](weekly-audit-candidates.schema.json) | `audit-candidates` | 3 |
| [weekly-harness-digest](weekly-harness-digest.schema.json) | `harness` | 5 |
| [weekly-insights](weekly-insights.schema.json) | `insights` | 6 |
| [weekly-draft-candidates](weekly-draft-candidates.schema.json) | `draft-candidates` | 4 |
| [skill-curate](skill-curate.schema.json) | `skill-curate` | 6.5 |

Le rapport final (`weekly-report-<date>.md`) suit la structure de sections
décrite dans [`doc/spec/06-sorties.md`](../../spec/06-sorties.md) ; la page HTML
est une projection interactive best-effort du même contenu.
