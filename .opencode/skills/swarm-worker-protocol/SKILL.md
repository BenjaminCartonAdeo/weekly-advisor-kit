---
name: swarm-worker-protocol
description: Survival checklist for any agent spawned as a swarm worker (subtask in a swarm/swarmx mission). Load FIRST, before anything else, when your task prompt says to execute a swarm subtask. Covers swarmmail init, file reservations, progress reporting, cost constraints (Worker Cost Guards), and the swarm_complete closing protocol.
---

# Swarm Worker Protocol

Tu es un agent worker dans une mission swarm. Ton coordinateur décompose, spawn,
review — il n'exécute jamais le travail lui-même. Toi si.

## Checklist de survie (9 étapes — DANS L'ORDRE)

1. **`swarmmail_init(project_path)`** — PREMIER appel, avant tout le reste.
2. **`hivemind_find(query="<mots-clés de ta sous-tâche>", limit=5)`** — vérifier les
   apprentissages passés ; un agent antérieur a peut-être déjà résolu ça.
3. **`skills_list()` / `skills_use()`** — charger les skills pertinentes pour ton
   domaine (ex. `file-search` pour localiser du code).
4. **`swarmmail_reserve(paths=[...])`** — TOI seul réserves tes fichiers, jamais le
   coordinateur.
5. **Faire le travail** — lire par plages (`offset`/`limit`) ou grep ciblé,
   implémenter, vérifier (lint/tests scoped).
6. **`swarm_progress()`** — reporter à 25/50/75 %.
7. **Bloqué ?** → `swarm_progress(status="blocked")` + message au coordinateur :
   ```
   swarmmail_send(to=["coordinator"], subject="BLOCKED: <bead-id>", body="<besoin>",
   importance="high")
   hive_update(id="<bead-id>", status="blocked")
   ```
8. **`hivemind_store(information, tags=...)`** — stocker ce que tu as appris
   (patterns, pièges, contraintes découvertes).
9. **`swarm_complete(...)`** — PAS `hive_close`. Seul moyen de clore : libère les
    réservations et enregistre l'outcome. Échoue sans l'étape 1.
   - **Summary OBLIGATOIRE non-vide** : jamais de `swarm_complete` avec un summary vide ou
     absent. Un `task_result` vide (state=completed mais corps vide) est un **échec silencieux**,
     pas un succès — le coordinateur doit le traiter comme un crash (re-spawn / rc=1), jamais
     comme une livraison (incident swarm 24→26/08 : re-spawns en boucle, coût doublé).

## Non négociables

- Étape 1 OBLIGATOIRE — `swarm_complete` échoue sans init.
- Étape 2 fait gagner du temps — ne pas la sauter.
- Étape 4 prévient les conflits d'édition — workers réservent, pas le coordinateur.
- Étape 6 prévient l'échec silencieux — un worker muet est supposé mort.

## CONTRAINTES DE COÛT (Worker Cost Guards v5.30 — non négociables)

- **Périmètre** : ne travailler QUE sur les fichiers listés dans ton prompt ;
  hors périmètre = à signaler au coordinateur, pas à explorer.
- **Lectures** : par plages (offset/limit) ou grep ciblé ; jamais de relecture
  intégrale d'un fichier déjà lu (le brief d'exploration du coordinateur fait foi).
- **Tests** : exécuter uniquement les classes de test des modules touchés ; jamais la
  suite globale (`mvnw test` / `npm test` complets interdits).
- **Itérations** : max 3 passes de correction avant restitution ; ne pas relancer la
  même revue sur un code qui change peu.
- Si le coût estimé de ta sous-tâche dépasse le budget indiqué dans ton prompt →
  t'arrêter et restituer l'état partiel avec les blocages.

## Focus

- Ne modifier QUE tes fichiers assignés.
- Ne pas corriger le code des autres agents — coordonner via le coordinateur.
- Signaler tout changement de périmètre AVANT de l'étendre.

Commence par relire intégralement ton prompt de tâche, puis exécute l'étape 1.
