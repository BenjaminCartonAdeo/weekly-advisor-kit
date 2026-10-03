---
id: daily-spike
name: Daily cost spike
group: cost
severity: medium
scope: daily_spikes
version: "1"
tags: [cost, spike, insights]
thresholds:
  min_occurrences: 1
  z_cap: 10.0
---

# Description

{{count}} journée(s) dépassent le plafond de z-score ({{thresholds.z_cap}}) sur le
coût quotidien — pic anormal à expliquer plutôt qu'à ignorer.

# When Triggered

Un enregistrement `daily_spikes` porte `z_score >= {{thresholds.z_cap}}`.

# How to Improve

Identifier la session ou la campagne responsable du pic et vérifier qu'il est
légitime (grosse feature, run de migration). Portage de `DAILY_SPIKE_Z_CAP`
(`insights.py`) en seuil de règle.

# Examples

```
{"date": "2026-09-10", "z_score": 11.5} -> triggered
{"date": "2026-09-11", "z_score": 3.0}  -> clean
```

```detect
scan: daily_spikes
match: z_score >= thresholds.z_cap
aggregate:
  occurrences: count(matched)
  pct: ratio(count(matched), total)
  max_z: max(matched, "z_score")
  max_cost: max(matched, "cost_usd")
examples: first(matched, 3, "date")
check:
  triggered: count(matched) >= thresholds.min_occurrences
```

```test
- context:
    daily_spikes:
      - date: "2026-09-10"
        z_score: 11.5
        cost_usd: 12.0
  expect: triggered
- context:
    daily_spikes:
      - date: "2026-09-11"
        z_score: 3.0
        cost_usd: 4.0
  expect: clean
```
