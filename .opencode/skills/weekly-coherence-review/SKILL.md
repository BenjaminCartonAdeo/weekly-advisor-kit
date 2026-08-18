---
name: weekly-coherence-review
description: Passe critique de cohérence environnement (étape 6.5 du weekly-advisor) — état déclaratif de .opencode vs usage réel, findings avec tags action, aucune écriture auto.
metadata:
  authored_by: opencode-weekly-advisor
  skill_class: pipeline-step
---

# Weekly Coherence Review — étape 6.5

Dernière passe LLM : vérifier que l'état **déclaratif** de l'environnement
(`.opencode/{agents,skills,commands}`) colle à son **usage réel**. Lecture seule —
les findings alimentent la revue humaine (fusion, retrait, recalibrage).

## Entrées

- Catalogue déclaratif : `ls`/`read` de `.opencode/{agents,skills,commands}` + frontmatter
  (`skill_catalog` du summary)
- Usage réel : `summary.skill_usage` / `command_usage` / `skills_never_loaded`
- `insights.maintenance` (R1 retire / R3 merge) et le digest harness (triggers/budget)
- Findings de l'étape 3 (sessions coûteuses → cibles de forcing)

## Style

Ponytail-audit : **une ligne par finding**, ranked par impact (le plus gros gain d'abord),
tag action sur chaque finding. `generated_at` : **ancre du run en ISO-8601 UTC** (ex. `2026-08-18T10:39:33Z`, copiée de
`anchor-last.txt`) — jamais minuit, jamais une date seule.

Preuve obligatoire dans `evidence_summary` — sans preuve,
écarté à la revue.

## Catégories

| Catégorie | Détection | Tag action |
|---|---|---|
| `duplicate` | Agents/skills/commands redondants — les chevauchements d'AGENTS sont **invisibles à R3** (R3 ne couvre que les skills) | `merge` / `delete` |
| `dead-reference` | Référence vers un agent/skill/command supprimé ou passé hidden | `fix` |
| `unused-unreferenced` | Skill/command jamais utilisé sur N runs ET référencé nulle part (R1 confirme) ; à l'inverse, référencé mais jamais chargé = `mismatch` | `delete` |
| `mismatch` | Description mal calibrée vs usage réel, frontmatter invalide, `overlaps_with` non résolu | `recalibrate` / `fix` |
| `token-risk` | Agent aux sessions coûteuses + skill dédié jamais chargé → **cible de forcing** (croiser findings étape 3) | `reference` |

## Sortie

`<output_dir>/runs/current/weekly-coherence-findings-<date>.json` (répertoire du run actif —
`runs/current` est l'alias stable) — format findings (category, tag, severity,
description, evidence_summary, recommendation, recommendation_type,
impact_order_of_magnitude), ranked.

## Règle d'or

**Aucune écriture auto** dans `.opencode/` — pas de suppression, pas de fusion, pas
d'édition. Le pipeline détecte et recommande ; l'humain décide.