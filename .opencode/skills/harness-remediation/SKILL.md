---
name: harness-remediation
description: Analyse les findings harness-eval et prépare/applique uniquement les corrections à haute confiance autorisées par la gate déterministe.
metadata:
  authored_by: opencode-weekly-advisor
  skill_class: pipeline-step
---

# Harness remediation — étape 5.5

Cette étape transforme le digest `harness-eval` en décisions explicites. Elle ne donne
jamais au LLM un droit implicite d'édition : le LLM écrit un fichier de propositions,
puis `weekly_harness_remediate` décide déterministiquement ce qui peut être appliqué.

## Entrées

- `weekly-harness-digest-<date>.json` — findings du scan allowlisté, avec `path`, `rule`,
  `severity`, `message` et `suggestion`
- `weekly-telemetry-config.json` — `harness_auto_fix_rules` et
  `harness_auto_fix_max_files`
- le contenu des seuls fichiers ciblés par les findings

## Sorties

Écrire d'abord :

```text
reports/weekly-harness-remediation-proposals-<date>.json
```

Puis appeler obligatoirement :

```text
weekly_harness_remediate({
  proposal_file: "<chemin absolu>",
  mode: "apply"
})
```

Le tool écrit ensuite :

```text
reports/weekly-harness-remediation-<date>.json
```

Le résultat explique chaque décision (`applied`, `proposed`, `manual`, `blocked`,
`rolled_back`) et contient le post-check lorsqu'une correction a été appliquée.

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
la cible `.opencode/skills/` ou `.opencode/commands/`, une seule occurrence et une
limite de taille.

## Règles de sécurité

- Ne jamais appliquer automatiquement une règle `security/*`, même avec `confidence=high`.
- Ne jamais éditer directement un fichier pendant cette étape.
- Ne jamais modifier `.opencode/opencode.json`, un agent, un plugin, la config moteur,
  la base SQLite, la CI ou un fichier hors worktree.
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
