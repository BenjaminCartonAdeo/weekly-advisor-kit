# 2. Vocabulaire métier

| Terme | Définition |
|---|---|
| **Revue (hebdomadaire)** | Le cycle complet de collecte, analyse, veille, drafting et rapport, exécuté à cadence régulière sur une fenêtre glissante. |
| **Session** | Unité d'activité d'un agent de code : une conversation avec ses métadonnées (titre, modèle, agent, coûts, tokens) et son transcript (tours de dialogue, appels d'outils, étapes). |
| **Harnais** | Écosystème d'agent de code supporté dont le produit lit les sessions. Les harnais supportés sont : **OpenCode**, **Claude Code**, **Copilot VS Code**, **Codex**. Leurs noms sont du vocabulaire métier : le produit opère sur eux. Les **providers de télémétrie** (sources de sessions lues) sont OpenCode, Claude Code et Copilot VS Code ; **Codex n'est jamais un provider de télémétrie** — cible de drafting seule (`.agents/`), le produit ne lit jamais ses sessions. |
| **Identifiant canonique** | Identifiant unique d'une session sur toutes les sources, de la forme `<harnais>:<identifiant local>`. Un identifiant brut (sans préfixe de harnais) reste accepté et est résolu par la première source qui le possède. |
| **Run** | Exécution d'une revue ; chaque run est associé à une **ancre** temporelle et à une fenêtre glissante. |
| **Fenêtre glissante** | Intervalle temporel analysé : `[instant de référence − (nombre de jours × 24 h), instant de référence]`. Par défaut 7 jours. |
| **Ancre** | Instant de référence d'un run, en temps universel. Toutes les étapes d'un même run partagent la même ancre ; elle est dérivée de façon stable et surchargeable. |
| **Candidat** | Session sélectionnée pour un examen qualitatif, ou constat retenu pour un drafting. |
| **Constat (finding)** | Résultat structuré d'un examen : catégorie, sévérité, description, preuve résumée, recommandation. |
| **Harnais cible de drafting** | Harnais unique vers lequel les documents de compétence générés sont projetés dans un projet donné. |
| **Baseline** | Référence figée (instantané d'un état) utilisée pour mesurer un écart ; capturée une fois, jamais réécrite par les runs suivants. |
| **Surface de remédiation** | Périmètre sur lequel des corrections déclaratives peuvent s'appliquer, décidé de façon déterministe. |
| **Coût estimé** | Ordre de grandeur de coût d'une session sans prix enregistré, calculé par taux — distinct du coût facturé enregistré. |
| **Donnée structurée sérialisable** | Toute sortie d'étape : un document texte structuré (champs et sémantique définis dans ce document), indépendant de tout format de stockage. |
| **Cible de drafting** | Emplacement de projection des documents de compétence (skills, commands) pour un harnais donné. |
| **Snooze (ignored_findings)** | Liste manuelle d'identifiants de constats à ne plus proposer ; éditée dans la configuration, jamais maintenue automatiquement. |
| **Digest** | Extraction bornée d'une sortie volumineuse, destinée à alimenter une étape suivante sans excès de volume. |
| **Observation d'architecture** | Projection **en lecture seule** de faits d'environnement (compteurs d'états déclaré/observé/absent/inconnu, disponibilité et validité de la configuration, compteurs d'inventaire, périmètre de harnais). Elle n'infère aucune intention, ne propose ni n'applique aucun changement ; utilisée pour détecter une dérive d'architecture/configuration. |
| **Vue de configuration** | Regroupement thématique **en lecture seule** de la configuration (sources, stockage, coûts, curation), dérivé de la configuration persistée. Une vue ne modifie jamais la configuration ; la forme persistée (plate) reste inchangée, rétro-compatible, aucune migration imposée. |

---

# 3. Acteurs & consommateurs

| Acteur | Rôle | Interactions |
|---|---|---|
| **Orchestrateur agent** | Exécute la revue de bout en bout : porte l'ordre figé des étapes, fait le travail qualitatif (analyse des transcripts, veille critique, rédaction de la prose), appelle les capacités déterministes. | Reçoit les sorties structurées de chaque étape ; écrit le brouillon qualitatif et le rapport. |
| **Humain** | Destinataire final du rapport ; prend les décisions d'installation, de retrait, de fusion, de revert. | Lit le rapport (page interactive et archive), revoit les documents auto-rédigés, édite la configuration (snooze, seuils), déclenche manuellement une revue. |
| **Planificateur horaire** | Déclenche la revue à cadence fixe, hors périmètre du produit mais soumis à un contrat d'invocation. | Lance l'orchestrateur agent avec la racine du projet, le modèle, et capture la journalisation ; alerte si le rapport attendu est absent. |

**Contrat d'invocation (planificateur)**
- L'invocation transmet à l'orchestrateur : la **racine du projet** audité (celle qui contient la
  configuration de l'environnement), le **modèle** à utiliser (décision du déploiement, jamais
  figée dans le produit), et le texte de déclenchement de la revue.
- L'exécution est **non interactive** : aucune commande ne doit ouvrir d'éditeur ni attendre une
  saisie.
- La journalisation (sortie standard et erreur) est capturée par le planificateur : en cas
  d'échec, le log explique *pourquoi*, le rapport absent dit *que* le run a échoué.
- Le **rapport final est le signal** : la présence du rapport final marque la fin d'un run
  réussi ou partiel ; son absence à la fin de l'exécution est un échec (y compris un arrêt brutal
  de l'orchestrateur). Le planificateur alerte sur l'absence de rapport.
- Limite de couverture assumée : « rapport absent = échec » ne vaut que pour les exécutions
  réellement lancées. Si le planificateur lui-même ne tourne pas, aucune alerte locale n'est
  possible ; une sonde externe (hors périmètre) est recommandée comme seul maillon indépendant
  de l'exécution locale.
- La racine du projet est **transmise, jamais devinée** : tous les chemins dérivent de cette
  racine. Le critère d'installation de la racine est la présence du répertoire de configuration
  de l'environnement ; son absence est un **échec d'installation bloquant**.

---
