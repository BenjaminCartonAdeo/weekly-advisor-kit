---
id: agent-loop
name: Repeated user prompt (agent loop)
group: cost
severity: high
scope: user_prompt_repeats
version: "1"
tags: [cost, loop, insights]
thresholds:
  min_occurrences: 1
  loop_min_repeats: 8
---

# Description

{{count}} groupe(s) de prompts répétés atteignent le seuil d'agent-loop
({{thresholds.loop_min_repeats}} répétitions) — coût gaspillé et signal d'un
manque de contexte ou d'une boucle de correction.

# When Triggered

Un enregistrement `user_prompt_repeats` porte `count >= {{thresholds.loop_min_repeats}}`.

# How to Improve

Créer ou améliorer un skill/command qui fournit le contexte manquant, ou
clarifier la consigne de départ. Portage de `AGENT_LOOP_MIN_REPEATS_DEFAULT`
(`insights.py`) en seuil de règle.

# Examples

```
{"normalized_preview": "refais le test", "count": 9}  -> triggered
{"normalized_preview": "corrige le lint", "count": 3} -> clean
```

```detect
scan: user_prompt_repeats
match: count >= thresholds.loop_min_repeats
aggregate:
  occurrences: count(matched)
  pct: ratio(count(matched), total)
  max_repeats: max(matched, "count")
  estimated_time_saved_mins: sum(matched, "estimated_time_saved_mins")
examples: first(matched, 3, "normalized_preview")
check:
  triggered: count(matched) >= thresholds.min_occurrences
```

```test
- context:
    user_prompt_repeats:
      - normalized_preview: "refais le test"
        count: 9
        session_id: "s1"
  expect: triggered
- context:
    user_prompt_repeats:
      - normalized_preview: "corrige le lint"
        count: 3
        session_id: "s2"
  expect: clean
```
