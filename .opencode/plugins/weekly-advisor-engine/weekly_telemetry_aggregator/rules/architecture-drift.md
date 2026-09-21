---
id: architecture-drift
name: Architecture drift (observation)
group: quality
severity: low
scope: architecture_drift
version: "1"
tags: [quality, drift, observation, insights]
sink: findings
ignore: [observation_only]
emit:
  recommendation_type: architecture-drift
  action: recalibrate
  observation_only: true
thresholds:
  min_occurrences: 1
  drift_runs: 2
---

# Description

{{count}} dérive(s) d'architecture persistante sur {{drift_runs}} run(s)
consécutif(s) : les champs stables (state_counts, config, inventory_counts,
harness_scope) ne reviennent pas à leur valeur précédente. **Observation only** —
`action: recalibrate`, relecture humaine, aucune correction automatique.

# When Triggered

L'entrée unique du scope `architecture_drift` satisfait
`length(changed_fields) > 0 and drift_runs >= drift_threshold`
(défaut : `drift_threshold = {{thresholds.drift_runs}}`).

# How to Improve

Relire à la main l'état déclaré / observé / absent et la portée du harness :
confirmer que le changement est voulu, puis recalibrer l'observation pour que
le run suivant ne reproduise plus la dérive. Portage de
`_architecture_drift_finding` (`insights.py`, 4 champs stables + streak de runs)
en règle observation-only.

# Examples

```
{"changed_fields": ["state_counts"], "drift_runs": 3, "drift_threshold": 2}  -> triggered
{"changed_fields": ["config"],     "drift_runs": 1, "drift_threshold": 2}  -> clean
{"changed_fields": [],             "drift_runs": 0, "drift_threshold": 2}  -> clean
```

```detect
scan: architecture_drift
match: length(changed_fields) > 0 and drift_runs >= drift_threshold
aggregate:
  occurrences: count(matched)
  pct: ratio(count(matched), total)
  drift_runs: max(matched, "drift_runs")
  has_observation: everyWhere(matched, "length(observation) > 0")
examples: first(matched, 1, "changed_fields")
check:
  triggered: count(matched) >= thresholds.min_occurrences
```

```test
- context:
    architecture_drift:
      - changed_fields: ["state_counts", "config"]
        drift_runs: 3
        drift_threshold: 2
        observation:
          state_counts: 12
          config: 4
  expect: triggered
- context:
    architecture_drift:
      - changed_fields: ["config"]
        drift_runs: 1
        drift_threshold: 2
        observation:
          config: 4
  expect: clean
- context:
    architecture_drift:
      - changed_fields: []
        drift_runs: 0
        drift_threshold: 2
        observation: {}
  expect: clean
```

## Conception

**Le scope n'est jamais vide** (une entrée, filet de sécurité de
`rule_context`) : la règle peut donc comparer `drift_runs` à un seuil sans
compter sur `count(matched) >= 1` comme le faisait la version précédente, qui
comptait des « skills » alors que la dérive porte sur des champs, pas des skills.

**Seuil effectif = `drift_threshold`** (donnée du scope, = la valeur de config
du run, minimum 1). `thresholds.drift_runs: 2` en est le défaut documenté et
surchargeable — mais le DSL n'a **pas de `max()`/`min()` scalaire**, donc pas de
moyen d'exprimer « le plus contraignant des deux » dans une expression. Choix :
aligner sur la production (`drift_threshold`), et documenter l'écart plutôt que
de le contourner. Lacune DSL signalée au coordinateur : un `max()`/`min()`
scalaire permettrait d'écrire `max(drift_threshold, thresholds.drift_runs)` et de
supprimer cette ambiguïté de double réglage.

**`observation_only: true` dans `emit`, pas dans `emit.observation` :**
`action: recalibrate` et le marqueur sont deux clés top-level comme dans la
finding de référence (`insights._architecture_drift_finding`), ce qui permet au
câblage de router sans connaître la règle.

**`ignore: [observation_only]` — cible pertinente.** La finding *est* une
observation : un utilisateur qui ne veut pas de bruit observation-only peut
l'écarter via `ignored_findings: ["architecture-drift:observation_only"]` (ou
`["observation_only"]`). C'est additif — la référence Python
(`_architecture_drift_finding`) ne reçoit pas encore la liste d'ignore — donc
aucun comportement existant n'est modifié, la cible ne mord que si la chaîne
apparaît explicitement.

**`examples` rend `['state_counts']`** (repr Python de la liste de champs) : le
DSL n'a pas de `join`, et `first(matched, 1, "changed_fields")` est la seule
voie pour sortir les noms de champs. Lisible tel quel dans le rapport ; un
`join` DSL rendrait `state_counts, config`.
