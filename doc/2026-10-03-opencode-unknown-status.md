# Note upstream — opencode affiche « Unknown » à la place du résultat d'un outil

> Date : 2026-10-03 · Source : log du run cron `2026-10-03-71b8769f`
> (`~/log/weekly/weekly-advisor-2026-10-03T14-00-01+02-00.log`) · Statut : signalement,
> **aucune correction dans ce dépôt** (comportement externe au kit weekly-advisor).

## Comportement observé

Dans la sortie d'un run opencode, la colonne « statut » du renderer affiche
`Unknown` pour certains appels d'outils :

```text
⚙ weekly_preflight Unknown
⚙ weekly_doctor Unknown
⚙ weekly_run Unknown
⚙ weekly_audit_candidates Unknown
⚙ weekly_report_contract Unknown
⚙ weekly_self_cost Unknown
```

Les outils concernés sont ceux du kit weekly-advisor qui, **à cette date**, ne
prenaient **aucun argument**. Le kit en compte aujourd'hui quatre
(`weekly_preflight`, `weekly_doctor`, `weekly_report_contract`,
`weekly_self_cost`). Les outils appelés avec un argument affichent cet argument
à la même place :

```text
⚙ weekly_report_blocks_check {"anchor":"2026-10-03"}
✗ weekly_report_assemble {"anchor":"2026-10-03"} failed
```

Interprétation retenue (cohérente avec l'observation) : opencode affiche
**l'argument** de l'appel d'outil, **pas son résultat**. Un appel sans argument
n'a rien à afficher → `Unknown`.

## Impact sur la lecture des logs

`Unknown` peut être lu à tort comme un statut d'échec : un audit du log du
2026-10-03 a d'abord interprété ces six lignes comme six pannes, alors que
toutes les étapes correspondantes avaient réussi. Le statut réel d'une ligne
n'est pas la colonne statut : il est porté par le **préfixe** (`⚙` = exécuté
sans erreur, `✗` = échec, `FATAL`/`FATALE` = arrêt). La confusion est
coûteuse pour quiconque audite un run à partir du log brut.

## Demande formulée

Côté opencode, clarifier ou corriger l'affichage :

1. afficher le **résultat** (ou au minimum un statut explicite) au lieu de
   l'argument dans la colonne statut ; ou
2. si l'argument reste l'affichage prévu, remplacer `Unknown` par un libellé
   non ambigu (ex. « sans argument ») pour qu'il ne soit pas confondu avec un
   échec.

## Périmètre

Ce comportement est **externe** au kit weekly-advisor : le renderer des logs
appartient à opencode, pas au kit. Le kit ne peut pas le corriger — il peut
seulement documenter l'interprétation correcte des logs (voir `INSTALL.md`,
section « Lire un log de run ») et remonter le problème upstream.