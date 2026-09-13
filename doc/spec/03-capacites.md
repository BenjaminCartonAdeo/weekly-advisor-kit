# 4. Capacités exposées

Le produit expose **18 capacités** nommées fonctionnellement. Chaque capacité décrit une
intention, des entrées, des sorties, des règles et des erreurs. Les capacités déterministes
produisent des sorties structurées reproductibles ; certaines sont des capacités qualitatives
réalisées par l'orchestrateur agent.

| # | Capacité | Nature | Rôle |
|---|---|---|---|
| C1 | **Collecte télémetrique** | Déterministe | Agrège la semaine : sessions, coûts, tokens, cache, outils, skills, commands. |
| C2 | **Veille écosystème** | Déterministe | Collecte les nouveautés du marché et les évolutions du cœur. |
| C3 | **Distillation de veille** | Déterministe | Réduit la collecte à des fiches scorées, filtrées sécurité, dédupliquées par mémoire inter-run. |
| C4 | **Contexte de veille** | Déterministe | Inventorie l'environnement local et enrichit chaque fiche. |
| C5 | **Validation de veille** | Déterministe | Applique des coercitions d'état et valide les décisions de veille. |
| C6 | **Sélection des candidats d'audit** | Déterministe | Détermine quelles sessions auditer, par des signaux chiffrés. |
| C7 | **Transcription de session** | Déterministe | Produit un transcript lisible et compact d'une session. |
| C8 | **Contrôle de santé déclaratif** | Déterministe | Analyse statiquement la configuration de l'environnement. |
| C9 | **Insights & tendances** | Déterministe | Deltas, alertes, maintenance, comparaison dans le temps. |
| C10 | **Candidats de drafting** | Déterministe | Sélectionne les constats éligibles à un drafting. |
| C11 | **Remédiation déclarative** | Déterministe | Applique (sur gate) des corrections aux findings déclaratifs. |
| C12 | **Préparation du rapport** | Déterministe | Rend les sections chiffrées du rapport à partir des sorties. |
| C13 | **Blocs de prose** | Qualitatif | Rédige le bloc qualitatif (constats) du rapport. |
| C14 | **Assemblage du rapport** | Déterministe | Fusionne sections chiffrées et prose, publie les rendus. |
| C15 | **Publication de brouillon** | Déterministe | Valide, gate et historise un document de compétence généré. |
| C16 | **Coût propre** | Déterministe | Mesure le coût de la session de revue elle-même. |
| C17 | **Diagnostic** | Déterministe | Vérifie l'installation et la disponibilité des prérequis, sans rien écrire. |
| C18 | **Curation des compétences** | Déterministe | Produit un manifeste de curation et, sur validation humaine explicite, applique les actions autorisées aux documents de compétence du projet. |

Chacune des capacités est détaillée ci-dessous (intention, entrées, sorties, règles, erreurs).

---

## C1 — Collecte télémetrique

**Intention** : agréger, pour toutes les sessions actives des harnais configurés dans la
fenêtre glissante, les métriques déjà persistées par chaque harnais — sans relancer d'agent,
sans recalculer de tokenisation.

**Entrées** : liste des sources de sessions actives (harnais), fenêtre glissante, seuils de
configuration.

**Sorties** : une donnée structurée « résumé hebdo » (détaillée en §7.1).

**Règles**
1. Lecture **en lecture seule** des sources, sans écriture et sans dépendre d'un serveur de
   session actif.
2. **Fenêtrage** : on analyse `[ancre − N×24 h, ancre]`, avec N (jours) par défaut 7, surchargeable
   par run (jamais persisté).
3. **Pré-filtre** : on écarte d'abord les sessions sans mise à jour depuis le début de la fenêtre,
   puis on fenêtre précisément les événements par session.
4. **Exclusion des sessions actives** : toute session mise à jour moins de 10 minutes avant
   l'instant de référence est considérée comme encore en cours, exclue des totaux et listée en
   avertissement (couvre la session de revue elle-même et les sessions utilisateur en cours).
5. **Exclusion anti auto-pollution** : toute session dont le titre correspond au titre de
   déclenchement de la revue est exclue des totaux — le produit ne doit pas se voir lui-même
   dans ses statistiques.
6. **Dédup intra-harnais** : deux sources du même harnais exposant les mêmes identifiants
   canoniques → la première source (ordre de configuration) gagne, un avertissement récapitulatif
   est tracé ; le double comptage est structurellement impossible.
7. **Sessions enfants** : les sessions liées à une session parent sont agrégées comme sous-agents,
   comptées dans des totaux dédiés, et jamais fusionnées en double dans les totaux globaux.
8. **Coût facturé** : lu tel quel depuis la télémétrie, jamais recalculé. S'il est absent, un
   avertissement `missing-pricing` est émis, la session reste dans les totaux de tokens et est
   exclue des totaux de coût. **Jamais de taux par défaut silencieux pour un coût enregistré.**
9. **Coût estimé** : toute session **sans aucun coût enregistré** se voit attribuer un coût
   estimé = `tokens totaux × taux du harnais`. Taux par défaut : **5,0** (harnais générique),
   **9,0** (OpenCode), **2,5** (Copilot VS Code) USD par million de tokens, surchargeables par
   source. Le coût estimé est **distinct** du coût facturé : les deux ne se somment jamais ; une
   session sans coût enregistré reste dans les totaux de tokens, son coût estimé ne figure que
   dans le champ « coûts estimés ».
10. **Prompts utilisateur répétés** : les tours utilisateur sont normalisés (minuscules, espaces
    aplatis, ponctuation finale tronquée) ; on compte les occurrences exactes puis les
    quasi-doublons par similarité de séquence (ratio ≥ **0,9**). Une entrée est émise si le compte
    est ≥ **3** et la longueur normalisée ≥ **80** caractères ; la liste est plafonnée à **20**.
11. **Invocation de commands** : tout tour utilisateur commençant par `/` est compté comme
    invocation de command, par command et par session.
12. **Outliers de coût** : calcul robuste par **médiane + écart absolu médian (MAD)** sur les coûts
    de la fenêtre ; une session est outlier si son z-score ≥ **3,0** **et** son coût ≥ **0,50 $**.
    Un avertissement est émis si la fenêtre compte **moins de 15** sessions (échantillon trop
    petit). Les outliers sont un signal d'investigation humaine, jamais une décision automatique.
13. **Bucketing journalier** : totaux (coût, tokens, taux de cache) par jour, pour la tendance.
14. **Paires de skills similaires** : comparaison par similarité de séquence sur la description et
    le début du corps ; seuil **0,8** ; les 5 meilleures paires sont émises, triées par similarité
    décroissante.
15. **Cross-check d'intégrité** : la somme des événements par session est comparée aux agrégats de
    la session ; un écart relatif supérieur à **0,25** émet un avertissement.
16. **Ratio de cache** : `cache lu ÷ (cache lu + entrée fraîche)`. `null` s'il n'y a aucune entrée.
    Les écritures de cache sont un coût, pas un « miss », et n'entrent pas au dénominateur.
17. **Répartition par modèle** : clé normalisée, alias fusionnés.
18. **Tri déterministe** de chaque liste (coût décroissant + identifiant en tranche pour le top
    sessions ; alphabétique ailleurs). Montants arrondis à 6 décimales ; divisions par zéro → `null`.
19. **Avertissements plafonnés à 50** entrées par run.

**Erreurs (fail-soft / fatal)**
- Source de sessions indisponible ou en échec d'initialisation → avertissement + source sautée,
  le run continue.
- Aucune source active → repli sur la source par défaut (OpenCode locale) ; si elle aussi est
  indisponible → échec bloquant.
- Aucune session dans la fenêtre → sortie valide à totaux nuls, pas d'erreur.
- Événement illisible sur une session → journalisé + session ignorée, le run continue.
- Source absente ou verrouillée de façon prolongée, ou schéma inattendu → échec bloquant avec
  message explicite invitant au diagnostic.

---

## C2 — Veille écosystème

**Intention** : collecter, sans état persistant, les nouveautés du marché et les évolutions du
cœur du harnais principal, datées dans la fenêtre.

**Entrées** : fenêtre glissante, mots-clés de pertinence, seuil minimal d'étoiles, liste de
sources typées.

**Sorties** : une donnée structurée « écosystème » (§7.2).

**Règles**
1. **Sources de base** : catalogue de paquets publiés ; dépôts GitHub par sujet ; serveurs d'outils
   officiels mis à jour dans la fenêtre ; versions publiées du cœur du harnais. Filtrage client sur
   les dates.
2. **Sources étendues (configurées)** : dépôt suivi (versions + commits dans la fenêtre), liste de
   curation (nouveaux liens depuis un instantané local), page web (diff de liens), sujet GitHub,
   flux RSS/Atom daté.
3. **Plafond** : **50 items par source et par run**.
4. **Sans état** : pas de dédup inter-runs — un item dans la fenêtre est « nouveau pour vous »
   depuis la dernière lecture. Dédup **intra-run** uniquement (le même dépôt trouvé par plusieurs
   sources → un seul item, les sources sont listées). Exception assumée : les types liste et web
   utilisent un **instantané local** avec **baseline silencieuse au premier run**, nécessaire au
   diff.
5. **Évolutions du cœur** : chaque version publiée dans la fenêtre est marquée par ses
   mots-clés pertinents ; **≥ 2 mots-clés → pertinence haute, 1 → moyenne, 0 → non émise**.
6. **Item notable** : découvert par ≥ 2 sources, ou créé dans la fenêtre (nouveau dépôt).

**Erreurs**
- Source d'API indisponible ou limitée → retry avec backoff sur les erreurs 429 et 5xx, puis
  avertissement ; le run reste **complet** si au moins une source a réussi, **partiel** si toutes
  échouent (pas d'alerte hebdomadaire pour une panne passagère).
- Item à source unique, non nouveau → conservé mais relégué en « à surveiller ».
- Aucune version avec mot-clé pertinent → section vide, pas d'entrée forcée.

---

## C3 — Distillation de veille

**Intention** : réduire la collecte brute (potentiellement volumineuse) à un nombre borné de
fiches scorées, filtrées sécurité et dédupliquées par une mémoire inter-run.

**Entrées** : l'écosystème (C2), la mémoire inter-run, les seuils de distillation.

**Sorties** : une donnée structurée « candidats de veille » + un digest de mémoire borné (§7.3).

**Règles**
1. **Fusion multi-sources par identité** : les occurrences d'un même item sont fusionnées en une
   fiche unique.
2. **Screening sécurité local** : chaque fiche est classée **propre | suspecte | bloquée**, selon
   des marqueurs déterministes (exfiltration de variables d'environnement, injection de prompt,
   chemins d'identifiants, typosquat ≤ 2 caractères vs paquets cœur). Une fiche **bloquée** n'est
   **jamais** soumise à l'analyse qualitative et rejoint une annexe de sécurité ; une fiche
   **suspecte** conserve sa mention de risque.
3. **Score 0-100 déterministe** par pondération : autorité **25**, pertinence **30**, fraîcheur
   **20**, multi-source **15**, traction **10**.
4. **Quotas par catégorie** : nouvelles **12**, améliorables **8**, réapparues **5**, sous un plafond
   total **30** fiches.
5. **Mémoire inter-run append-only** : les fiches sont fusionnées par identité, étiquetées par
   semaine ISO. Purge au-delà de **26 semaines**, sauf les fiches « recommandées » ou « bloquées
   sécurité ». Un item « ignoré » à signature inchangée est retiré ; il **réapparaît** si sa
   version ou sa date change.
6. **Digest de mémoire borné** : « récemment ignorés » ≤ **20**, « précédemment recommandés » ≤
   **30**, « récurrents » (occurrences ≥ **3**) plafonnés à **20**.

**Erreurs**
- Écosystème absent ou distillation désactivée → échec bloquant de cette étape ; le flux aval
  retombe sur l'écosystème complet (mode dégradé documenté).
- L'étape est **séquentielle** : exécutée après la veille écosystème, avant le contexte de veille,
  jamais en parallèle.

---

## C4 — Contexte de veille

**Intention** : inventorier l'environnement local du projet et enrichir chaque fiche candidate
avec l'état local réel (existe-t-il déjà ? est-il déclaré, observé, absent ?).

**Entrées** : l'écosystème (C2), les candidats de veille (C3, s'ils existent), la racine du projet.

**Sorties** : une donnée structurée « contexte de veille » (inventaire + crosswalk marché/existant)
et, si des candidats existent, les fiches **enrichies** (§7.3).

**Règles**
1. **Inventaire déterministe** du worktree : les éléments de l'environnement déclarés et observés
   sont catalogués.
2. **Enrichissement des fiches** : pour chaque fiche candidate, état local
   (`absent | déclaré | observé | inconnu`), état de capacité, adéquation marché, indices de
   pertinence locale, et une **bande résiduelle** compacte sous le seuil de coupure (ce qui reste
   pertinent même hors du top).
3. **Séquentiel** : lit l'écosystème (jamais en parallèle) ; les candidats de distillation sont
   consommés s'ils existent (production des fiches enrichies), sinon l'étape travaille sur
   l'écosystème complet.
4. **Projection d'observation d'architecture** : la sortie inclut un instantané **en lecture
   seule** des faits d'environnement (compteurs d'états `déclaré | observé | absent | inconnu`,
   fichiers de configuration, disponibilité/validité de la configuration, compteurs
   d'inventaire plugins/skills/commands/agents, périmètre de harnais). Aucune inférence
   d'intention, aucune proposition ni application de changement.

**Erreurs**
- Contexte absent (panne de veille ou de distillation) → cette étape est **dégradée** : sortie
  vide + avertissement, jamais un échec bloquant de la revue.

---

## C5 — Validation de veille

**Intention** : valider les décisions de veille issues de l'analyse qualitative et les rendre
actionnables, par coercitions d'état déterministes.

**Entrées** : le brut de veille (analyse qualitative), le contexte de veille, la mémoire inter-run.

**Sorties** : une donnée structurée « findings de veille » (§7.4) + mise à jour de la mémoire.

**Règles**
1. **Coercitions d'état** (une décision ne peut pas contredire l'état local) :
   - état `absent` → `installer-nouveau` ;
   - état `déclaré` → `vérifier-existant` ;
   - état `observé` → `améliorer-existant` ;
   - état `inconnu` → `vérifier-existant` ;
   - une identité déclarée/observée ne peut **jamais** rester `installer-nouveau`.
2. **Cible locale hors inventaire** → `installer-nouveau`.
3. Une fiche **suspecte** sans mention de risque dans sa décision → sévérité **haute**.
4. **Annexe sécurité** : clé réservée de premier niveau, inusurpable par le contenu externe.
5. **Mise à jour de la mémoire** : les fiches recommandées / ignorées / vues sont écrites dans la
   mémoire inter-run.
6. **Jamais d'installation automatique** : le produit remonte des recommandations, l'humain
   décide.

**Erreurs**
- Contexte absent → **dégradé** : validation vide + avertissement, jamais bloquant.

---

## C6 — Sélection des candidats d'audit

**Intention** : déterminer, par des signaux chiffrés issus de la collecte télémetrique, quelles
sessions examiner qualitativement — sans aucun jugement de l'orchestrateur dans la sélection.

**Entrées** : le résumé hebdo (C1), les seuils de configuration.

**Sorties** : la liste ordonnée des sessions candidates à auditer.

**Règles** (une session est candidate si elle répond à au moins un signal)
1. **Top par coût** : les N sessions les plus coûteuses (N par défaut **5**).
2. **Coût par minute actif** au-dessus d'un seuil (défaut **0,50 $/min**) — signal de boucle,
   indépendant du coût absolu.
3. **Efficacité de cache** significativement sous la moyenne hebdomadaire (écart ≥ **0,20**) —
   signal d'invalidation de cache.
4. **Prompts utilisateur répétés** (C1 règle 10) — couvre les sessions bon marché.
5. **Outliers statistiques** de coût (C1 règle 12) — anormalement chères pour la baseline locale,
   même hors du top absolu.
6. **Plafond dur** : au plus **8** sessions auditées par run (défaut). Les candidates au-delà sont
   auditées dans l'ordre de priorité des signaux (top coût, outliers, boucle, cache, prompts
   répétés) jusqu'à épuisement du plafond ; les non-auditées restent listées au rapport comme
   « candidates non traitées ».

---

## C7 — Transcription de session

**Intention** : produire un transcript lisible et compact d'une session, pour l'examen qualitatif.

**Entrées** : un identifiant de session (canonique ou brut), option d'inclusion des sous-agents.

**Sorties** : un texte structuré lisible, une entrée par tour (outil appelé, arguments clés,
résumé du résultat, texte des messages).

**Règles**
1. L'identifiant brut est résolu par la **première source** qui le possède ; le harnais source est
   rappelé dans la sortie.
2. **Compaction des séquences répétitives** : si N ≥ 3 tours consécutifs appellent le même outil
   avec des arguments et un résultat d'erreur similaires (comparaison textuelle simple), n'afficher
   que le premier et le dernier et masquer le reste. Règle mécanique, toujours déterministe —
   déduplication, pas synthèse.
3. L'inclusion des sous-agents est une option explicite.

---

## C8 — Contrôle de santé déclaratif

**Intention** : analyser statiquement la configuration de l'environnement (agents, skills,
commands, configuration, intégrations) indépendamment de son usage, pour y détecter violations,
chevauchements, budgets de tokens et dépendances cassées.

**Entrées** : la racine du projet, le profil d'analyse (périmètre autorisé), la baseline des
findings.

**Sorties** : un « digest déclaratif » structuré (§7.5).

**Règles**
1. **Analyse statique sans exécution** : elle inspecte la configuration déclarée, pas son usage.
2. **Périmètre autorisé (profil)** : deux profils — `strict` (surfaces de politique seules) et
   `advisory` (politique **+** documentation ; profil par défaut). Les surfaces hors périmètre sont
   listées comme non scannées et ne sont jamais incluses implicitement.
3. **Projection** : l'analyse porte sur une vue temporaire de la configuration (fichiers réels
   copiés, **jamais de lien symbolique**, aucun fichier suivi par lien ignoré), étendue aux cibles
   du harnais de drafting résolu. Le projet audité n'est jamais modifié. Les chemins créés sans
   existence réelle sont tracés comme **orphelins**.
4. **Digest structuré** : budget de tokens (total / toujours chargés / à la demande / ratio /
   plus lourds), déclencheurs (counts, descriptions manquantes, chevauchements), dépendances
   (total, cassées), findings, compteurs d'inspection (total / erreurs / avertissements), méta
   (version du scanner, durée, composants scannés, règles vérifiées), périmètre, compteurs
   normalisés (fichiers / composants / findings). Le nombre de composants inspectés n'est jamais
   présenté comme un nombre de violations.
5. **Baseline des findings** : au premier run, l'instantané courant devient la baseline (statut
   « créée ») ; les runs suivants la relisent telle quelle (statut « réutilisée ») — la baseline
   est **capturée et jamais réécrite** par les runs ; seuls les findings **nouveaux** depuis la
   baseline sont listés. La baseline est rafraîchie uniquement sur décision explicite (changement
   de règles).
6. **Version minimale du scanner** : un scanner à version minimale est requis (le diagnostic le
   vérifie) ; un scanner absent ou en échec rend le digest indisponible (géré en aval comme
   dégradation, jamais un crash).
7. **Après le drafting** : le contrôle s'exécute sur l'état final de la semaine, servant de
   **garde-fou indépendant** sur les documents générés (doublons, budget de tokens, chevauchement
   de déclencheurs) — plus rigoureux que la vérification de chevauchement qualitative.

**Erreurs**
- Les codes de sortie « tout va bien » et « violations présentes » sont acceptés (les violations
  vivent dans le digest). Tout autre code, scanner absent ou erreur d'exécution est **bloquant**
  pour cette étape ; un digest absent reste **non bloquant** pour les insights (deltas à `null` +
  avertissement).

---

## C9 — Insights & tendances

**Intention** : comparer le run courant au run précédent (résumé hebdo + digest déclaratif),
appliquer des seuils configurables, et émettre des candidats d'action de maintenance. **100 %
déterministe.**

**Entrées** : résumé hebdo courant + précédent, digest déclaratif courant + précédent, les 4
derniers résumés (historique local), seuils de configuration.

**Sorties** : une donnée structurée « insights » (§7.6).

**Règles — découverte du run précédent**
1. Le run précédent est le **plus récent strictement antérieur** à la date du run courant (par la
   date), parmi l'historique local. S'il manque, les deltas sont `null`, sans erreur (premier run).
2. **Fenêtres de durées différentes** : si `|durée précédente − durée courante| > 1 heure`, les
   deltas de volume (coût, tokens) sont mis à `null` + avertissement explicite — jamais d'artefact
   silencieux. Le delta de ratio de cache reste calculé.
3. **Écrasement protecteur** : ré-exécuter un run du même jour avec une fenêtre de durée différente
   détruit la baseline de comparaison ; le produit en avertit explicitement.

**Règles — alertes (seuils en configuration)**

| Règle | Condition | Sévérité |
|---|---|---|
| Budget hebdomadaire | coût total de la semaine > **25 $** | haute |
| Budget mensuel | somme des résumés des **30** derniers jours > **100 $** | haute |
| Pic journalier | z-score (médiane+MAD) d'un jour vs les jours des 4 résumés précédents ≥ **3,0** ; **z borné à 10,0** (un MAD ≈ 0 produit des z astronomiques, artefact et non signal plus fort ; l'intensité observée = min(z brut, 10), avec note « MAD≈0, z borné » si dépassé) | moyenne |
| Cache bas | ratio de cache hebdomadaire < **0,6** | moyenne |
| Coût WoW | hausse de coût > **15 %** — sautée si fenêtres hétérogènes (delta `null`) | moyenne |
| Violations déclaratives | somme des violations du digest > **10** | moyenne |

**Règles — maintenance (déterministe, R1-R5)** — les règles ignorent les constats listés en
snooze manuel.

| Règle | Condition | Constat |
|---|---|---|
| R1 — retrait | skill jamais chargé sur ≥ **4** runs consécutifs | retrait, moyenne |
| R2 — retrait prioritaire | R1 **et** chevauchement de déclencheurs sur ce skill | retrait, haute |
| R3 — fusion | doublon déclaratif **ou** paire de skills similaires (couverture complémentaire des skills non vus par le contrôle déclaratif) | fusion, moyenne |
| R4 — correction de format | violation triviale (frontmatter / description manquante ou invalide, erreur de format) | correction, basse |
| R5 — réécriture | violation non triviale (budget de tokens dépassé, doublon partiel hors R3) | correction, moyenne — **jamais automatique** |
| R6 — dérive d'architecture | observation d'architecture modifiée sur ≥ **2** runs consécutifs (seuil configurable `architecture_drift_runs`) | `architecture-drift`, basse, **observation-only**, action `recalibrate` |

**Dérive d'architecture — observation-only** : R6 produit un **constat de rapport** (champs
`session_id` absent, `observation_only: true`, recommandation de revue manuelle), **jamais une
alerte bloquante** (sans impact CI) et ne déclenche **ni curation ni application**. Une
projection absente ou inconnue (premier run, étape dégradée) est ignorée sans signal.

**Règles — cohérence de l'environnement (passe critique finale, qualitative)**
1. **Fait** : croise l'état déclaratif de l'environnement (agents, skills, commands) avec l'usage
   réel (chargements, invocations, retraits proposés, contrôle déclaratif, sessions coûteuses).
2. **Ne fait pas** : aucune écriture dans l'environnement — les findings alimentent la revue humaine.
3. **Catégories** : `doublon` (chevauchements invisibles aux règles déterministes), `référence
   morte` (référence vers un élément supprimé ou passé en caché), `inutilisé-non-référencé`
   (jamais utilisé sur N runs **et** référencé nulle part), `désalignement` (description mal
   calibrée, frontmatter invalide, chevauchement non résolu), `risque-token` (agent aux sessions
   coûteuses + skill dédié jamais chargé → cible de référencement prioritaire).
4. Chaque finding porte un **tag d'action** : `supprimer | réduire | fusionner | recalibrer |
   référencer`, classé par impact décroissant.
5. Les preuves (`evidence_summary`) sont **obligatoires** ; un finding sans preuve est écarté à la
   revue.

---

## C10 — Candidats de drafting

**Intention** : sélectionner, parmi les constats d'audit, ceux qui sont éligibles à la création ou
l'amélioration d'un document de compétence.

**Entrées** : les findings d'audit (C6 + analyse qualitative), le plafond combiné, la liste de
snooze.

**Sorties** : la liste ordonnée des candidats de drafting.

**Règles**
1. **Types éligibles** : `skill-candidat` (création de skill), `command-candidat` (création de
   command), `command-amélioration` (amélioration d'une command existante).
2. **Plafond combiné** : au plus **3** candidats par run (skills + commands confondus), classés par
   sévérité décroissante. Les non retenus restent dans l'archive des findings et sont listés au
   rapport comme « candidats non traités ».
3. **Périmètre projet** : un constat dont la session source appartient à un **autre projet** que la
   racine audité n'est **pas** écrit dans l'environnement de la racine : il devient un constat
   « changement d'environnement » (rapport seul), à traiter manuellement dans son projet.
4. **Snooze** : un candidat déjà listé dans le snooze n'est pas ré-émis.

---

## C11 — Remédiation déclarative

**Intention** : proposer puis (sur gate déterministe) appliquer des corrections aux findings du
contrôle de santé déclaratif.

**Entrées** : le digest déclaratif, les propositions de correction (analyse qualitative), la
configuration (règles autorisées, plafond de fichiers).

**Sorties** : une donnée structurée « remédiation » (statuts : appliquée / proposée / manuelle /
bloquée / restaurée) + les fichiers éventuellement corrigés.

**Règles**
1. **Gate d'application** (mode « appliquer » uniquement) — une proposition n'est appliquée que si
   **toutes** les conditions sont réunies :
   - niveau de confiance **élevé** ;
   - la règle figure dans la liste explicite des règles auto-corrigeables (par défaut **vide**) ;
   - remplacement textuel **unique** dans le périmètre autorisé ;
   - au plus **1** fichier distinct modifié par run.
2. **Jamais automatique** pour les règles de sécurité : toute règle préfixée `security/` est
   **toujours bloquée**. Agents, intégrations, configuration et fichiers hors worktree sont
   toujours bloqués.
3. **Défaut sûr** : liste des règles autorisées vide + **aucun commit automatique**.
4. **Transactionalité** : toute application est testée dans une vue temporaire ; si le finding
   ciblé ne disparaît pas ou si le contrôle post-application échoue, **tous** les fichiers modifiés
   sont restaurés.
5. Le résultat est intégré au rapport avec les statuts ci-dessus.

---

## C12 — Préparation du rapport

**Intention** : rendre les sections **chiffrées** du rapport final à partir des sorties
structurées, par un template déterministe — le LLM n'écrit **aucun chiffre**.

**Entrées** : résumé hebdo, insights, digest déclaratif, écosystème, historique git (créations de
documents auto-rédigés dans la fenêtre), baseline.

**Sorties** : un brouillon de rapport avec les sections déterministes (voir §7.7).

**Règles**
1. Tous les nombres proviennent du calcul et de l'historique git, jamais de l'interprétation.
2. L'historique git des créations est **borné à la fenêtre** (`depuis` et `jusqu'à` l'instant de
   référence) et filtré par **sujet** (le filtre de texte matche les corps de message → évite les
   faux positifs) ; marqueur « (+N autres) » au-delà d'une troncature à 10.
3. Les seuils de bruit (fusion des modèles < **5 %**, regroupement des faibles, relégation « à
   surveiller », top 3 modèles) sont implémentés dans le rendu, jamais confiés au LLM.
4. **Consommation du brouillon** : l'assemblage (C14) consomme ce brouillon ; pour un nouvel
   assemblage après édition, la préparation doit être relancée d'abord — sinon erreur explicite
   « brouillon inexistant, relancer la préparation ».

---

## C13 — Blocs de prose

**Intention** : rédiger le bloc qualitatif (constats) du rapport, la **seule** section non rendue
par le calcul.

**Entrées** : les constats (en mémoire), les transcripts si besoin.

**Sorties** : un bloc de prose markdown (≤ ~40 lignes).

**Règles (contrat anti-hallucination de la prose)**
1. **Zéro chiffre** : la prose ne cite ni montants, ni pourcentages, ni compteurs — seules des
   catégories et des sévérités.
2. **Sources closes** : la prose ne fait référence qu'aux constats déjà produits (sélection
   déterministe et analysis qualitative) — aucun fait externe inventé.
3. **Pas de citation verbatim** des transcripts : paraphrase ≤ ~200 caractères.
4. Constats triés sévérité décroissante puis impact ; les faibles groupés en liste compacte.
5. Le bloc doit contenir au moins **40 mots** pour être accepté à l'assemblage.
6. Ne recommande **jamais** l'installation d'un item d'écosystème à source unique et non nouveau
   (la sélection est du calcul).

---

## C14 — Assemblage du rapport

**Intention** : fusionner les sections chiffrées et le bloc de prose, produire l'archive finale et
la page interactive de l'utilisateur.

**Entrées** : le brouillon (C12), le bloc de prose (C13, s'il existe), les artefacts de toutes les
étapes, le coût propre.

**Sorties** : le rapport final (archive) + la page interactive (§7.7).

**Règles**
1. **Le rapport existe toujours**, même si le bloc de prose échoue : s'il est absent, le rapport
   est rendu avec « constats non disponibles » et l'annexe le mentionne. Le rapport présent = run
   terminé ; les dégradations sont visibles dans son annexe.
2. **Publication** : l'archive est écrite dans le répertoire du run (signal du planificateur) ;
   la page interactive autonome est publiée dans un dossier **visible et connu de l'utilisateur**
   (historique daté + exemplaire « dernier » écrasé à chaque run), dans le projet, en rendu
   **best-effort** (échec de rendu → avertissement, jamais fatal).
3. Ouverture automatique de la page dans le navigateur après rendu réussi, **best-effort** (jamais
   fatale ; désactivable pour un planificateur headless).
4. L'orchestrateur termine en affichant le chemin de la **page interactive** d'abord, puis
   l'archive.
5. L'utilisateur ne connaît ni le répertoire interne des runs ni son organisation.

---

## C15 — Publication de brouillon

**Intention** : valider, gater et historiser un document de compétence généré (skill ou command)
dans les cibles du harnais de drafting résolu.

**Entrées** : le document généré (chemin + type), les règles de portabilité, la configuration git.

**Sorties** : un commit isolé par écriture (ou une non-publication explicite avec motif).

**Règles**
1. **Validation de forme** : frontmatter parsable, `name` == nom du répertoire (skills),
   `description` non vide. En échec → **pas de commit**, constat « changement d'environnement »,
   avertissement au rapport.
2. **Pré-checks git** : tête détachée, ou rebase/fusion en cours → **pas de commit** (fichier
   écrit, non historisé), avertissement. Un working tree sale n'est pas un blocage.
3. **Ajout scopé** : toujours le fichier exact, jamais un répertoire ni un ajout global.
4. **Identité** : lue depuis la configuration, injectée à la commande de commit.
5. **Message construit** : préfixe par type (`skill:` / `command:`), mention « auto-rédigé, revue
   hebdo », corps traçant les sessions sources et le chevauchement détecté (lus depuis le document
   lui-même). **Un commit par écriture, jamais groupé.**
6. **Gate de portabilité (vérificateur dédié)** : avant tout commit d'un **skill**, une vérification
   mécanique est appliquée (règles de portabilité) avec pour racine la racine du projet (chargement
   des règles locales) :
   - **≥ 1 erreur → commit refusé** (fix manuel requis) ;
   - **avertissements seuls → commit autorisé**, note jointe au résultat ;
   - **vérificateur absent → note « gate ignorée » (fail-soft)**, le diagnostic signale l'absence
     du vérificateur ;
   - **vérificateur défaillant (timeout, crash) ou sortie illisible → commit refusé** (environnement
     à réparer — une gate morte ne produit jamais de faux vert) ;
   - **type command → gate non applicable** (la vérification mécanique ne couvre que les skills) :
     **skip explicite** avec note dans le résultat, le commit part sans gate.
7. **Échec (hooks, autre)** : fichier conservé, **pas de retry en boucle**, rattrapage manuel.
8. **Rollback** : toujours un revert de commit isolé, **action humaine** (jamais automatisée,
   jamais interactive).
9. **Périmètre** : le document doit appartenir aux cibles de drafting du harnais résolu, dans le
   projet. Rien hors de ce périmètre n'est écrit.

---

## C16 — Coût propre

**Intention** : mesurer le coût de la session de revue elle-même — le seul endroit où le produit
se voit dans ses propres statistiques (il est par ailleurs exclu des totaux par construction).

**Entrées** : titre de déclenchement de la revue, sources de sessions.

**Sorties** : le coût de la session de revue (identifiant, coût, tokens).

**Règles**
1. Recherche par titre de déclenchement, puis repli sur la session la plus récente de l'orchestrateur
   de revue, toutes sources confondues.
2. Un coût nul est annoté « coût nul — modèle gratuit ou session non retrouvée ».
3. Aucune source utilisable → échec partiel avec message explicite.

---

## C17 — Diagnostic

**Intention** : vérifier l'installation et la disponibilité des prérequis, **sans rien écrire**.

**Entrées** : la configuration, la racine du projet, l'environnement d'exécution.

**Sorties** : un rapport de diagnostic (statut par point, cibles de drafting résolues, surface de
remédiation, source de sessions retenue) + un code de sortie : **0** prêt, **1** avertissements,
**2** blocage.

**Règles**
1. Configuration parsable et clés requises présentes ; config introuvable → **avertissement**
   (continuation avec valeurs par défaut) sauf si elle était explicitement fournie.
2. **Racine d'installation** : la racine contient le répertoire de configuration de
   l'environnement — sinon **blocage** avec message d'action (sentinelle d'installation : plus
   jamais un avertissement silencieux).
3. **Version minimale du harnais principal** vérifiée (l'épinglage du schéma de lecture passe par
   l'épinglage de la version du harnais).
4. **Sources de sessions** : itération générique sur les sources configurées, statut par source
   (chemin, état du schéma). Aucune source disponible → **blocage**.
5. **Répertoire de sortie** accessible en écriture.
6. **Prérequis externes** : vérificateur déclaratif et outil de versionnement résolus ; vérificateur
   absent → avertissement (étape dégradée, pas un blocage).
7. Affiche les **cibles de drafting résolues** (harnais + mode) et la **surface de remédiation**
   (voir §5.6).
8. À relancer après chaque mise à jour du produit.

---

## C18 — Curation des compétences

**Intention** : transformer les décisions de cohérence et les signaux de décroissance en un
manifeste déterministe, puis permettre leur application contrôlée aux skills du projet.

**Entrées** : findings de cohérence, catalogue des skills, données d'usage TTL, ancre du run et
validation humaine éventuelle.

**Sorties** : un manifeste structuré contenant les décisions proposées, les décisions ignorées ou
protégées, les candidats TTL, les compteurs de statut et le mode (`dry-run` ou `apply`).

**Règles**
1. L'étape intervient après la jointure de WAVE 2 et avant la préparation du rapport (WAVE 2.5). Elle est **requise à l'assemblage** : si les findings de cohérence portent des actions de curation mais que le manifeste est absent, le rapport est signalé **priorité haute** et le run est **partiel** (code 1).
2. Le mode par défaut est **dry-run** : aucune archive, fusion, suppression, déplacement ou
   modification n'est effectuée ; le manifeste est néanmoins écrit.
3. Le mode `apply` n'est recevable qu'après validation humaine explicite et reste borné aux racines
   de skills du projet audité. Les racines globales, les liens symboliques et les chemins hors
   projet sont refusés. **Gate politique** : même en `apply`, seule l'action `archive` est
   exécutée (déplacement idempotent vers un répertoire d'archives daté, **jamais de suppression**) ;
   `merge`, `reference`, `pin`, `delete` et `recalibrate` restent des propositions, sans aucune
   opération fichiers.
4. La provenance `origin=user` est toujours protégée : ses décisions destructives deviennent
   `skip`, et ne peuvent pas être appliquées. Une politique `pin` reste prioritaire sur archive.
5. L'archivage TTL exige un dépassement de **90 jours** depuis le dernier chargement, ou zéro
   chargement pendant **3 runs consécutifs** ; les skills pinnées ou utilisateur sont exemptées.
6. Chaque décision porte une cible, une action, une raison et un statut ; les compteurs du manifeste
   doivent correspondre à ses décisions. Une anomalie de lecture produit un avertissement et ne
   déclenche jamais une application implicite.

**Erreurs** : entrée absente ou illisible → manifeste dégradé et code partiel ; racine projet
irrésoluble ou tentative de sortie du périmètre → application refusée ; jamais de fallback vers une
racine globale.

---
