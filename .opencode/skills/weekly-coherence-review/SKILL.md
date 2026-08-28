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

- **Inventaire déterministe (source de vérité, étape 2.5)** : `weekly-watch-context-<date>.json`
  du run — `skills`/`commands`/`agents`/`plugins` + `declared_plugins`/`local_plugins` +
  `plugin_config.files|available|valid` et `counts` — **lecture seule, zéro re-scan**
  de `.opencode/` (G2, v6.0.p) ; le frontmatter des items vient du `skill_catalog` du summary
- Usage réel : `summary.skill_usage` / `command_usage` / `skills_never_loaded`
- `insights.maintenance` (R1 retire / R3 merge) et le digest harness (triggers/budget)
- Findings de l'étape 3 (sessions coûteuses → cibles de forcing)

> La passe critique ne refait jamais `ls`/`read` de `.opencode/{agents,skills,commands}`
> pour établir l'état déclaratif : l'inventaire JSON de l'étape 2.5 est produit par la
> même exécution du moteur et fait foi. Un re-scan LLM re-dériverait l'état avec une
> précision moindre et ferait diverger la revue du run qu'elle documente.

## Style

Ponytail-audit : **une ligne par finding**, ranked par impact (le plus gros gain d'abord),
tag action sur chaque finding. `generated_at` : **ancre du run en ISO-8601 UTC** (ex. `2026-08-18T10:39:33Z`, copiée de
`anchor-last.txt`) — jamais minuit, jamais une date seule.

Preuve obligatoire dans `evidence_summary` — sans preuve,
écarté à la revue.

## Catégories

CHAQUE finding doit produire une **DÉCISION explicite** (`tag_action` étendu) avec
`target_skill_id` renseigné. Décisions possibles : `archive | merge | pin | reference`.

| Catégorie | Détection | Tag action (décision) | `target_skill_id` |
|---|---|---|---|
| `duplicate` | Agents/skills/commands redondants — les chevauchements d'AGENTS sont **invisibles à R3** (R3 ne couvre que les skills) | `merge` (sinon `delete` pour le doublon) | skill/command à conserver |
| `dead-reference` | Référence vers un agent/skill/command supprimé ou passé hidden | `fix` | référent à corriger |
| `unused-unreferenced` | Skill/command jamais utilisé sur N runs ET référencé nulle part (R1 confirme) ; à l'inverse, référencé mais jamais chargé = `mismatch` | `delete` (ou `archive` si stale — voir TTL) | item visé |
| `mismatch` | Description mal calibrée vs usage réel, frontmatter invalide, `overlaps_with` non résolu | `recalibrate` / `fix` | skill à recalibrer |
| `token-risk` | Agent aux sessions coûteuses + skill dédié jamais chargé → **cible de forcing** (croiser findings étape 3) | `reference` | skill dédié à forcer |

### Staleness & TTL — R4 curation + R7/R8

La review LIT `metadata.usage.last_loaded` / `load_count` et `metadata.ttl_policy`
(du frontmatter étendu de chaque item, source : `skill_catalog`) :

- **stale** : `metadata.usage.last_loaded` > 90j OU `metadata.usage.load_count == 0`
  sur 3 runs consécutifs → `tag_action: archive` (décision `archive`, `target_skill_id`
  = l'item lui-même). À distinguer de `delete` : `archive` conserve le fichier, `delete`
  le retire.
- **pin** : si `metadata.ttl_policy: pin` → `tag_action: pin` et **JAMAIS** `delete` /
  `archive` (exclus permanent, ex. skills critiques du harness).

### Boucle fermée vers l'étape 4

Ces findings (décisions de curation + les `skill-improvement` remontés par l'étape 3)
alimentent l'étape 4 (drafting) : la revue émet les décisions, le drafting les traite
comme entrées de raffinement/curation. Boucle fermée usage → cohérence → drafting.

## Sortie

`<output_dir>/runs/current/weekly-coherence-findings-<date>.json` (répertoire du run actif —
`runs/current` est l'alias stable) — format findings (category, tag_action, decision,
target_skill_id, severity, description, evidence_summary, recommendation,
recommendation_type, impact_order_of_magnitude), ranked.

## Règle d'or

**Aucune écriture auto** dans `.opencode/` — pas de suppression, pas de fusion, pas
d'édition, pas d'archivage. La review est **read-only** : les décisions
(`archive | merge | pin | reference`) sont **ÉMISES** dans le findings JSON, jamais
appliquées ici. Leur application est déléguée au nouveau module **`weekly-skill-curate`**
(étape 6.6) qui consomme ces décisions de façon déterministe. Le pipeline détecte et
recommande ; `weekly-skill-curate` applique ; l'humain valide.