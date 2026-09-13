# Spécification fonctionnelle — Revue hebdomadaire des agents de code

Ce dossier contient la spécification fonctionnelle du produit, découpée en huit
documents. Elle décrit **ce que le produit doit faire**, indépendamment de toute
technologie : aucun langage, aucune infrastructure logicielle, aucune solution de
stockage, aucune unité de code interne, aucun outil interne, aucune version de
produit.

L'objectif de cette spécification est de permettre de reconstruire le produit
*from scratch* sur une architecture totalement différente sans perdre une seule
fonctionnalité. Les valeurs chiffrées (seuils, plafonds, pondérations, priorités)
font partie intégrante du contrat fonctionnel.

## Sommaire

| #  | Document                                                   | Contenu                                                              |
| -- | ---------------------------------------------------------- | -------------------------------------------------------------------- |
| 01 | [Vision et périmètre](01-vision-perimetre.md)              | Objet, périmètre (ce que le produit fait / ne fait pas), cycle de vie |
| 02 | [Vocabulaire et acteurs](02-vocabulaire-acteurs.md)        | Termes normalisés, acteurs, contrat d'invocation                      |
| 03 | [Capacités](03-capacites.md)                               | Capacités C1–C18, règles détaillées par capacité                      |
| 04 | [Pipeline](04-pipeline.md)                                 | Étapes 0–8, catégories de constats, statuts 0/1/2                     |
| 05 | [Données](05-donnees.md)                                   | Données ingérées, données écrites, mémoire                            |
| 06 | [Sorties](06-sorties.md)                                   | Résumé hebdomadaire, écosystème, veille, digest, insights, rapport    |
| 07 | [Règles transverses](07-regles-transverses.md)             | Déterminisme, ancre, fail-soft, portabilité, gates, garde-fous        |
| 08 | [Critères d'acceptation](08-criteres-acceptation.md)       | Checklist de reconstruction A–F, testable par un humain                |

La conformité d'une reconstruction se vérifie avec la checklist du document 08.
L'implémentation de référence (choix techniques, architecture logicielle) est
décrite dans [`doc/architecture/`](../architecture/README.md) : elle ne fait pas
partie du contrat fonctionnel et peut être remplacée dans son intégralité.
