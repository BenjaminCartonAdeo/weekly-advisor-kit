---
id: prompt-loop
name: Repeated user prompt (prompt loop)
group: cost
severity: low
scope: user_prompt_repeats
version: "1"
tags: [cost, loop, prompt, insights]
sink: findings
emit:
  recommendation_type: prompt-loop
thresholds:
  min_occurrences: 1
  prompt_min_repeats: 4
---

# Description

{{count}} groupe(s) de prompt utilisateur rejoué ({{max_repeats}} fois au max,
{{sessions}} session(s) contributrice(s), {{avg_chars}} caractères en moyenne) —
l'utilisateur relance parce que la demande n'avance pas.

Détection **additive** : elle complète `agent-loop` (qui porte les arguments
d'outils), elle ne le remplace pas. Un même run peut donc produire les deux
findings sur une session.

# When Triggered

Un enregistrement `user_prompt_repeats` porte
`count >= {{thresholds.prompt_min_repeats}}`.

# How to Improve

Lire les exemples du groupe : soit le contexte manque et il mérite un
skill/command, soit la demande est ambiguë et le she doit préciser le format
attendu une fois pour toutes. `estimated_time_saved_mins` est déjà estimé par
l'agrégateur (répétitions × 2 min) — le gain est déjà compté, ne pas le
recompter. Ne pas dupliquer ces groupes côté drafting : `agent-loop` couvre la
boucle d'outils de la même session.

# Examples

```
{"normalized_preview": "refais le test en i...", "count": 5}  -> triggered
{"normalized_preview": "refais le test en i...", "count": 4}  -> triggered (borne)
{"normalized_preview": "refais le test en i...", "count": 3}  -> clean (plancher agreg)
```

```detect
scan: user_prompt_repeats
match: count >= thresholds.prompt_min_repeats
aggregate:
  occurrences: count(matched)
  pct: ratio(count(matched), total)
  max_repeats: max(matched, "count")
  sessions: max(matched, "sessions_distinct")
  avg_chars: avg(matched, "avg_chars")
  time_saved_mins: sum(matched, "estimated_time_saved_mins")
examples: first(matched, 3, "normalized_preview")
check:
  triggered: count(matched) >= thresholds.min_occurrences
```

```test
- context:
    user_prompt_repeats:
      - normalized_preview: "refais le test en integration, sans cache"
        count: 5
        session_id: "ses_1"
        sessions_distinct: 1
        avg_chars: 118
        estimated_time_saved_mins: 10
  expect: triggered
- context:
    user_prompt_repeats:
      - normalized_preview: "refais le test en integration, sans cache"
        count: 3
        session_id: "ses_2"
        sessions_distinct: 1
        avg_chars: 118
        estimated_time_saved_mins: 6
  expect: clean
- context:
    user_prompt_repeats:
      - normalized_preview: "relance la migration avec le backup complet"
        count: 4
        session_id: "ses_3"
        sessions_distinct: 3
        avg_chars: 96
        estimated_time_saved_mins: 8
      - normalized_preview: "reviens sur le point de securite"
        count: 2
        session_id: "ses_3"
        sessions_distinct: 1
        avg_chars: 82
        estimated_time_saved_mins: 4
  expect: triggered
```

## Conception

**Capacité additive** : aucun équivalent en production aujourd'hui. La règle ne
reprend pas le vieux `agent-loop` (qui scannait ce scope à tort) — elle occupe
le scope que ce faux positif libère.

**Choix du seuil : 4.** Le scope arrive déjà filtré par l'agrégateur
(`user_prompt_repeat_min = 3`, plus `user_prompt_repeat_min_chars = 80`) : un
seuil de 3 rejouerait *chaque* entrée du scope, donc une règle incapable de
rester silencieuse, donc non testable. 4 = le **troisième** rejeu, la demande a
échoué 3 fois sans progrès. En dessous, itérer (relancer, reformuler,
préciser) est un comportement normal. Plus bas que le seuil outils (8) parce
qu'un rejeu utilisateur coûte un tour complet et porte une intention (« ma
demande n'est pas comprise »), là où répéter un outil est souvent une
re-vérification légitime. Cas de test n°3 : 4 déclenche, 2 dans le même lot
reste muet.

**Sévérité `low`, assumée.** (1) Détecteur sans précédent en production : le
taux de faux positifs n'est pas calibré, le seuil est un premier choix à
recalibrer sur des données réelles. (2) Recouvrement partiel avec `agent-loop` :
un `medium` ici ferait doubler le récit de coût sur la même session. (3) La
recommandation est déjà douce (draft de skill/command), donc l'urgence est
faible. C'est une observation à investiguer, pas une violation. Passer en
`medium` quand le seuil aura été recalibré sur un run réel.

**`group: cost`** plutôt que `quality` : même famille de dépense gaspillée que
`agent-loop`, le regroupement du rapport les garde ensemble au lieu de les
répartir sur deux lignes.

**Pas de garde sur `cancel_rate` / `avg_correction_turns`** : ces champs sont
des indices de corroboration, pas des conditions. Les mettre en `match`
transformerait un détecteur lisible en une conjonction de seuils à calibrer sans
donnée — ils restent disponibles pour une version ultérieure.
