---
id: prompt-maturity-f
name: Prompt maturity grade F
group: quality
severity: low
scope: session_classifications
version: "1"
tags: [quality, prompt-maturity, p6]
thresholds:
  min_occurrences: 1
  grade: F
---

# Description

{{count}} session(s) obtiennent le grade de maturité de prompt F
(score 0-100, seuil A≥80 B≥65 C≥50 D≥40).

# When Triggered

Une `session_classification` porte `prompt_maturity_grade == "{{thresholds.grade}}"`.

# How to Improve

Reformuler les prompts : spécificité, contexte, contrainte, vérifiabilité,
itérativité. Portage de `score_prompt_maturity` / `grade_for_score`
(`classifiers.py`) en règle déclarative.

# Examples

```
{"session_id": "s1", "prompt_maturity_grade": "F", "prompt_maturity_score": 20} -> triggered
{"session_id": "s2", "prompt_maturity_grade": "B", "prompt_maturity_score": 70} -> clean
```

```detect
scan: session_classifications
match: prompt_maturity_grade == thresholds.grade
aggregate:
  occurrences: count(matched)
  pct: ratio(count(matched), total)
  min_score: min(matched, "prompt_maturity_score")
examples: first(matched, 3, "session_id")
check:
  triggered: count(matched) >= thresholds.min_occurrences
```

```test
- context:
    session_classifications:
      - session_id: "s1"
        prompt_maturity_grade: "F"
        prompt_maturity_score: 20
  expect: triggered
- context:
    session_classifications:
      - session_id: "s2"
        prompt_maturity_grade: "B"
        prompt_maturity_score: 70
  expect: clean
```
