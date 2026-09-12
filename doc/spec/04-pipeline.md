# 5. Pipeline de revue

La revue s'exécute en une séquence d'étapes numérotées. Les étapes **déterministes** produisent
les sorties structurées ; les étapes **qualitatives** sont réalisées par l'orchestrateur agent à
partir de ces sorties. L'ordre est figé et les dépendances d'ordre sont **séquentielles** :
certaines étapes ne peuvent jamais tourner en parallèle.

## 5.1 Étape 0 — Diagnostic

Exécuter le diagnostic (C17) de façon systématique au début de la revue. Un diagnostic **bloquant**
(arrêt d'installation) arrête la revue sans produire de rapport.

## 5.2 Étape 1 — Collecte télémetrique

Exécuter C1. Produit le résumé hebdo. En cas d'échec **bloquant** de la collecte, la revue
**s'arrête sans écrire de rapport** (l'orchestrateur ne continue pas sur une collecte défaillante).
En cas de collecte **partielle** (avertissements), la revue continue et les dégradations sont
notées à l'annexe.

## 5.3 Étape 2 — Veille écosystème

Exécuter C2, puis C3 (distillation, séquentielle après 2), puis C4 (contexte, séquentielle après
2 — consomme les candidats de 3 s'ils existent). Ces étapes ne sont **jamais** exécutées en
parallèle.

## 5.4 Étape 3 — Audit qualitatif

1. Sélection déterministe des candidats (C6) — aucune session n'est écartée ou retenue par
   jugement de l'orchestrateur.
2. Pour chaque candidat : transcription (C7), sauvegarde de l'extrait sur disque, examen selon les
   catégories de constats.
3. Production des findings d'audit structurés (archive).
4. Transmission des constats aux étapes de drafting, de veille critique, de cohérence et de rapport.

**Catégories de constats**

| Catégorie | Ce qui est cherché | Signal déterministe de sélection |
|---|---|---|
| `boucle` | retries répétés sur le même point | coût/min actif élevé + appels API élevés sur courte fenêtre |
| `invalidation-cache` | modification répétée des premiers messages / prompt système | efficacité de cache basse malgré du contexte réutilisable |
| `contexte-gonflé` | session longue jamais compactée alors que le sujet a changé | durée / temps actif / tokens élevés |
| `injection-manuelle` | copier-coller répété d'infos qui auraient pu venir d'un skill/command/MCP | transcript × usage de skills |
| `sous-usage-skill` | tâche couverte par un skill existant faite manuellement | skills jamais chargés + transcript |
| `sous-usage-command` | séquence de prompts longs répétée couverte par une command existante jamais invoquée | transcript (tours utilisateur) × scan des commands |
| `command-candidat` | séquence de prompts longs répétée **≥ 3 fois**, aucune command existante ne la couvre | transcript (tours utilisateur) — détection qualitative |
| `skill-candidat` | séquence de **5+** appels d'outils cohérente vers un même objectif, aucun skill existant ne la couvre | transcript × usage de skills |
| `mauvais-modèle` | modèle coûteux pour une tâche simple (ou l'inverse, à l'origine de retries) | répartition par modèle × nature de la tâche |
| `amélioration-command` | pattern coûteux lancé par une commande existante (premiers tours `/nom`) — la commande doit embarquer des garde-fous de coût | transcript (tours initiaux) × catégories coûteuses |

**Format d'un constat** : identifiant de session (canonique), catégorie, sévérité (haute/moyenne/
basse), description, preuve résumée (**paraphrase**, pas de citation verbatim, ≤ ~200 caractères),
recommandation, type de recommandation, impact en ordre de grandeur (petit/moyen/grand / non
estimable).

**Types de recommandation → action**
- `habitude-prompting` → rapport uniquement (retour sur la façon de prompter).
- `changement-environnement` → rapport uniquement (y compris skill/command existant mal calibré :
  la réécriture d'un fichier existant dont les agents dépendent reste manuelle).
- `skill-candidat`, `command-candidat`, `amélioration-command` → transmis au drafting (C10,
  plafond combiné). Seules catégories d'écriture.

## 5.5 Étape 3.5 — Veille critique (qualitative)

À partir des fiches enrichies (ou de l'écosystème complet en repli si la distillation est absente),
du digest de mémoire, des findings des sessions coûteuses et des alertes, l'orchestrateur produit
un brut de veille avec des décisions orientées action : `installer-nouveau | améliorer-existant |
ignorer`, avec l'impact en tokens. Jamais d'installation automatique. **Budget borné** : les
entrées de cette étape (fiches enrichies + digest mémoire) restent < 10 % du volume de la collecte
brute historique.

**Filet de rattrapage (conditionnel)** : si le nombre de candidats de veille est inférieur à **20**
et que des éléments résiduels pertinents existent sous le seuil de coupure, une phase
supplémentaire de balayage est déclenchée pour ne pas manquer d'éléments pertinents hors du top.

## 5.6 Étape 3.6 — Validation de veille

Exécuter C5 (coercitions d'état, annexe sécurité, écriture mémoire). En l'absence de contexte de
veille, l'étape est **dégradée** (sortie vide + avertissement, jamais bloquant).

## 5.7 Étape 4 — Auto-drafting (qualitative)

1. Reçoit les candidats de drafting (C10, ≤ 3).
2. Pour chaque candidat : lit l'extrait de transcript (re-fetch seulement si manquant), évalue si
   la séquence est généralisable (rejet si trop spécifique — **double filtre**), choisit skill ou
   command, rédige le document selon les conventions existantes.
3. **Arbre de décision skill/command** : respecter le type du constat par défaut. En cas de doute :
   une séquence qui généralise des appels d'outils / navigation / analyse → **skill** (procédure) ;
   une séquence qui généralise un prompt long répété côté utilisateur, sans raisonnement
   multi-outils → **command** (template à paramètres). Changer de type par rapport au constat est
   une **exception** à justifier dans le document.
4. **Vérification de chevauchement** : si le document chevauche un skill/command/agent existant →
   **ne pas créer de doublon** : émettre un constat « changement d'environnement » (fusion manuelle)
   et passer au suivant.
5. **Public cible** : déterminer qui utilisera le document ; si le public est identifiable, le
   référencer dans le corps de chaque agent ciblé (un document jamais référencé ni chargé est
   proposé au retrait par R1) ; sinon, chargement piloté par la seule description.
6. Publier via C15 (gate de portabilité, commit isolé).

**Placement mono-cible du drafting (§4 C10/C15)** : le drafting cible **un harnais par projet**,
résolu par **override config > détection par marqueurs > défaut**.

| Marqueur (répertoire à la racine du projet) | Harnais | Cibles de projection |
|---|---|---|
| `.claude/` | Claude Code | `.claude/skills` |
| `.opencode/` | OpenCode | `.opencode/skills` |
| `.github/prompts/` **ou** `.github/skills/` | Copilot VS Code | `.github/prompts`, `.github/skills` |
| `.agents/` | Codex | `.agents` |

- **Priorité en cas de marqueurs multiples** (le premier gagne) : `claude-code > opencode >
  copilot-vscode > codex`.
- **Aucun marqueur → défaut OpenCode + avertissement explicite**.
- **Override config** : liste de harnais (→ override) ; liste vide (→ mode « legacy », toutes les
  cibles connues) ; clé absente ou invalide (→ détection automatique).
- L'écriture du drafting **et** la projection du contrôle déclaratif ciblent le harnais résolu ;
  la vérification de chevauchement lit les cibles de ce harnais.

**Format des documents générés**

*Skill* : frontmatter (`name`, `description`, métadonnées de traçabilité — auteur, date, sessions
sources, chevauchements, agents ciblés), puis sections « Quand utiliser », « Procédure » (étapes
généralisées, pas la trace verbatim), « Points d'attention » (erreurs rencontrées à éviter).

*Command* : frontmatter (`description` obligatoire, `agent`/`model` optionnels) + corps du prompt
généralisé, avec paramètres pour les valeurs variables.

## 5.8 Étape 5 — Contrôle de santé déclaratif + remédiation

1. Exécuter C8 sur l'état final de la semaine (après le drafting), puis C9 (insights).
2. **Surface de remédiation** (décision déterministe) :

| Harnais de drafting résolus | Surface 5.5 | Raison |
|---|---|---|
| cible unique OpenCode | `projection` | périmètre natif déjà couvert |
| cible unique connue hors OpenCode | `portabilité` | remédiation conditionnée au mapping de portabilité |
| plusieurs cibles **avec** OpenCode | `combinée` | projection + mapping pour les autres |
| plusieurs cibles **sans** OpenCode | `portabilité` | mapping requis |
| entrée vide / harnais inconnu | `projection` (repli sûr) | raison explicite |

3. **Remédiation (qualitative + gate)** : le skill de remédiation lit le digest, rédige des
   propositions, puis C11 applique (sur gate) les corrections éligibles. Résultat intégré au
   rapport.

## 5.9 Statuts de sortie — conditions exactes

| Code | Sens | Conditions exactes |
|---|---|---|
| **0** | Succès complet | Toutes les étapes ont tourné complètement, aucune étape partielle ni fatale. |
| **1** | Succès partiel (dégradé) | Au moins une étape partielle : avertissements de la collecte, source de veille en panne (toutes échouées), audit sans findings produits, drafting avec un commit en échec (fichier écrit non historisé), contexte de veille absent, digest déclaratif absent (deltas à `null`), coût propre non mesurable, **manifeste de curation absent alors que les findings de cohérence portent des actions de curation** (WAVE 2.5 requise). L'orchestrateur remonte le **pire** des codes rencontrés parmi ses étapes. |
| **2** | Échec bloquant | Échec fatale d'une étape : collecte défaillante (source bloquante), diagnostic d'installation bloquant, échec d'exécution du contrôle déclaratif. L'orchestrateur **s'arrête sans écrire de rapport**. |

**Étapes qualitatives — codes dérivés de leurs livrables** : ces étapes n'ont pas de code de
sortie propre ; c'est la présence/absence de leur livrable qui dégrade le run. Audit sans findings
produits → **1** ; drafting avec un commit en échec → **1** ; rapport non écrit → **2**.

**Contrat d'alerte (le rapport est le signal)** : le rapport est écrit en **dernier** ; le rapport
présent = run terminé (dégradations visibles à l'annexe) ; rapport absent à la fin de l'exécution =
échec, le planificateur alerte.

## 5.10 Étape 6 — Insights & cohérence

Exécuter C9 (deltas, alertes, maintenance) puis la passe de cohérence de l'environnement
(§ C9, qualitative). Aucune écriture dans l'environnement.

## 5.10.1 Étape 6.6 — Curation WAVE 2.5

Après la jointure de l'étape 6, exécuter C18. Écrire le manifeste, par défaut en `dry-run`
(**gate no-apply** : aucun fichier n'est déplacé, fusionné, supprimé ou modifié), puis
transmettre son statut au rapport. Une application éventuelle avec `apply=true` est une action
séparée, explicitement validée par l'humain ; elle ne fait pas partie du chemin automatique.
**Gate politique de l'application** : même avec `apply=true`, seules les actions `archive` sont
exécutées (déplacement idempotent, jamais de suppression) ; les autres actions restent des
propositions sans opération fichiers. L'étape est **requise à l'assemblage** : si les findings
de cohérence portent des actions de curation mais que le manifeste est absent, le rapport est
signalé **priorité haute** et le run est **partiel** (code 1).

## 5.11 Étape 7 — Rapport final

1. **7a — Préparation** (C12) : rend les sections chiffrées.
2. **7b — Prose** (C13) : rédige le bloc qualitatif (optionnel).
3. **7c — Assemblage** (C14) : fusionne, publie l'archive (signal) et la page interactive.
   ⚠ Un assemblage réussi **consomme** le brouillon : pour un nouvel assemblage, relancer la
   préparation d'abord.

## 5.12 Étape 8 — Coût propre (annexe)

Exécuter C16 et reporter le coût propre à l'annexe du rapport.

---
