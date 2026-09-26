---
id: code-non-relu
name: Edited code never reviewed
group: quality
severity: medium
scope: session_classifications
version: "1"
tags: [quality, review, p6]
thresholds:
  min_occurrences: 1
  review_gap_seconds: 30
---

# Description

{{count}} session(s) ont édité du code sans jamais le relire (0 % d'édits suivis
d'un tour utilisateur après {{thresholds.review_gap_seconds}} s).

# When Triggered

Une `session_classification` porte `production_review_measured > 0` et
`production_review_pct == 0.0` (le champ reste `null` quand le harnais n'expose
pas les timestamps — jamais 0 inventé).

# How to Improve

Relire systématiquement les édits avant de continuer : portage du détecteur
`classify_production_review` (`classifiers.py`) en règle déclarative.

# Examples

```
{"session_id": "s1", "production_review_measured": 4, "production_review_pct": 0.0} -> triggered
{"session_id": "s2", "production_review_measured": 4, "production_review_pct": 0.5} -> clean
```

```detect
scan: session_classifications
match: production_review_measured > 0 and production_review_pct != null and production_review_pct == 0.0
aggregate:
  occurrences: count(matched)
  pct: ratio(count(matched), total)
  measured_edits: sum(matched, "production_review_measured")
examples: first(matched, 3, "session_id")
check:
  triggered: count(matched) >= thresholds.min_occurrences
```

```test
- context:
    session_classifications:
      - session_id: "s1"
        production_review_measured: 4
        production_review_pct: 0.0
  expect: triggered
- context:
    session_classifications:
      - session_id: "s2"
        production_review_measured: 4
        production_review_pct: 0.5
  expect: clean
- context:
    session_classifications:
      - session_id: "s3"
        production_review_measured: 0
        production_review_pct: null
  expect: clean
```
