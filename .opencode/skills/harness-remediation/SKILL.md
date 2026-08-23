---
name: harness-remediation
description: Analyse les findings harness-eval du digest et prépare des propositions de correction (étape 5.5 du weekly-advisor). Use when: corrections à haute confiance, validées par la gate déterministe — dry-run par défaut, aucun commit automatique.
metadata:
  authored_by: opencode-weekly-advisor
  skill_class: pipeline-step
---

# Harness remediation — étape 5.5

Cette étape transforme le digest `harness-eval` en décisions explicites. Elle ne donne
jamais au LLM un droit implicite d'édition : le LLM écrit un fichier de propositions,
puis la sous-commande `harness-remediate` du plugin weekly-advisor décide
déterministiquement ce qui peut être appliqué.

## Entrées

- `weekly-harness-digest-<date>.json` — findings du scan allowlisté, avec `path`, `rule`,
  `severity`, `message` et `suggestion`. Depuis la cellule 2.2, le scan couvre aussi les
  répertoires du harnais détecté (`DRAFT_HARNESS_TARGETS`, ex. `.claude/skills`) et le
  contenu engine du kit injecté dans la projection — les fichiers sans source réelle
  sont listés comme **orphelins** dans `draft_targets.orphan_files`.
- `weekly-telemetry-config.json` — `harness_auto_fix_rules` et
  `harness_auto_fix_max_files`
- le contenu des seuls fichiers ciblés par les findings

## Sorties

Écrire d'abord :

```text
<output_dir>/runs/current/weekly-harness-remediation-proposals-<date>.json
(répertoire du run actif — alias stable `runs/current`)
```

Puis appeler obligatoirement la sous-commande `harness-remediate` du plugin
weekly-advisor, avec les paramètres :

```text
  proposal_file: "<chemin absolu>"
  mode: "apply"
```

Le tool écrit ensuite :

```text
<output_dir>/runs/current/weekly-harness-remediation-<date>.json
```

Le résultat explique chaque décision (`applied`, `proposed`, `manual`, `blocked`,
`rolled_back`) et contient le post-check lorsqu'une correction a été appliquée.

## Surface de remédiation (matrice 5.5, cellule 2.2)

Le résultat porte un bloc `draft_target` : `{mode, harnesses, decision, reason}` —
décision pure déduite du harnais résolu (`resolve_remediation_surface`) :

- cible unique `opencode` → `projection` (surface native `.opencode` déjà couverte) ;
- cible unique hors `.opencode` → `portability` (remédiation conditionnée au mapping
  `portability.yaml` — règle YAML elle-même : cellule 3.1) ;
- plusieurs cibles avec `opencode` → `combined` ; sans `opencode` → `portability` ;
- entrée vide ou harnais inconnu → repli sûr `projection`, raison explicite.

Le digest étape 5 expose la même décision dans `draft_targets.surface_decision`, plus
une **baseline findings** (`harness_baseline`) : capturée au premier run dans
`<output_dir>/weekly-harness-baseline.json`, réutilisée telle quelle aux runs suivants
(`status=reused`, nouveaux findings listés dans `new_findings`). Les findings orphelins
(contenu engine projeté sans source réelle) sont des signaux, jamais des cibles d'apply.

## Décision

Pour chaque finding :

1. vérifier le fichier et la ligne indiqués dans le digest ;
2. classer `apply`, `propose`, `manual` ou `dismiss` ;
3. renseigner `confidence` et sa justification ;
4. fournir `old_text` et `new_text` uniquement lorsqu'un patch concret est justifié.

Le fichier brut suit ce format :

```jsonc
{
  "schema_version": 1,
  "date": "YYYY-MM-DD",
  "proposals": [
    {
      "rule": "quality/example-rule",
      "path": ".opencode/commands/example.md",
      "line": 12,
      "decision": "apply",
      "confidence": "high",
      "description": "problème observé",
      "rationale": "correction mécanique et unique",
      "old_text": "texte actuel exact",
      "new_text": "texte corrigé exact"
    }
  ]
}
```

`apply` n'est admissible que pour une correction mécanique, un remplacement exact et
une règle explicitement présente dans `harness_auto_fix_rules`. Le tool impose en plus
la cible `.opencode/AGENTS.md` exact ou `.opencode/{skills,commands,agents,plugins}/`
(hors `plugins/weekly-advisor-engine/` — le moteur n'est jamais une cible), une seule
occurrence et une limite de taille (v6.0.k F2).

`old_text`/`new_text` : chaîne non vide obligatoire pour `apply` ; pour `propose`,
`manual` et `dismiss`, ils peuvent être omis ou `null` (v6.0.k F3).

## Règles de sécurité

- Ne jamais appliquer automatiquement une règle `security/*`, même avec `confidence=high`.
- Ne jamais éditer directement un fichier pendant cette étape.
- Ne jamais éditer un fichier directement pendant cette étape : seules les propositions
  validées par la gate sont appliquées (jamais `.opencode/opencode.json`, jamais le moteur
  `weekly-advisor-engine/`, jamais la base SQLite, jamais la CI, jamais hors worktree).
- Ne jamais inventer une correction à partir du seul message du scanner.
- Une correction sémantique devient `propose` ou `manual`, avec une explication claire.
- Le mode `dry-run` ne modifie jamais le worktree et sert pour une exécution manuelle.
- Aucun commit automatique : les modifications appliquées sont listées dans le résultat
  pour revue humaine.

## Traitement des findings actuels

Les règles suivantes restent `manual` ou `propose` :

```text
security/memory-write-unscoped
security/unbounded-delegation
security/mcp-tool-poisoning
security/reverse-shell
security/data-exfiltration
security/stealth-persistence
security/no-credential-access
```

Si aucune règle n'est autorisée dans `harness_auto_fix_rules`, toutes les propositions
restent bloquées ou proposées ; c'est le comportement sûr par défaut.
