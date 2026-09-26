---
id: anti-learning
name: Anti-learning pattern
group: drafting
severity: medium
scope: findings
version: "1"
tags: [anti-learning, drafting, r2]
patterns:
  secret: ["secret", "password", "token", "api key", "credential"]
  ref: ["pr #", "ticket", "jira", "gh-"]
  transient: ["transient", "transitory", "flaky", "intermittent", "sporadic", "temporary failure"]
  env: ["environment-specific", "env-specific", "environment specific", "only in staging", "only in dev", "dev-only", "staging-only"]
  oneoff: ["one-off", "one off", "one-time", "one time", "single occurrence", "rare occurrence"]
thresholds:
  min_occurrences: 1
---

# Description

{{count}} finding(s) décrivent un pattern à NE PAS capturer (anti-learning, R2).

# When Triggered

Le texte du finding (description, evidence, recommendation_type — le pseudo-champ
`text`) contient au moins un motif de l'un des 5 groupes : secret, référence
PR/ticket, échec transitoire, spécificité d'environnement, récit one-off.

# How to Improve

Exclure ces findings du drafting (DROP) : ce sont des cas anecdotiques ou
sensibles, jamais des skills réutilisables. Ne pas les promouvoir ni les
consolider.

# Examples

```
description: "leaked API key in the build log"  -> DROP (groupe secret)
description: "flaky test only in staging"       -> DROP (transient + env)
```

```detect
scan: findings
match: containsAny(text, patterns.secret) or containsAny(text, patterns.ref) or containsAny(text, patterns.transient) or containsAny(text, patterns.env) or containsAny(text, patterns.oneoff)
aggregate:
  occurrences: count(matched)
  pct: ratio(count(matched), total)
  groups: length(patterns)
examples: first(matched, 3)
check:
  triggered: count(matched) >= thresholds.min_occurrences
```

```test
- context:
    findings:
      - description: "leaked API key in the build log"
        evidence: "logs/build.txt"
        recommendation_type: skill-candidate
  expect: triggered
- context:
    findings:
      - description: "use pytest fixtures consistently"
        evidence: "tests/skills/test_x.py"
        recommendation_type: skill-candidate
  expect: clean
```
