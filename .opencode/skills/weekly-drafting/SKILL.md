---
name: weekly-drafting
description: Auto-drafting des skills et commands (étape 4 du weekly-advisor) — généralisation depuis les transcripts, format agentskills.io, public cible, commit traçable via commit-draft.
metadata:
  authored_by: opencode-weekly-advisor
  skill_class: pipeline-step
---

# Weekly Drafting — étape 4

Généralise les patterns coûteux des sessions en **skills/commands réutilisables**, avec
commit direct traçable. Jamais de correction du code applicatif : écriture dans `.opencode/` uniquement.

## Flux

1. `draft-candidates --anchor $RUN_TIME` → `weekly-draft-candidates-<date>.json`
   (candidats skill-candidate / command-candidate / command-improvement, sévérité DESC,
   plafonné par `max_candidates_per_run`)
2. Pour chaque candidat : lire `extracts/transcript-extract-<session_id>.md`
   (fallback `show-session`), **généraliser (jamais verbatim)**, rédiger le fichier
3. Vérifier le chevauchement vs `.opencode/skills/*/SKILL.md` + `.opencode/commands/*.md` →
   **si `overlaps_with` non vide ⇒ NE PAS committer** : constat `environment-change`
   (report-only), pas de création
4. `commit-draft --kind skill|command --file <chemin absolu>` — validation frontmatter,
   pré-checks (HEAD/rebase), add scopé, message construit depuis le frontmatter ;
   **1 commit par écriture** ; échec → exit 1, fichier conservé, signaler au rapport

## Format — Skill (agentskills.io)

```yaml
---
name: string            # == nom du dossier
description: string     # une ligne, déclenche le chargement à la demande
metadata:
  authored_by: opencode-weekly-advisor
  authored_at: "ISO-8601"
  source_sessions: ["ses_xxx"]        # traçabilité > 6 mois
  overlaps_with: []                   # vide OBLIGATOIRE pour committer
  target_agents: ["<agent-cible>"]    # public du skill (voir règle ci-dessous)
---
# <Nom>
## Quand utiliser
## Procédure
## Points d'attention
```

## Format — Command

```yaml
---
description: string     # une ligne obligatoire — déclenche la suggestion
# agent: string         # optionnel
# model: string         # optionnel
---
Corps généralisé, $ARGUMENTS pour les paramètres variables
```

## Règle du public cible (question obligatoire : « QUI utilisera ce skill ? »)

- Si le public cible est identifiable (typiquement les agents qui reproduisent le pattern
  observé) :
  1. renseigner `metadata.target_agents` ;
  2. **référencer le skill dans le corps de chaque agent ciblé** (`.opencode/agents/**/*.md`,
     section « pour toute tâche X, charger le skill Y ») — un skill jamais référencé ni
     chargé sera proposé au retrait par R1 ;
  3. pour les commands : aucune référence nécessaire (invocation manuelle `/nom`), mais
     documenter la cible dans `target_agents` si utile
- Si aucune cible identifiable (skill générique) : `target_agents` vide, chargement piloté
  par la description — le noter
- `command-improvement` : **éditer** la commande existante (ajouter les garde-fous de coût
  recommandés : périmètre obligatoire, budget d'itérations, mode audit) — même
  `commit-draft --kind command --file <chemin>` (modification, pas création) ; ne jamais
  réécrire le corps à blanc : conserver la structure et ajouter la section de garde-fous

## Périmètre projet

Session dont `project_path` est hors `project_root` ⇒ constat `environment-change`
(report-only) — rien n'est écrit dans le `.opencode/` du projet courant.