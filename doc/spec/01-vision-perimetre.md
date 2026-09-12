# Spécification fonctionnelle — Revue hebdomadaire des agents de code

Référentiel des **règles fonctionnelles** du produit « revue hebdomadaire » : ce que le produit
fait, pour qui, avec quelles entrées, quelles sorties, quels seuils et quelles décisions.
Ce document est volontairement **indépendant de toute technologie** : aucun langage, aucune
infrastructure logicielle, aucune solution de stockage, aucune unité de code interne, aucun outil
interne, aucune version de produit. Son objectif est de permettre de **reconstruire le produit
from scratch sur une architecture totalement différente sans perdre une seule fonctionnalité**.

Les valeurs chiffrées (seuils, plafonds, durées, taux, priorités, quotas, formules) font partie
intégrante du contrat : elles doivent survivre telles quelles à toute reconstruction.

---

# 1. Objet & périmètre

## 1.1 Ce que fait le produit

Le produit réalise, à cadence hebdomadaire, une **revue complète de l'activité des agents de
code** sur une instance locale, pour produire un rapport unique qui :

- agrège et chiffre l'usage (tokens, coûts, cache, modèles) des sessions de la semaine ;
- détecte les dérives coûteuses et les opportunités d'économies (boucles, invalidation de
  cache, sous-emploi de l'environnement, mauvais choix de modèle) ;
- surveille l'écosystème du marché (nouveaux outils, plugins, serveurs d'outils, évolutions du
  cœur) et le confronte à l'environnement existant ;
- contrôle la santé de la configuration de l'environnement lui-même ;
- rédige automatiquement des documents de compétence réutilisables (skills, commands) à partir
  des patterns observés ;
- produit des alertes, des tendances et des recommandations d'action ;
- assemble un rapport final lisible en 5 minutes, dont tous les chiffres proviennent du calcul
  et non d'une interprétation.

Le produit **audite et documente** ; il **ne développe pas** : il n'édite jamais le code
applicatif d'un projet, n'installe jamais d'outil externe de son propre chef, et ne modifie
l'environnement que par la création (et, à terme, l'amélioration) de documents de compétence
dans le périmètre explicitement autorisé.

## 1.2 Ce que le produit ne fait pas

- Il n'analyse pas la qualité des prompts en temps réel — il le fait en différé, une fois par
  semaine, sur des sessions terminées.
- Il ne modifie jamais la configuration des harnais d'agents qu'il audite, sauf dans les
  emplacements de drafting explicitement autorisés.
- Il n'exécute pas d'installation automatique d'outils externes : il remonte des
  recommandations, l'humain décide.
- Il ne réécrit pas ses propres consignes ni son propre code à partir de l'audit (hors contrôle
  de santé déclaratif de l'environnement).
- Il ne réalise **pas** d'exploration d'architecture : aucune analyse de graphe de code ni aucun
  artefact d'architecture n'apparaît dans la revue ou le rapport.

## 1.3 Cycle de vie d'une revue

Une revue se déroule en une séquence d'étapes numérotées, chacune produisant une sortie
consommée par les étapes suivantes. Le produit garantit qu'une revue est **déterministe** pour
un même jeu d'entrées et une même fenêtre temporelle. Une revue peut se terminer en trois états
(voir §5.9) : **succès complet**, **succès partiel** (au moins une étape dégradée, le rapport
est quand même produit), ou **échec bloquant** (une étape fatale empêche la production du
rapport).

---
