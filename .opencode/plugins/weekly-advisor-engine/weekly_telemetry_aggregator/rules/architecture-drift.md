---
id: architecture-drift
name: Architecture drift
group: quality
severity: medium
scope: architecture_drift
version: "1"
tags: [quality, drift, insights]
thresholds:
  min_occurrences: 1
  drift_runs: 2
---

# Description

{{count}} skill(s) dérivent depuis {{thresholds.drift_runs}} run(s) consécutif(s)
sans correction — la dérive s'installe et devient structurelle.

# When Triggered

Un enregistrement `architecture_drift` porte
`consecutive_runs >= {{thresholds.drift_runs}}`.

# How to Improve

Corriger le skill ou l'archiver : au-delà du seuil, la dérive n'est plus
accidentelle. Portage de `ARCHITECTURE_DRIFT_RUNS_DEFAULT` (`insights.py`) en
seuil de règle.

# Examples

```
{"skill_id": "skill_ab12cd34", "consecutive_runs": 2} -> triggered
{"skill_id": "skill_ef56gh78", "consecutive_runs": 1} -> clean
```

```detect
scan: architecture_drift
match: consecutive_runs >= thresholds.drift_runs
aggregate:
  occurrences: count(matched)
  pct: ratio(count(matched), total)
  max_runs: max(matched, "consecutive_runs")
examples: first(matched, 3, "skill_id")
check:
  triggered: count(matched) >= thresholds.min_occurrences
```

```test
- context:
    architecture_drift:
      - skill_id: "skill_ab12cd34"
        consecutive_runs: 2
  expect: triggered
- context:
    architecture_drift:
      - skill_id: "skill_ef56gh78"
        consecutive_runs: 1
  expect: clean
```
