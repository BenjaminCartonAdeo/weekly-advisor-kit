---
id: agent-loop
name: Repeated tool arguments (agent loop)
group: cost
severity: medium
scope: tool_argument_loops
version: "3"
tags: [cost, loop, arguments, insights]
sink: findings
emit:
  recommendation_type: agent-loop
  impact_order_of_magnitude: medium
thresholds:
  min_occurrences: 1
  loop_min_repeats: 8
  loop_task_min_repeats: 3
---

# Description

{{count}} outil(s) rejoué(s) avec des arguments identiques dans **une seule session**
(pic le plus fort : {{max_peak}} fois ; total du même bucket sur la fenêtre :
{{max_total}}, dont {{task_loops}} boucle(s) de délégation) — l'agent tourne en boucle
sans avancer. Détection sur les **arguments**, jamais sur la sortie : `raw_output` est
constant pour `edit`/`write`, si bien qu'une règle comparant la sortie classait
chaque édition en boucle (7 findings sur 9 au run 2026-10-03).

La preuve garde **les deux** nombres : c'est `total=13 peak=1` (outil cron, 13 runs
de cron sur la fenêtre) contre `total=13 peak=13` (boucle) qui rend le constat
lisible. Ne garder que le pic perdrait la seule preuve qui discrimine, et ne garder
que le total rendrait la règle toujours allumée.

# When Triggered

Un enregistrement `tool_argument_loops` satisfait
`(tool == "task" and peak >= {{thresholds.loop_task_min_repeats}}) or
(tool != "task" and peak >= {{thresholds.loop_min_repeats}})`.

# How to Improve

Inspecter le résultat de l'appel et borner les re-spawns / lectures répétées :
sortir de la boucle au plus tôt (garde sur l'identique-args), ou faire porter le
contexte manquant par un skill. Portage de `AGENT_LOOP_MIN_REPEATS_DEFAULT` /
`AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT` (`insights._agent_loop_findings`) en seuils
de règle.

# Examples

```
{"tool": "grep", "total": 9,  "peak": 9, "task_threshold": 8}  -> triggered
{"tool": "grep", "total": 7,  "peak": 7, "task_threshold": 8}  -> clean
{"tool": "task", "total": 4,  "peak": 4, "task_threshold": 3}  -> triggered
{"tool": "weekly_run", "total": 13, "peak": 1, "task_threshold": 8} -> clean (13 runs de cron)
```

```detect
scan: tool_argument_loops
match: (tool == "task" and peak >= thresholds.loop_task_min_repeats) or (tool != "task" and peak >= thresholds.loop_min_repeats)
aggregate:
  occurrences: count(matched)
  pct: ratio(count(matched), total)
  max_peak: max(matched, "peak")
  max_total: max(matched, "total")
  task_loops: countWhere(matched, "tool == 'task'")
examples: first(matched, 3, "tool")
targets: pluck(matched, "tool")
check:
  triggered: count(matched) >= thresholds.min_occurrences
```

```test
- context:
    tool_argument_loops:
      - tool: "grep"
        total: 9
        peak: 9
        task_threshold: 8
  expect: triggered
- context:
    tool_argument_loops:
      - tool: "grep"
        total: 7
        peak: 7
        task_threshold: 8
  expect: clean
- context:
    tool_argument_loops:
      - tool: "task"
        total: 4
        peak: 4
        task_threshold: 3
  expect: triggered
- context:
    tool_argument_loops:
      - tool: "weekly_run"
        total: 13
        peak: 1
        task_threshold: 8
  expect: clean
- context:
    tool_argument_loops:
      - tool: "grep"
        total: 9
        peak: 9
        task_threshold: 8
      - tool: "task"
        total: 2
        peak: 2
        task_threshold: 3
  expect: triggered
```

## Conception

**Deux seuils déclarés, pas un.** `task` est un self-spawn : 3 répétitions
signalent déjà une boucle de délégation. Les autres outils vérifient : 8 =
relire 8 fois le même fichier n'est pas un défaut. La comparaison est écrite en
clair dans `match` parce que le DSL n'a pas de `if` : `countWhere(matched,
"tool == 'task'")` sert à la métrique, pas au routage.

**`match` porte sur `peak`, jamais sur `total`.** La fenêtre de 7 jours ne sait
pas distinguer « 13 runs de cron » de « 13 appels identiques dans une session
bloquée » : les deux font `total = 13`. Le pic, lui, est par définition
intra-session. Mesuré sur deux résumés réels : le run 2026-09-16 allumait
**25 outils sur 106**, le run 2026-10-06 **12 sur 124** ; sur le pic, ils
tombent à **6** dans les deux cas. Ce qui sort est exactement le bruit visé :
14 des 19 outils sortis du 2026-09-16 sont des outils cron du plugin
(`weekly_run` `total=13 peak=1`, `weekly_preflight` `total=13 peak=1`,
`weekly_doctor` `total=16 peak=1`…), et les 6 qui restent sont de vraies boucles
(`bash total=17 peak=17`, `invalid 14/14`, `swarmmail_inbox 20/9`).
**Les seuils n'ont pas bougé** (3 / 8) : la donnée n'impose rien de neuf, elle
supprime seulement le bruit qui les rendait illisibles.

**`total` reste publié, sans être comparé.** Un constat `agent-loop` ne se
comprend pas sur un seul nombre : `total=13 peak=1` (cron) contre `total=13
peak=13` (boucle), c'est la paire qui discrimine. Les deux remontent dans
`aggregate` (`max_peak` / `max_total`), donc dans la preuve §7 construite par
`insights._rule_evidence`.

**Les deux nombres décrivent le MÊME bucket.** Le scope (`rule_context._tool_argument_loops`)
retient l'empreinte au plus gros pic (égalité sur `total`, puis sur le nom de
l'empreinte pour le déterminisme) et publie ses deux nombres. Prendre le maximum
du pic et le maximum du total indépendamment publierait des paires qu'aucune
empreinte n'a eues (`total=20 peak=9`), ce qui nuirait précisément à la preuve
qu'on cherche à clarifier.

**Compat ascendante : bucket `int` ancien ⇒ `peak = total`.** Les artifacts déjà
écrits (`weekly-summary-2026-09-16.json` et suivants) portent des buckets entiers,
sans décomposition intra-session — l'information n'y existe pas. La rattacher au
total (et non à 0) est le seul choix qui ne change pas le comportement observé de
ces résumés : tout ce qui déclenchait avant déclenche encore. Un résumé sans peak
est donc plus bavard qu'un résumé neuf, jamais moins : il se lit comme avant.

**Le scope expose `task_threshold` comme donnée de référence** (3 pour `task`,
8 sinon — posé par `rule_context.build_context`). La règle ne s'y fie pas : elle
compare à ses propres seuils pour rester retunable via
`load_rules(overrides={"agent-loop": {"thresholds": {"loop_min_repeats": 5}}})`
sans toucher au Python. Le champ reste dans le scope parce que l'adaptateur et
le rapport le publient.

**Deux canaux d'ignore, pas un seul.** Le filtre *par règle*
(`is_rule_ignored`, évalué **avant** la règle, donc une règle ignorée ne coûte
rien) éteint tout : `"agent-loop"` nu, ou une cible listée dans le frontmatter
`ignore` de la règle — `architecture-drift` en garde une (`observation_only`),
`agent-loop` n'en a plus. Le filtre *par cible* (`apply_finding_ignore`, évalué
**après** la règle) ne retire que ce qui porte la cible : `targets:
pluck(matched, "tool")` publie les outils du finding dans `details.targets`, et
`"agent-loop:weekly_run"` supprime le finding dont **toutes** les cibles sont
`weekly_run`, ou réduit ses `examples` aux autres outils s'il en garde au moins
un. C'est la sémantique de l'ancien `_ignored(ignored_findings, "agent-loop",
tool)`, qui portait sur l'entrée et pas sur la règle : ignorer `weekly_run`
n'éteint plus la détection de boucle sur `read`, `skill`, `grep`… Un finding qui
ne déclare pas `targets` n'est jamais supprimé par ce chemin — seul le canal par
règle peut le faire.

**`ignore: [task]` retiré du frontmatter.** C'est ce qui fait que `"task"` et
`"agent-loop:task"` passent désormais par le canal par cible, comme n'importe quel
autre outil : ils retirent les findings dont la cible est `task` et laissent
survivre ceux qui en portent une autre. Le canal par règle reste entier — c'est
`"agent-loop"` nu qui éteint tout. Épinglé par
`tests/test_insights.py::test_agent_loop_declares_no_rule_level_ignore_target`
(l'absence de la clé) et
`tests/test_insights.py::test_ignored_findings_are_merged_into_extra_and_filter_by_target`
(la forme cible : `read` survit, un finding qui ne porte que `task` disparaît).

**`pluck(matched, "tool")` plutôt qu'une liste écrite en dur.** Le DSL n'avait
aucune forme « liste de cibles par finding » : `first(matched, 3, "tool")`
tronque à 3 exemples et `min`/`max` renvoient un scalaire. `pluck` retourne tous
les scalaires du sélecteur, saute les lignes sans le champ (comme `first`) et
refuse un scalaire en premier argument (comme `min`/`max`/`sum`/`avg`) — une cible
erronée est une erreur rapportée, pas une boucle muette.

**Régression couverte par le cas de test n°5** : `grep` à 9 (≥ 8) déclenche
pendant que `task` à 2 (< 3) reste muet — un seuil unique ne ferait pas le tri
par outil. Le cas n°4 verrouille l'autre moitié du changement : `weekly_run` à
`total = 13` mais `peak = 1` reste **silencieux** ; une règle qui retombait sur le
total l'allumerait et le constat resterait illisible.

**`version` portée à "3"** : `version` est la provenance sémantique du finding.
Elle valait "1" pour une détection sur les prompts utilisateur, "2" quand le
câblage a porté la détection sur les **arguments** des outils, "3" depuis que la
règle matche sur le **pic intra-session** au lieu du total de fenêtre — un finding
`agent-loop` ne se lit plus de la même façon, et le dire « 2 » serait faux. Aucun
consommateur comportemental n'en dépend (recopiée dans `details["version"]` par
`rule_pipeline.evaluate_rule`, jamais branchée) : le bump est une honnêteté de
provenance, pas un changement de comportement.
`tests/test_rules.py::test_load_rule_single_file_shipped` épingle « 3 ».
**Elle reste « 3`** après l'ajout de `targets` : la détection, ses seuils et sa
preuve sont inchangés, seul le filtrage d'ignore gagne une granularité — le
finding se lit exactement comme avant.

