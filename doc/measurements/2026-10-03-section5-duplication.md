# Section 5 du rapport final — mesure de duplication (2026-10-03)

Question posée par le lot : la section 5 « Santé de l'environnement (harness + cohérence) »,
non bornée, est passée de 63 à 142 lignes entre deux runs. **Faut-il la plafonner à 12 lignes ?**

Réponse mesurée : **non.** Le ratio lignes / findings distincts est de **1,33** (agrégat) et de
**1,31** sur le run le plus récent — sous le seuil de 1,5. **Aucun plafond n'a été implémenté.**
La croissance 63 → 142 vient d'ailleurs, et le diagnostic est reproduit en §4.

---

## 1. Méthode

### 1.1 Sources

| Run | Artefacts | Rapport rendu |
|---|---|---|
| `2026-09-17-888ed03e` | `weekly-coherence-findings-2026-09-17.json` | `weekly-report-2026-09-17.md` |
| `2026-10-01-e46a9c3b` | `weekly-coherence-findings-2026-10-01.json` | `weekly-report-2026-10-01.md` |
| `2026-10-03-71b8769f` | `weekly-coherence-findings-2026-10-03.json` | `weekly-report-2026-10-03.md` |

Racine : `/home/benjamin/Dev/Adeo/reports/runs/<run>/`.

### 1.2 Périmètre des lignes comptées

Section 5 = toutes les lignes comprises entre `## 5. …` et `## 6. …` (bornes exclusives),
dans le **rapport rendu** (pas dans le gabarit). Deux conventions de comptage sont données :
la section 5 **avec** sa ligne de titre `## 5.` (convention du ticket : 63 / 142) et **sans**
(la ligne de titre comptée à part).

Décomposition par sous-bloc, via les marqueurs `###` / `####` du gabarit :

| Sous-bloc | 2026-09-17 | 2026-10-01 | 2026-10-03 |
|---|---:|---:|---:|
| `### Cohérence de l'environnement` (boucle `coherence_items`) | 30 | 42 | **40** |
| `#### Curation (WAVE 2.5 …)` | 18 | 17 | **98** |
| Préambule + lignes vides | 3 | 3 | 3 |
| **Section 5, titre inclus** | **52** | **63** | **142** |
| **Section 5, titre exclu** | 51 | 62 | 141 |

Les valeurs du ticket (63 → 142) sont donc reproduites exactement avec la convention
« titre inclus ». Le tableau §2 garde la convention **titre inclus** pour rester comparable
au ticket.

### 1.3 Ce qu'est un « finding distinct »

Deux clés de déduplication sont calculées, parce que le résultat dépend de la clé et qu'il
faut donc les deux :

- **`distinct-sem`** = findings distincts sur `(category, description, recommendation)`.
  C'est la clé de référence : deux findings ne sont le même constat que s'ils décrivent
  le même problème **et** demandent la même action. `evidence_summary` est de la provenance,
  pas du constat — deux findings qui ne diffèrent que par leur ligne de preuve JSON
  (`weekly-insights-2026-10-01.json:282-291` vs `:292-301`) sont le même constat.
- **`distinct-rendu`** = findings distincts sur la **ligne rendue**, reproduite à l'identique
  du gabarit (`report_template.md.j2:150-152`, preuve tronquée à 110 caractères). Clé
  conservative : ne compte comme doublon que ce qui est **octet pour octet identique** à
  l'écran.

Le ratio demandé — lignes de section 5 / findings distincts — est calculé sur la **boucle
`coherence_items`**, et non sur la section 5 entière. Raison : c'est la seule partie qu'un
plafond de 12 lignes pourrait réduire. La section 5 contient aussi la curation et le digest
harness, qui ne sont pas des findings de cohérence ; les inclure au dénominateur ferait
mesurer « la section 5 a d'autres sous-blocs », pas « les findings sont dupliqués ».
Les deux lectures sont données en §2.4.

---

## 2. Valeurs brutes

### 2.1 Findings dans les artefacts (JSON)

| Run | findings bruts | `distinct-sem` | `distinct-rendu` |
|---|---:|---:|---:|
| 2026-09-17-888ed03e | 28 | 28 | 28 |
| 2026-10-01-e46a9c3b | 40 | 23 | 40 |
| 2026-10-03-71b8769f | 38 | 29 | 29 |

### 2.2 Lignes rendues par la boucle `coherence_items`

Une ligne rendue par finding — la boucle est « une ligne par finding », donc
lignes rendues == findings bruts, pour les trois runs.

| Run | lignes rendues | findings bruts | `distinct-sem` | `distinct-rendu` |
|---|---:|---:|---:|---:|
| 2026-09-17-888ed03e | 28 | 28 | 28 | 28 |
| 2026-10-01-e46a9c3b | 40 | 40 | 23 | 40 |
| 2026-10-03-71b8769f | 38 | 38 | 29 | 29 |

### 2.3 Lignes de section 5 (les deux lectures)

| Run | section 5 (titre inclus) | section 5 (titre exclu) | findings distincts (`distinct-sem`) |
|---|---:|---:|---:|
| 2026-09-17-888ed03e | 52 | 51 | 28 |
| 2026-10-01-e46a9c3b | 63 | 62 | 23 |
| 2026-10-03-71b8769f | 142 | 141 | 29 |

### 2.4 Les ratios

**Lecture B — boucle `coherence_items` (celle que le plafond pourrait borner).**
C'est la métrique de décision.

| Run | lignes rendues / `distinct-sem` | lignes rendues / `distinct-rendu` |
|---|---:|---:|
| 2026-09-17-888ed03e | **1,00** | 1,00 |
| 2026-10-01-e46a9c3b | **1,74** | 1,00 |
| 2026-10-03-71b8769f | **1,31** | 1,31 |
| **agrégat (106 / 80)** | **1,33** | 1,09 |
| moyenne des 3 runs | 1,35 | 1,10 |
| médiane des 3 runs | 1,31 | 1,00 |

**Lecture A — section 5 entière / `distinct-sem`** (dénominateur et numérateur non
homogènes ; donné pour complétude, **non** retenu pour la décision).

| Run | section 5 (titre inclus) / `distinct-sem` |
|---|---:|
| 2026-09-17-888ed03e | 1,86 |
| 2026-10-01-e46a9c3b | 2,74 |
| 2026-10-03-71b8769f | 4,90 |

---

## 3. Décision

**Seuil : ratio ≥ 1,5. Mesure retenue : 1,33 (agrégat) / 1,31 (run 2026-10-03).**
→ **Sous le seuil. La duplication n'est pas établie. Aucun plafond implémenté.**

Aucun changement de `report.py`, du gabarit ni de `tests/test_report.py`. Ce document est
le seul livrable de code du lot.

### 3.1 Ce qui pushes le ratio au-dessus du seuil, et pourquoi on ne l'a pas retenu

Le ratio **le plus défavorable** à la duplication est 1,74 (run 2026-10-01), au-dessus du
seuil. Il repose sur la clé `distinct-sem` : ce run émet 12 findings `stale` de description
et de recommandation **identiques** (« Jamais chargé (8/8) mais ttl_policy=pin : protection
explicite, aucun archive/delete. »), qui ne diffèrent que par la plage de lignes JSON citée
en preuve. Sur `distinct-rendu` (ligne réellement affichée), la preuve tronquée à 110
caractères diffère encore, donc le ratio de ce run tombe à 1,00.

Autrement dit : le seul run au-dessus du seuil est celui où la duplication est **invisible à
la lecture** — le lecteur voit 12 lignes apparemment différentes (chacune avec sa référence
de preuve) qui ne demandent pourtant rien de différent. C'est un défaut réel, mais il porte
sur **la matière des findings** (le producteur de la passe de cohérence réémet un constat
par skill au lieu d'un constat par cohorte), pas sur le rendu. **Le bon endroit pour le
traiter est `weekly-coherence-review`, pas le gabarit du rapport.**

C'est aussi ce que montre le run 2026-10-03 : 11 findings `unused-unreferenced` de
description identique (« Skill jamais chargé mais protégé par une politique TTL pin ») et
8 autres de description identique (« Skill jamais chargé et référencé nulle part »), mais
dont la **recommandation** nomme le skill concerné. Agréger par `(category, description)`
perdrait le skill à traiter ; la clé `distinct-sem`, qui inclut la recommandation, les garde
séparés — d'où 29 distincts et non 21.

### 3.2 Coût informationnel d'un plafond à 12 lignes

Le plafond demandé est de 12 lignes. Sur les trois runs, la boucle rend 28 / 40 / 38 lignes :

| Run | findings | plafonné à 12 | findings non affichés | part perdue |
|---|---:|---:|---:|---:|
| 2026-09-17-888ed03e | 28 | 12 | 16 | 57 % |
| 2026-10-01-e46a9c3b | 40 | 12 | 28 | 70 % |
| 2026-10-03-71b8769f | 38 | 12 | 26 | 68 % |

Ce n'est pas un regroupement par catégorie qui rendrait cela à 12 lignes sans perte : les
recommandations de la section 5 **nomment chacune le skill à traiter**
(« Archiver `.opencode/skills/cli-builder/SKILL.md` après revue »), et ces findings sont
l'entrée de `weekly_skill_curate` via `tag_action`. Une agrégation par catégorie — que
la section 7 n'applique pas non plus (liste plate triée par catégorie puis sévérité via
`_sort_maintenance_findings`, commit `d5ad5c6`) — perdrait
ici précisément le détail par skill qui fait la section actionnable, tout en restant ≥ 12
lignes : les trois runs ont 4, 6 et 4 catégories distinctes seulement.

Autrement dit : sur la section 5, le plafond à 12 lignes n'est pas atteignable *sans perte
d'information actionnable*, et la duplication mesurée ne justifie pas ce prix.

---

## 4. Ce que la croissance 63 → 142 explique réellement

La croissance est **intégralement** dans le bloc `#### Curation (WAVE 2.5)`, pas dans la
boucle `coherence_items` :

| Sous-bloc | 2026-10-01 | 2026-10-03 | Delta |
|---|---:|---:|---:|
| `### Cohérence de l'environnement` (`coherence_items`) | 42 | 40 | **−2** |
| `#### Curation (WAVE 2.5)` | 17 | 98 | **+81** |
| **Section 5 total (titre inclus)** | **63** | **142** | **+79** |

La boucle `coherence_items` n'a pas grandi : elle a **rétréci** de 2 lignes. Les 79 lignes
gagnées viennent du bloc curation.

### 4.1 Cause : le commit `c2a07ce` a ajouté `{{ "\n" }}` dans la boucle des décisions

Le gabarit est identique avant et après (le bloc n'a pas été touché par `22531c6` ni par
`bc5cc35`). La différence de rendu vient de `c2a07ce`
(`fix(report): fenetre des commits du run, ratio securite, lisibilite des annexes`,
2026-10-03), qui a ajouté `{{ "\n" }}` à la fin de la ligne de décision de curation :

```diff
  {% for d in curation_detail_local.decisions|default([]) if d.status|default("") != "skipped" %}
- - `{{ d.action }}` `{{ d.skill_id }}` ({{ d.source }}) — {{ d.reason }}{% if d.status %} [{{ d.status }]{% endif %}
+ - `{{ d.action }}` `{{ d.skill_id }}` ({{ d.source }}) — {{ d.reason }}{% if d.status %} [{{ d.status }]{% endif %}{{ "\n" }}
  {% endfor %}
```

L'Environnement Jinja est configuré `trim_blocks=True, lstrip_blocks=True`
(`report.py:2445-2451`). Effet mesuré sur le fragment exact, avec cette configuration :

| Corps de la boucle | Rendu sur 3 décisions |
|---|---|
| sans `{{ "\n" }}` (état avant `c2a07ce`) | `'- \`a\` x r [s]- \`a\` x r [s]- \`a\` x r [s]'` → **1 ligne**, décisions concaténées |
| avec `{{ "\n" }}` (état actuel) | `'- \`a\` x r [s]\n\n- \`a\` x r [s]\n\n- \`a\` x r [s]\n\n'` → **3 lignes + 2 lignes vides** |

Donc `{{ "\n" }}` corrige un vrai défaut (les décisions étaient illisibles, concaténées sur
une seule ligne dans les runs 09-17 et 10-01) mais **émet `\n\n`** : la boucle produit une
ligne de décision **plus une ligne vide** à chaque itération.

Décompte du bloc curation du run 2026-10-03 : **99 lignes = 36 lignes de décision
(35 non-skipped + 1 skipped) + 46 lignes vides + 17 lignes d'en-tête/totaux.** Soit **46 %
du bloc curation, et 33 % de la section 5, sont des lignes vides.** Dans les runs 09-17 et
10-01, les 27 et 16 décisions tenaient sur **une** ligne chacune : d'où 17 et 17 lignes de
bloc.

**Écart résiduel :** sur les +81 lignes, ~+20 viennent des décisions supplémentaires
(16 → 36) et ~+46 des lignes vides. Le solde s'explique par la tête du bloc.

### 4.2 Suite à donner (non implémentée — hors périmètre de ce lot)

Le correctif est d'une ligne, dans le fichier déjà réservé par ce lot
(`report_template.md.j2:167-169`) : supprimer le `{{ "\n" }}` de la ligne de décision.
`trim_blocks=True` garantit déjà le retour à la ligne en fin d'itération, donc le
comportement « une décision par ligne » est conservé sans les lignes vides. Gain attendu :
**~46 lignes** sur le run 2026-10-03, soit 142 → ~96.

Non implémenté ici pour deux raisons : le lot est « mesurer, puis plafonner **si** la
duplication est établie » — elle ne l'est pas ; et la correction du rendu de la curation
n'est ni un plafond de section 5 ni une duplication de findings, c'est un defect de
rendu whitespace. **À faire dans un lot dédié.**

À noter aussi : la même correction s'applique à la seconde occurrence de `{{ "\n" }}`
ajoutée par `c2a07ce`, dans la boucle des règles harness
(`report_template.md.j2:199-201`), hors section 5.

---

## 5. Reproductibilité

Les chiffres viennent d'un lecture des quatre fichiers listés en §1.1, sans modification des
artefacts. Points de contrôle :

- Section 5 = lignes de `## 5. …` à `## 6. …` exclusive, sur le rapport rendu.
- Une ligne rendue par finding, reproduite depuis `report_template.md.j2:150-152` :
  `- **{tag}** {description} (preuve : {evidence_summary[:110]}) → {recommendation}`,
  avec `tag = finding.tag or finding.category` et la preuve omise si vide.
- Déduplication sur tuple normalisé (espaces collapsés) des champs indiqués en §1.3.
- Le nombre de findings de la boucle est cross-vérifié contre
  `weekly-coherence-findings-<date>.json` → identique aux lignes rendues sur les trois runs.

Les trois rapports ont été rendus par des versions du moteur différentes (§4). La
**boucle `coherence_items`** est identique sur les trois (une ligne par finding, pas de
`{{ "\n" }}`, pas de changement de gabarit entre `a87c75e` et `c2a07ce`), donc la
comparaison des ratios de la boucle est valide. La comparaison des totaux de section 5, elle,
mélange une évolution du moteur et une évolution des données — c'est pourquoi le bloc
curation est décomposé séparément au lieu d'être mis dans le ratio de décision.
