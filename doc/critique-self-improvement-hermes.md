# Critique du pipeline self-improvement du kit vs modèle Hermes

Analyse du pipeline d'auto-amélioration du weekly-advisor-kit au regard du modèle
**Hermes agent** (Nous Research). Objectif : identifier les écarts structurels
(design) et non pas les simples manques de fonctionnalités, pour fonder le redesign
décrit dans [`spec-self-improvement-redesign.md`](spec-self-improvement-redesign.md).

Ce document est un document **de critique et de cartographie** : il ne contient pas
de spécification de redesign (voir le doc compagnon). Les composants Hermes y sont
décrits une fois, puis servent de référentiel à la table de cartographie et aux
constats G1–G9.

---

## 1. Le modèle Hermes en 10 composants

Hermes (Nous Research) conçoit l'auto-amélioration comme une boucle continue qui
**améliore la procédure, pas les poids** : on ne retouche pas le modèle, on améliore
les instructions, skills et outils qui pilotent son comportement. Dix composants
structurent cette boucle.

### 1.1 Foreground nudge
Incitation **faible coût, en premier plan**, injectée à chaque tour : rappelle au
modèle qu'il peut améliorer sa propre procédure quand il observe un pattern
récurrent ou un obstacle. Léger, non bloquant, jamais auto-déclenché par le fork
arrière-plan.

### 1.2 Cadence / itération counter
Un **compteur d'itérations** borne la boucle : empêche le système de s'auto-améliorer
à l'infini en une seule session. Chaque amélioration proposée consomme une itération ;
au-delà du plafond, le modèle doit consolider et s'arrêter. Garantit un terme à la
boucle et évite la récursion.

### 1.3 Background review fork
**Fourche en arrière-plan**, après la réponse et **non-bloquante** : elle ne retarde
jamais le retour utilisateur. Elle analyse la session terminée, détecte les moments
où une skill aurait aidé, et prépare une proposition d'amélioration. Si l'utilisateur
repart avant la fin, la fourche est abandonnée sans coût.

### 1.4 Narrow tool whitelist
Dans la fourche arrière-plan, le modèle n'a accès qu'à un **sous-ensemble restreint
d'outils : mémoire et skills uniquement**. Pas de navigation web, pas d'exécution
générale, pas d'édition arbitraire du code applicatif. Le périmètre d'action de
l'auto-amélioration est volontairement étroit.

### 1.5 `skill_manage`
Un outil dédié de gestion des skills : **create / edit / patch / delete /
write_file**. Les mutations sont **atomiques** (toute écriture est soit appliquée
entièrement, soit abandonnée). Après mutation, **purge du cache** pour que la skill
mise à jour soit relue à neuf.

### 1.6 Provenance taxonomisée
Chaque skill est marquée d'une **origine dans une taxonomie fermée** :
`background_review` / `assistant_tool` / `user` / `bundled`. La provenance guide
ensuite les politiques : on ne supprime pas une skill `user` de sa propre initiative,
on archive ce qui vient d'une review arrière-plan, etc.

### 1.7 Review-prompt policy (anti-learning)
La politique de capture est **explicite et restrictive** :
- **Préférer un PATCH** d'une skill existante à la création d'une nouvelle.
- **Anti-learning** : ne **PAS** capturer les échecs **transitoires**, les
  **prohibitions liées à l'environnement** (locks, permissions), les **récits
  one-off** (incidents ponctuels non reproductibles), ni les **secrets**.
Cette politique évite d'empoisonner la mémoire avec du bruit non généralisable.

### 1.8 Curation / GC
Un cycle de **ramasse-miettes** de la mémoire : **consolidation** des doublons,
**archivage** des skills mortes (non chargées, obsolètes), **pin** des skills
utilisateur protégées. La curation est périodique et détachée de la capture
immédiate.

### 1.9 Cache invalidation
Après toute mutation d'une skill, la version en cache doit être **invalidée** pour
que les lectures suivantes chargent la version à jour. Sans invalidation, la
curation et les patches sont inertes.

### 1.10 Failure modes
Hermes documente les modes d'échec de la boucle :
- **over-capture** : trop de skills, dilution, coût de chargement ;
- **under-capture** : les patterns coûteux ne sont jamais capturés ;
- **stale prohibitions** : une règle capturée autrefois devient obsolète mais reste
  appliquée ;
- **recursive learning** : la boucle s'auto-alimente (la fourche améliore le code qui
  produit la fourche) sans borne.

Principe transversal : **« améliore la procédure, pas les poids »**.

---

## 2. Cartographie existant ↔ Hermes

| # | Composant Hermes | État dans le kit | Verdict |
|---|---|---|---|
| 1 | Foreground nudge | absent | ❌ |
| 2 | Cadence / itération counter | cadence **hebdomadaire en batch** | ⚠ batch |
| 3 | Background review fork (post-réponse, non-bloquant) | étapes 3–4 **synchrones** dans le run hebdo | ⚠ sync |
| 4 | Narrow tool whitelist (mémoire + skills) | drafting limité à `.opencode/skills/` + commands | ✅ partiel |
| 5 | `skill_manage` (create/edit/patch/delete/write_file, atomique) | tools `weekly_commit_draft` (écriture via safe_git_write) | ✅ |
| 6 | Provenance taxonomisée | **absent** — pas d'origine structurée par skill | ❌ |
| 7 | Review-prompt policy / anti-learning | **absent** — pas de filtre anti-learning | ❌ |
| 8 | Umbrella / consolidation | **absent** — pas de regroupement des fragments | ❌ |
| 9 | Curation / GC (consolidation, archive, pin) | recommandé par le rapport, **pas appliqué** | ⚠ recommande, n'applique pas |
| 10 | Cache invalidation | après écriture safe_git_write | ✅ implicite |
| — | Vérification procédurale (sections, contenu) | gate de **portabilité** harness-eval | ⚠ portabilité seulement |

Légende : ✅ composant présent ; ⚠ partiel / dégradé ; ❌ absent.

---

## 3. Constats G1–G9

### G1 — Pas de provenance taxonomisée
Les skills créées par le drafting (étape 4) ne portent aucune origine structurée. On
ne peut pas distinguer, par la donnée, une skill issue d'une review arrière-plan
d'une skill utilisateur ou bundled. Conséquence : la curation (si elle existait) ne
saurait pas quoi archiver, quoi préserver.

### G2 — Pas d'anti-learning (empoisonnement)
Aucun filtre n'empêche de capturer des échecs **transitoires**, des **prohibitions
liées à l'environnement**, des **récits one-off** ou des **secrets**. Le drafting
peut donc figer du bruit non généralisable en skill « durable », ce qui **empoisonne**
la mémoire : une prohibition obsolète ou un incident ponctuel devient une règle
appliquée par la suite.

### G3 — Over-capture sans consolidation (fragments)
Chaque pattern capturé donne tendanciellement lieu à une **nouvelle** skill plutôt
qu'à un **patch** d'une skill existante. Il en résulte des **fragments** : plusieurs
skills chevauchantes, redondantes, qui se fragmentent au fil des runs. Aucun
mécanisme d'umbrella / consolidation ne les regroupe.

### G4 — Pas de cycle curation / GC (avis obsolètes)
Le kit **recommande** la curation dans son rapport (étape 6.5) mais ne l'**applique**
jamais : pas d'archivage des skills mortes, pas de consolidation des doublons, pas de
pin utilisateur opérationnel. Les skills obsolètes s'accumulent et restent chargées.

### G5 — Cadence mismatch (under-capture des patterns peu coûteux)
Le batch **hebdomadaire** ne voit que les sessions coûteuses de la semaine. Les
patterns **peu coûteux mais récurrents**, et les micro-améliorations à fort retour,
ne sont jamais capturés en temps réel. La cadence batch **sous-capture** précisément
ce que le foreground nudge de Hermes capturerait à chaque tour.

### G6 — Pas de patch-preferring
La politique de drafting ne privilégie pas le **PATCH d'une skill existante**. Les
`overlaps_with` ne sont pas exploités pour enrichir une skill plutôt que d'en créer
une nouvelle, ce qui aggrave G3.

### G7 — Vérification procédurale manquante
La gate actuelle vérifie la **portabilité** (harness-eval) mais pas le **contenu
procédural** des skills/commands : sections obligatoires présentes, champ de
vérification, cohérence interne. Une skill bien portable mais vide ou incomplète
passe la gate.

### G8 — Boucle usage → raffinement non fermée
Aucun retour **mesuré de l'usage** (load_count, last_loaded) vers la phase de
drafting. Les skills ne sont ni raffinées selon leur adoption réelle, ni signalées
comme mortes. La boucle d'auto-amélioration est ouverte : on crée, on ne réajuste pas.

### G9 — Pas de TTL / décroissance
Aucune politique d'**expiration ou de décroissance** de la mémoire. Une skill jamais
rechargée ne décroît pas et n'est jamais proposée à l'archivage. Le seul mécanisme de
décroissance du kit (watch-memory.jsonl) est réservé à la veille, pas aux skills.

---

## 4. Synthèse

Le kit couvre correctement la **mécanique d'écriture** (skill_manage, atomicité,
cache) et le **périmètre étroit** (whitelist partielle). Il lui manque toute la
**couche de gouvernance** qui fait la valeur de Hermes : provenance, anti-learning,
consolidation, curation, cadence fine, boucle usage → raffinement, TTL. Ces neuf
constats sont les entrées du redesign [`spec-self-improvement-redesign.md`](spec-self-improvement-redesign.md).
