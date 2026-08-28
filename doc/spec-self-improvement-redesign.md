# Spécification — Redesign self-improvement du kit (Hermes)

Spécification fonctionnelle du redesign du pipeline d'auto-amélioration du
weekly-advisor-kit, aligné sur le modèle Hermes (Nous Research). Ce document est le
**contrat d'implémentation** : il définit le frontmatter canonique, la boucle de
redesign et les exigences R1–R8. Il complète la critique
[`critique-self-improvement-hermes.md`](critique-self-improvement-hermes.md) qui
établit le diagnostic G1–G9.

Convention : les valeurs chiffrées (seuils, durées, taux) font partie intégrante du
contrat et doivent survivre telles quelles à toute reconstruction.

---

## 1. Frontmatter étendu canonique

Toute skill du périmètre auto-amélioré porte un frontmatter YAML étendu. Le bloc
`metadata` ci-dessous est la **source de vérité** pour la provenance, l'usage et la
politique de cycle de vie.

```yaml
metadata:
  authored_by: <agent|humain|weekly>
  authored_at: <ISO-8601>
  origin: user | bundled | weekly-foreground | weekly-background
  write_context: <description libre du contexte d'écriture>
  confidence: high | medium | low
  skill_id: "skill_<8 hex>"        # = 'skill_' + sha256(nom.normalize().lower())[:8]
  source_sessions: [ <session_id>, ... ]
  overlaps_with: [ <skill_id>, ... ]
  target_agents: [ <agent_name>, ... ]
  last_verified_at: <ISO-8601|null>
  usage:
    last_loaded: <ISO-8601|null>
    load_count: <int>
  ttl_policy: decay | pin | null
```

### 1.1 Règles du bloc metadata

- **`skill_id`** : identifiant **stable**, déterministe et indépendant du contenu —
  `'skill_' + sha256(nom.normalize().lower())[:8]`. `nom.normalize()` : nom de la
  skill en NFC, minuscules, sans espaces redondants. Une skill renommée **garde** son
  `skill_id` (le renommage ne change pas l'identité). C'est la clé de jointure de la
  curation et des `overlaps_with`.
- **`origin`** : taxonomie fermée `user | bundled | weekly-foreground |
  weekly-background`. Défaut : `weekly-background` (skill issue d'une review
  arrière-plan). Seules les skills `origin: user` sont protégées de l'archivage
  automatique (voir R4, R8).
- **`usage`** : mis à jour à chaque chargement effectif — `last_loaded` (ISO) et
  `load_count` (incrément). Données d'entrée de la décroissance R8.
- **`ttl_policy`** : `decay` (soumise à décroissance/archivage), `pin` (exemptée de
  l'archivage, typiquement `origin: user` explicitement pinnée), `null` (pas de
  politique, comportement par défaut de la source).
- **`last_verified_at`** : horodatage de la dernière validation procédurale /
  relecture (voir R6). `null` tant que jamais vérifiée.

### 1.2 Découpage / gestion de `skill_id` dans le pipeline

Le `skill_id` **vit dans le drafting et le frontmatter**, pas dans les artefacts
intermédiaires de sélection. En pratique :
- `candidates.py` **calcule** et propage `skill_id` sur chaque candidat (déterministe
  à partir du nom proposé).
- `draft_targets.py` **ne stocke pas** `skill_id` comme champ propre : il le propage
  tel quel depuis les candidats, sans le recalculer ni le persister comme source.
- La **source de vérité** de `skill_id` est le frontmatter de la skill écrite (étape 4
  drafting).

---

## 2. Nouvelle boucle de redesign

La boucle remplace la chaîne linéaire par un **cycle fermé** dont chaque étape nourrit
la suivante, et dont la sortie (curation) reboucle sur le drafting.

```
STEP3 quality-audit
  ├─ pré-filtre anti-learning (G2) : drop des patterns transitoires/env/one-off/secret
  └─ catégorise les candidats → cat: skill-improvement
        │
        ▼
STEP4 drafting
  ├─ CONSOLIDATE, patch-preferring (G3/G6) : overlaps_with → PATCH, pas de nouveau skill
  ├─ skill_id stable propagé (R1)
  ├─ gate portabilité (harness-eval) — inchangée
  └─ gate vérification procédurale (R6) — NOUVELLE
        │
        ▼
STEP5.5 harness-remediation
        │
        ▼
STEP6.5 coherence-review
  └─ émet des DÉCISIONS : archive | merge | pin | reference (+ target_skill_id)
        │
        ▼
STEP6.6 weekly-skill-curation   (NOUVEAU, gated)
  ├─ applique archive / merge sur origin ∈ {weekly-*}
  ├─ JAMAIS sur origin: user, sauf pin levé (R4)
  └─ met à jour watch-memory.jsonl (décroissance R5/R8)
```

### 2.1 Étape 3 — quality-audit (pré-filtre + catégorisation)

- **Pré-filtre anti-learning** (R2) : avant toute candidature au drafting, les
  findings qui tombent dans `is_anti_learning` sont **drop** (voir R2). Ils ne
  produisent pas de skill.
- **Catégorisation** : les candidats retenus sont étiquetés `cat: skill-improvement`
  pour alimenter la boucle usage → raffinement (R7).

### 2.2 Étape 4 — drafting (consolidation + gates)

- **CONSOLIDATE, patch-preferring** (R3) : si un candidat a des `overlaps_with`, le
  drafting **patche** la skill existante cible au lieu d'en créer une nouvelle. Une
  nouvelle skill n'est créée que si aucune cible de patch n'existe.
- **skill_id stable** : propagé depuis `candidates.py` jusqu'au frontmatter.
- **Gate portabilité** : inchangée (harness-eval).
- **Gate vérification procédurale** (R6) : NOUVELLE, s'exécute dans `commit-draft`.

### 2.3 Étape 5.5 — harness-remediation
Inchangée dans son rôle ; consomme les sorties du drafting validé.

### 2.4 Étape 6.5 — coherence-review (décisions)
La coherence-review, au lieu de se limiter à un constat, **émet des décisions
structurées** consommées par la curation :
- `archive` : skill morte / obsolète → proposer à l'archivage (soumis à R4/R8).
- `merge` : deux skills chevauchantes → fusionner dans la cible `target_skill_id`.
- `pin` : skill utilisateur à préserver explicitement (lève la protection d'archivage
  de manière ciblée).
- `reference` : skill à référencer (pas de mutation, simple lien dans une skill
  umbrella).

Chaque décision porte un `target_skill_id`. La coherence-review **alimente aussi**
l'étape 4 (R7) : les skills peu chargées / raffinables y sont renvoyées comme candidats.

### 2.5 Étape 6.6 — weekly-skill-curation (NOUVEAU)

- **Gated** : outil `weekly-skill-curate`, dry-run par défaut, application explicite.
- **Périmètre** : n'applique l'archivage/merge que sur `origin ∈ {weekly-background,
  weekly-foreground}`. **Jamais** sur `origin: user`, sauf si `pin` a été levé par une
  décision explicite.
- **Décroissance** : réutilise `watch-memory.jsonl` comme registre de décroissance
  (voir R8).
- Sortie : rapport des mutations (archivées, mergées, pinnées) consommé par le
  rapport final.

---

## 3. Exigences R1–R8

### R1 — Provenance taxonomisée et `skill_id` stable
Chaque skill du périmètre porte `origin` (taxonomie fermée, défaut
`weekly-background`) et un `skill_id` **stable, déterministe** (`'skill_' +
sha256(nom.normalize().lower())[:8]`). La provenance guide la curation ; le
`skill_id` est la clé de jointure des `overlaps_with` et des décisions de curation.
**Acceptance** : le `skill_id` d'une skill renommée ne change pas ; deux skills au
nom équivalent obtiennent le même `skill_id`.

### R2 — Anti-learning
Un prédicat `is_anti_learning` **drop** les candidats non généralisables avant le
drafting :
- **transitoires** : échecs ponctuels non reproductibles (timeout isolé, contention) ;
- **environnement** : prohibitions liées aux locks, permissions, outils manquants ;
- **one-off** : récits d'incident singuliers, non réutilisables ;
- **secret / PR / ticket** : contenus contenant des secrets, références à des PR ou
  tickets spécifiques non généralisables.
**Acceptance** : un finding `is_anti_learning=true` ne produit aucune skill (test
`test_anti_learning_drop`).

### R3 — Consolidation umbrella (patch-preferring)
Tout candidat avec des `overlaps_with` déclenche un **PATCH** de la skill existante,
pas une création. Une skill umbrella peut référencer des skills voisines. **Acceptance**
: avec un `overlaps_with` existant, le drafting patche et ne crée pas (test
`test_consolidation_patches_existing`).

### R4 — Cycle curation
La coherence-review (étape 6.5) émet des décisions `archive | merge | pin |
reference` ; le **nouveau** `curation.py` + tool `weekly-skill-curate` les applique.
**Gate** : dry-run par défaut ; application explicite uniquement. **Protection** : une
skill `origin: user` n'est **jamais** archivée/mergée sauf `pin` levé (test
`test_curation_protects_user_origin`).

### R5 — Cadence fine et décroissance
- **Nudge foreground léger** : un rappel faible coût, non bloquant, propose
  d'améliorer une skill quand un pattern récurrent est observé — sans déclencher le
  fork arrière-plan (contre la récursion, voir risques).
- **Décroissance** via `watch-memory.jsonl`, partagé avec la veille : les skills
  non rechargées décroissent (voir R8).
**Acceptance** : le nudge n'est actif qu'en premier plan, jamais dans le fork.

### R6 — Gate de vérification procédurale
`commit-draft` enrichit `safe_git_write.validate_draft` d'une **gate de contenu** :
- vérifie les **sections obligatoires** du draft (selon le type skill/command) ;
- vérifie le **champ `verification`** (comment la skill est vérifiée) ;
- échoue sur contenu procédural manquant / incohérent.
La gate de **portabilité** reste appliquée. **Acceptance** : un draft sans section
obligatoire ou sans champ `verification` est **refusé** par `commit-draft`.

### R7 — Boucle usage → raffinement
La **catégorisation `skill-improvement`** (étape 3) alimente le drafting ; la
coherence-review (étape 6.5) renvoie vers l'étape 4 les skills à raffiner selon
l'usage réel (`load_count`, `last_loaded`). La boucle est **fermée** : on ne se
contente pas de créer, on réajuste selon l'adoption. **Acceptance** : une skill à
`load_count` faible et raffinable réapparaît comme candidat au run suivant.

### R8 — TTL / décroissance
- **Archive** une skill si `last_loaded` > **90 jours**, ou si `load_count == 0`
  sur **3 runs** consécutifs.
- **Pin exempté** : `ttl_policy: pin` (ou `origin: user` pinnée) échappe à la
  décroissance.
- **Registre** : réutilise `watch-memory.jsonl` (déjà utilisé pour la décroissance
  de la veille) comme registre de décroissance des skills.
**Acceptance** : une skill non chargée > 90 j (ou 3 runs à zéro) est proposée à
l'archivage sauf si pinnée (test `test_ttl_archive_stale`).

---

## 4. Découpage d'implémentation

| Fichier / outil | Modification |
|---|---|
| `candidates.py` | + `is_anti_learning` (R2), + calcul `skill_id` (R1), + `consolidate_candidates` (R3) |
| `draft_targets.py` | propager `skill_id` (sans le recalculer ni le stocker comme source — il vit dans drafting/frontmatter, R1) |
| `curation.py` | **NOUVEAU** : applique archive/merge/pin selon les décisions de la coherence-review (R4) |
| tool `weekly-skill-curate` | **NOUVEAU** : outil gated (dry-run par défaut) pilotant `curation.py` |
| `weekly-drafting` SKILL.md | documenter CONSOLIDATE/patch-preferring, skill_id, gate procédurale |
| `weekly-quality-audit` SKILL.md | documenter pré-filtre anti-learning + cat `skill-improvement` |
| `weekly-coherence-review` SKILL.md | documenter l'émission des décisions archive/merge/pin/reference |
| `commit-draft` | gate `safe_git_write.validate_draft` (sections + champ `verification`, R6) |
| tests | `test_anti_learning_drop`, `test_consolidation_patches_existing`, `test_curation_protects_user_origin`, `test_ttl_archive_stale`, `test_provenance_skill_id_stable` |
| docs | ce document + `critique-self-improvement-hermes.md` |

---

## 5. Risques résiduels

- **Over-capture** : malgré le patch-preferring, le volume de skills peut croître —
  mitigé par la curation (R4) et la décroissance (R8).
- **Recursive learning** : la boucle s'auto-alimente. Mitigation : le **nudge est
  désactivé dans le fork** arrière-plan (R5) ; le compteur d'itérations borne la
  boucle.
- **Prompt bloat** : trop de skills → dilution et coût de chargement. Mitigation :
  consolidation umbrella (R3) + archivage (R8).
- **Stale prohibitions** : une règle capturée autrefois devient obsolète. Mitigation :
  TTL/décroissance (R8) + décisions d'archive de la coherence-review (R4).
