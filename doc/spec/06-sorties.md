# 7. Sorties observables

Chaque sortie est décrite par son **contenu et sa structure de données** (champs et sémantique),
jamais par un nom de fichier ni un format imposé. Toutes les sorties sont des **données
structurées sérialisables** dont les champs suivent les définitions ci-dessous.

## 7.1 Résumé hebdo (C1)

Champs de tête : version de schéma, période (`début`, `fin` — fin = instant de référence), instant
de génération.

- **Totaux** : nombre de sessions racines (les enfants sont comptés dans les sous-agents), tokens
  totaux, coût total (racines + enfants fusionnés une seule fois), tokens de cache lu / écrit,
  entrée fraîche, sortie, raisonnement, **ratio de cache** (`null` sans entrée).
- **Totaux journaliers** : par jour (coût, tokens, ratio de cache), tri chronologique.
- **Par modèle** : modèle normalisé, sessions, tokens, coût (alias fusionnés).
- **Top sessions par coût** : tri coût décroissant puis identifiant ; identifiant canonique,
  titre, coût, tokens, chemin projet, durée (premier → dernier événement de la fenêtre), temps
  actif (somme des écarts inter-événements < **5 min**), coût par minute actif (`null` si temps
  actif nul), nombre d'appels API, inclut des sous-agents, cache lu/écrit, efficacité de cache,
  composition du contexte (estimation par catégorie : fichiers, résultats d'outils, texte,
  raisonnement).
- **Outliers de coût** : session, coût, z-score, tri z décroissant (médiane+MAD).
- **Usage des outils** : outil, nombre d'appels, tokens d'entrée estimés (longueur des arguments ÷
  4 — proxy d'**entrée** uniquement, les résultats ne sont pas comptés).
- **Usage des skills** : skill, nombre de chargements, sessions d'utilisation.
- **Usage des commands** : command, invocations, sessions d'utilisation (tri alphabétique).
- **Paires de skills similaires** : paire, similarité (≥ 0,8 ; top 5).
- **Catalogue de skills** : taille du catalogue, skills jamais chargés.
- **Prompts répétés** : aperçu normalisé (80 premiers caractères), compte (≥ 3), session, longueur
  moyenne (plafonné à 20).
- **Totaux sous-agents** : nombre d'enfants, coût, par type d'agent.
- **Coûts estimés** : par session sans coût enregistré (tokens × taux du harnais) ; clé absente si
  rien à estimer.
- **Avertissements** : session concernée (ou aucune), message (plafonné à 50).

## 7.2 Écosystème (C2)

Champs de tête (schéma, période, instant) + :
- **Nouveautés** : nom, catégorie (plugin / skill / agent / serveur d'outils), URL de dépôt,
  paquet npm (ou `null`), description, date de publication, sources (`found_via`), indicateur
  « nouveau dépôt » (créé dans la fenêtre).
- **Évolutions du cœur** : version, date, résumé, mots-clés correspondants, pertinence
  (haute ≥ 2 mots-clés, moyenne = 1, non émise = 0).
- **Comptes par catégorie** et **par source**.
- **Avertissements** : source, message (API indisponible / limitée, source ignorée pour ce run).

## 7.3 Veille — candidats, contexte, mémoire (C3, C4)

- **Candidats de veille** : fiches {identifiant, nom, sources, score 0-100, sécurité, résumé
  (≤ 200 caractères), signature, indices de pertinence locale}, plafonnées à 30, avec quotas par
  catégorie (nouvelles 12 / améliorables 8 / réapparues 5).
- **Candidats enrichis** (C4) : fiche de base + état local (`absent | déclaré | observé |
  inconnu`), état de capacité, adéquation marché, indices de pertinence locale, bande résiduelle.
- **Observation d'architecture** (C4) : projection **en lecture seule** — compteurs d'états
  (`déclaré | observé | absent | inconnu`), configuration (fichiers, disponible, valide),
  compteurs d'inventaire (plugins/skills/commands/agents), périmètre de harnais.
- **Digest de mémoire** : récemment ignorés (≤ 20), précédemment recommandés (≤ 30), récurrents
  (≥ 3 occurrences, plafonné 20).

## 7.4 Findings de veille (C5)

Décisions orientées action : `installer-nouveau | améliorer-existant | ignorer`, avec impact en
tokens, après coercitions d'état. Annexe de sécurité (fiches bloquées, clé réservée inusurpable).

## 7.5 Digest déclaratif (C8)

Budget de tokens (total, toujours chargés, à la demande, ratio, plus lourds) ; déclencheurs
(counts, descriptions manquantes, chevauchements) ; dépendances (total, cassées) ; findings ;
compteurs d'inspection ; méta (version du scanner, durée, composants scannés, règles vérifiées) ;
périmètre (scope, exclusions, fichiers non scannés) ; compteurs normalisés (fichiers / composants /
findings) ; éléments de drafting (mode, harnais, avertissement, racines supplémentaires, fichiers
orphelins, surface 5.5).

## 7.6 Insights (C9)

Champs de tête (schéma, période, date du run précédent, instant) + :
- **Deltas** : coût WoW (%), delta de ratio de cache, delta de tokens (%), avertissements
  internes, skills (nouvellement chargés / nouvellement silencieux), violations déclaratives par
  règle.
- **Alertes** : règle, seuil, observé, sévérité.
- **Maintenance** : findings (même format que l'audit, session non liée), statistiques (runs
  scannés, skills au catalogue, non-chargés consécutifs, jours de baseline réels pour les pics).
  Les findings **observation-only** (ex. `architecture-drift`, R6) portent
  `observation_only: true` et une action `recalibrate` — constats de rapport, jamais bloquants.

## 7.7 Rapport final (C12, C13, C14)

**Archive** (le signal du planificateur) et **page interactive** (l'utilisateur). Sections :

1. **Résumé exécutif** : chiffres + compteurs (coût, delta, alertes, constats par sévérité,
   créations via git log, compteur « N auto-rédigés en attente de revue depuis M semaines » en
   tête — fenêtre de revue **4** semaines par défaut —, violations déclaratives et leur delta).
2. **Alertes & tendances** : tableau des alertes (règle / seuil / observé / sévérité) + deltas clés
   + tendance journalière (une ligne par jour).
3. **Coûts** : total ; top 3 modèles (fusion < 5 % en « autres ») ; top N sessions (coût, durée,
   coût/min actif, efficacité de cache) avec dédup des outliers (marqueur « † outlier ») ; top 5
   commands ; sous-agents si > 10 %.
4. **Constats qualitatifs** : bloc de prose (C13) — seule section non rendue par le calcul.
5. **Actions du pipeline** : fichiers + commits créés par le run (fenêtre bornée, filtre sujet) ;
   rappel du revert.
6. **Santé de l'environnement** : cohérence d'abord (passe critique, triée par impact, tags
    d'action), puis lint déclaratif (violations, budget, triggers, dépendances, findings R1-R5 avec
    preuves), surface de remédiation, baseline (créée / réutilisée), règles de portabilité.
    Les findings **observation-only** (R6, `architecture-drift`) sont présentés comme des
    observations, distincts des mutations proposées.
    Le statut C18 est affiché séparément : mode, actions proposées, actions protégées et actions
    appliquées ; un manifeste dry-run ne doit pas être présenté comme une mutation. Un manifeste
    requis absent (WAVE 2.5 requise) signale le rapport en priorité haute (run partiel).
7. **Écosystème** : évolutions du cœur (haute → paragraphe, moyenne → ligne) ; nouveautés notables
   (≥ 2 sources **ou** nouveau dépôt) ; le reste en « à surveiller ».
8. **Annexe technique** : artefacts produits, avertissements (plafond 50), statut par étape
   (présence des sorties — les dégradations visibles sans dépendre de la mémoire de
   l'orchestrateur), coût propre.

**Contrat anti-hallucination de la prose** : voir C13 (zéro chiffre, sources closes, paraphrase,
sélection par le calcul).

**Arbitrages du rendu (calcul, pas LLM)** : fusion des modèles < 5 %, regroupement des constats
faibles, relégation « à surveiller », top 3 modèles, sessions hors top N jamais listées, dédup
outliers, troncature des auto-rédigés avec marqueur.

---
