---
id: daily-spike
name: Daily cost spike
group: cost
severity: medium
scope: daily_spikes
version: "2"
tags: [cost, spike, zscore, insights]
sink: alerts
emit:
  recommendation_type: daily-spike
thresholds:
  min_occurrences: 1
  z_min: 3.0
  z_cap: 10.0
alert:
  signal: max_raw_z
  threshold: z_min
  cap: z_cap
  rows: spikes
  row_signal: raw_z
  row_day: day
---

# Description

{{count}} journée(s) au-dessus du seuil de z-score robuste ({{thresholds.z_min}})
sur le coût quotidien — pic anormal à expliquer, pas à ignorer. Le z le plus
fort observé vaut {{max_raw_z}} ({{max_cost_usd}} USD).

Note « MAD≈0, z borné » : quand le MAD de la baseline vaut 0, le z-score robuste
explose (99,19 observé) — c'est un artefact de baseline, pas une dépense plus
forte. `raw_z` est donc exposé **non borné** et la cap d'affichage
({{thresholds.z_cap}}) est appliquée en aval, selon le bloc `alert:` ci-dessous.

`spikes` publie les N lignes `(day, raw_z)` **sans troncature** : l'adaptateur en
déduit UNE alerte par jour qui spike, chacune avec son propre `day` et son propre
`observed` (borné par `z_cap`). `days` reste la version tronquée à 5 utilisée pour
les exemples.

Le bloc frontmatter `alert:` porte le rendu de l'alerte (`signal`, `threshold`,
`cap`, `rows`, `row_signal`, `row_day`) : la règle est propriétaire de sa forme de
sortie, l'adaptateur Python ne fait que la lire et poser la cap. Le nom d'alerte
affiché est l'id de la règle (souligné), jamais un alias maintenu à part.

# When Triggered

Un enregistrement `daily_spikes` porte `raw_z >= {{thresholds.z_min}}`.
Une baseline vide donne `raw_z = 0.0` à toutes les journées : la règle reste
silencieuse sans historique, jamais de pic inventé.

# How to Improve

Identifier la session ou la campagne responsable du pic et vérifier qu'il est
légitime (grosse feature, migration, campagne one-shot). Si le jour est attendu,
l'absorber dans la baseline plutôt que de le laisser gonfler les z suivants.
Portage de `_daily_spike_alerts` (`insights.py`, `z_min` + cap
`DAILY_SPIKE_Z_CAP`) en seuils de règle.

# Examples

```
{"day": "2026-09-10", "cost_usd": 12.0, "raw_z": 11.5}  -> triggered
{"day": "2026-09-11", "cost_usd": 4.0,  "raw_z": 2.4}   -> clean
{"day": "2026-09-12", "cost_usd": 5.0,  "raw_z": 0.0}   -> clean (baseline vide)
```

```detect
scan: daily_spikes
match: raw_z >= thresholds.z_min
aggregate:
  occurrences: count(matched)
  pct: ratio(count(matched), total)
  max_raw_z: max(matched, "raw_z")
  max_cost_usd: max(matched, "cost_usd")
  days: first(matched, 5, "day")
  spikes: first(matched, count(matched))
examples: first(matched, 5, "day")
check:
  triggered: count(matched) >= thresholds.min_occurrences
```

```test
- context:
    daily_spikes:
      - day: "2026-09-10"
        cost_usd: 12.0
        raw_z: 11.5
  expect: triggered
- context:
    daily_spikes:
      - day: "2026-09-10"
        cost_usd: 12.0
        raw_z: 2.4
      - day: "2026-09-11"
        cost_usd: 4.0
        raw_z: 2.9
  expect: clean
- context:
    daily_spikes:
      - day: "2026-09-12"
        cost_usd: 3.0
        raw_z: 99.19
  extra:
    baseline_days: 6
  expect: triggered
- context:
    daily_spikes:
      - day: "2026-09-13"
        cost_usd: 5.0
        raw_z: 0.0
  expect: clean
```

## Conception

**`sink: alerts` explicite.** Un pic daily_spike part dans le tas `alerts`
(l'agent ne peut pas le « réparer », seulement l'expliquer) et non dans
`findings` ; lu par le câblage via `finding["details"]["sink"]`.

**`raw_z` non borné, `z_cap` déclaré.** La règle *expose* la force du signal et
*déclare* la cap (`thresholds.z_cap = 10.0`, défaut de `DAILY_SPIKE_Z_CAP`) :
l'affichage borné appartient au Python, pas au DSL. Comparer `raw_z` à `z_cap`
comme le faisait la version précédente confondait « z astronomique » et « z
juste-au-dessus-du-seuil » : le seuil de détection est `z_min` (3.0, aligné sur
`InsightsConfig.daily_spike_z_min`).

**Borne `>=` et non `>`** : identique à `_daily_spike_alerts` (`if raw_z >=
z_min`). Le cas de test n°2 verrouille le comportement juste sous le seuil
(2.4 et 2.9), donc une dérive future vers `>` ne passerait pas inaperçue.

**`spikes` = `first(matched, count(matched))`, pas `first(matched, 5, ...)`.**
C'est ce qui distingue le fan-out d'un agrégat : `max_raw_z` est le z MAX sur
tous les jours, `days[0]` le PREMIER jour qui spike — les combiner publiait un
`observed` appartenant à un autre jour que le `day` annoncé, et faisait
disparaître les N-1 autres pics de la liste d'alertes. `first(matched,
count(matched))` renvoie les lignes entières, non tronquées, `day: null`
compris ; l'adaptateur zappe les lignes sans `day` plutôt que d'inventer une
attribution. Portage de la sémantique « une alerte par jour » de
`_daily_spike_alerts`.

**Pas de garde sur `extra.baseline_days`** dans `match`/`check` : une clé absente
de `extra` y lèverait `RuleError`. L'isolation est désormais le contrat de
`rule_pipeline.evaluate_rules` lui-même — le point d'entrée batch que
`insights` appelle : chaque règle y est évaluée sous `try/except Exception` (pas
seulement `RuleError`), l'échec devient un constat `rule-error` en `low`, et la
boucle poursuit avec les règles suivantes. Écrire la garde quand même resterait
correct (et l'est toujours), mais la justification « sinon le run meurt » est
fausse depuis l'évaluation par règle. La baseline vide est déjà traitée en amont
(`raw_z = 0.0`), et le compteur reste lisible en templating
`{{extra.baseline_days}}` (vide si absent, jamais d'exception).
