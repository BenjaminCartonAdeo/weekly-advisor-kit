# 8. Règles transverses

## 8.1 Déterminisme et reproductibilité
- Pour un même jeu d'entrées (mêmes sources, mêmes seuils) et une **même ancre**, une revue
  produit des sorties **identiques** : chaque liste a un tri total documenté, les montants sont
  arrondis, les divisions par zéro → `null`, les avertissements sont plafonnés.
- La **reprise d'une ancre** (rejouer la même fenêtre) couvre exactement la même fenêtre : un
  re-run du même jour écrase la sortie du jour (fenêtre identique), jamais purgée entre jours
  différents.

## 8.2 Ancre et fenêtrage
- Toutes les étapes d'un même run partagent la **même ancre** : la fenêtre dérivée est identique
  partout, sans dérive entre invocations. L'ancre est stable tant que la cadence est respectée ;
  elle est **rafraîchie** si périmée (sinon une ancre figée gèlerait la cadence et rejouerait la
  même fenêtre) et **créée** si absente. Une ancre explicite reste disponible pour les tests et
  rejeux.
- La **fenêtre** est `[ancre − N×24 h, ancre]`, N par défaut **7**, surchargeable par run (déduit
  du contexte, jamais persisté dans la configuration).

## 8.3 Fail-soft par classe d'erreur
- **Transitoire / isolée** (source de données en panne, item illisible, vérificateur absent) →
  avertissement + dégradation locale, le run continue.
- **Partielle** (au moins une étape dégradée) → code **1**, le rapport est quand même produit avec
  les dégradations à l'annexe.
- **Fatale** (collecte défaillante, installation invalide, exécution déclarative impossible) →
  code **2**, arrêt sans rapport.

## 8.4 Alias du run actif (comportement observable)
- Un **run actif** est observable par toutes les étapes : il désigne le répertoire du run courant,
  de façon stable pendant toute la revue. Un humain n'a **jamais** besoin d'ouvrir le répertoire
  interne des runs pour trouver son rapport.

## 8.5 Baseline capturée-jamais-réécrite
- Les **baselines** (findings déclaratifs, et, par extension, tout état de référence) sont
  **capturées une fois** (premier run → « créée ») puis **relues telles quelles** par les runs
  suivants (« réutilisée »), **jamais réécrites** par le déroulement normal. Elles ne sont
  **rafraîchies que sur changement de règles** (décision explicite).

## 8.6 Zéro lien symbolique pour les artefacts projetés
- Les artefacts projetés vers l'environnement (documents de compétence, vues temporaires du
  contrôle déclaratif) sont **toujours des copies réelles**, jamais des liens symboliques. Les
  liens de répertoire ne sont pas suivis ; les fichiers liés sont ignorés.

## 8.7 Contenu universel des documents générés — 5 règles de portabilité
Tout document de compétence généré est consommable par plusieurs harnais **sans modification
manuelle**, quel que soit le harnais cible :
1. **Frontmatter minimal** — `name` + `description` seuls ; `metadata`, `license`, `compatibility`
   tolérés. Tout autre champ de premier niveau est interdit (le surplus va sous `metadata`).
2. **Outils par nom conceptuel, jamais d'identifiant technique en dur** — aucun identifiant
   technique interne du produit dans les documents générés : les commandes du produit sont
   référencées par leur nom conceptuel.
3. **Section « Comment invoquer » multi-plateforme obligatoire** — l'invocation est décrite pour
   plusieurs harnais, jamais pour un seul.
4. **Chemins relatifs projet** — aucun chemin absolu (`/home/`, `/Users/`, `/root/`, `~/`,
   `C:\...`).
5. **Scripts auto-contenus** — tout script référencé est inclus/copié avec le document ; interdits
   les chemins hors projet (`../`, `~/`, `/usr|/opt|/etc|/var`) et les scripts distants.

## 8.8 Gate de qualité avant publication d'un brouillon
Avant de publier (historiser) un document de compétence, deux gates séquentielles, avec des
verdicts **exactement** :

| Gate | Verdict bloquant | Verdict « note » | Verdict ignoré | Causes exactes |
|---|---|---|---|---|
| **Validation de forme** | refus (pas de commit) | — | — | frontmatter illisible ; `name` ≠ nom du répertoire (skills) ; `description` vide |
| **Pré-checks git** | refus (pas de commit) | — | — | tête détachée ; rebase/fusion en cours (le fichier est écrit, non historisé) |
| **Portabilité (vérificateur dédié)** | refus (pas de commit) | commit autorisé + note | « gate ignorée » (fail-soft, binaire absent) · **non applicable aux commands** (skip explicite + note) | ≥ 1 erreur de portabilité → refus ; avertissements seuls → note ; vérificateur absent → ignorée ; vérificateur défaillant / sortie illisible → **refus** (fix environnement) ; kind=command → **skip explicite avec note** (skills uniquement) |

Un échec d'historisation (hooks, autre) → fichier conservé, **pas de retry en boucle**, rattrapage
manuel.

## 8.9 Garde-fous de coûts du coordinateur
- **Plafond d'audit** : au plus **8** sessions auditées par run — le volume de transcripts est le
  seul moteur de coût de l'analyse qualitative, il est plafonné.
- **Plafond de drafting** : au plus **3** documents générés par run.
- **Budget de veille** : entrées de la veille critique < **10 %** du volume brut historique.
- **Le produit se voit lui-même** uniquement via le coût propre (C16) ; il est par ailleurs exclu
  de ses propres statistiques par construction (exclusion des sessions actives + filtre de titre).

## 8.10 Snooze (constats ignorés)
La liste `ignored_findings` (identifiants « catégorie:cible ») supprime de la proposition les
constats correspondants. Elle est **éditée dans la configuration, jamais maintenue par un état
auto-géré** ; les ignorés sont listés en annexe du rapport.

## 8.11 Configuration en vues groupées (lecture seule)
- La configuration est persistée sous une forme **plate** et exposée aux étapes par **vues
  groupées thématiques** (sources, stockage, coûts, curation), **en lecture seule**.
- Une vue est une projection dérivée, jamais une réécriture : la forme persistée reste
  **inchangée**, rétro-compatible, aucune migration de format, aucune clé renommée.
- Lire une vue n'a aucun effet de bord sur la configuration.

---
