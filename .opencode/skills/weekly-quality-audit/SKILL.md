---
name: weekly-quality-audit
description: Audit qualitatif des sessions coûteuses (étape 3 du weekly-advisor) — catégories de constats, schéma findings, paraphrase stricte, lien session → commande lanceuse.
metadata:
  authored_by: opencode-weekly-advisor
  skill_class: pipeline-step
---

# Weekly Quality Audit — étape 3

Sélection déterministe en amont (aucun choix de session ici), examen LLM des transcripts.

## Flux

1. La sous-commande `audit-candidates` du plugin weekly-advisor → `weekly-audit-candidates-<date>.json`
   (**utiliser le chemin absolu retourné par le tool ; jamais de Glob depuis la racine
   sur l'arbre `reports/`**)
   (équivalent CLI : `audit-candidates --anchor <ancre>` — l'ancre est gérée par le
   plugin `<output_dir>/anchor-last.txt`, pas de calcul manuel)
   (`audited` / `unaudited`, plafond `audit_max_sessions` — ne PAS auditer au-delà)
2. Pour chaque session `audited` : la sous-commande `show-session` du plugin weekly-advisor
   (`<id>`, `--include-children` si sous-agents) → `transcript-extract-<id>.md`
   (le tool écrit l'extract et retourne le chemin absolu — l'utiliser tel quel ;
   équivalent CLI : `show-session <id> --extract-dir <répertoire extracts du run actif>`)
3. Examiner chaque transcript selon les catégories ci-dessous
4. **Relier chaque session à son lanceur** : si les premiers tours utilisateur commencent par
   `/` (ex. `/swarmx`), identifier la commande lanceuse et l'examiner. Si la session est
   coûteuse (`context-bloat`/`loop` high/medium) ET lancée par une commande existante →
   constat **`command-improvement`** (recommandation : ajouter les garde-fous de coût manquants
   à la commande — périmètre obligatoire, budget d'itérations, mode audit)
   avec `recommendation_type: command-improvement`
5. Écrire `weekly-quality-findings-<date>.json` (schéma ci-dessous)
6. Ne PAS ré-émettre un candidat snoozé (`ignored_findings` de la config)

## Harnais d'origine

Chaque session provient d'un harnais identifiable : ids canoniques
`<harness>:<uuid>` (`claude-code:`, `opencode:`, `copilot-vscode:`).
`show-session` accepte l'id canonique ET l'id brut (le brut est résolu vers la
première source qui le possède) ; les titres Claude Code sont tronqués à
100 caractères à la lecture.

## Pré-filtre anti-learning (déterministe) — R2

Avant d'émettre un `skill-candidate`, vérifier que le pattern N'EST PAS :

- **échec transitoire** : erreur ponctuelle due au modèle/infra, non reproductible (une seule session, pas de récurrence)
- **prohibition env-spécifique** : action interdite par l'environnement (permissions, secrets, réseau) — pas un pattern skillable
- **récit one-off** : demande utilisateur singulière, jamais réutilisable ailleurs
- **secret** : contient une clé/token/secret ou des données à ne pas généraliser
- **référence PR/ticket** : lié à un artefact externe éphémère (PR #, ticket Jira) sans valeur durable

Si un de ces critères matche → **ne PAS émettre `skill-candidate`**. Émettre
`environment-change` (si la cible est hors `project_root` ou non écrasable) ou **drop**
(ignorer le constat). Ce pré-filtre évite d'alimenter l'étape 4 avec du bruit
non-apprenable.

## Boucle usage → raffinement — R7

Les **gaps récurrents d'usage d'un skill existant** (ex. un skill mal ciblé,
mal documenté, ou dont le frontmatter ne déclenche pas le chargement attendu —
cf. catégorie `skill-underuse` poussée plus loin) doivent être émis comme :

```jsonc
"recommendation_type": "skill-improvement"
```

`skill-improvement` ferme la boucle usage→raffinement : il alimente directement
l'étape 4 (drafting) pour corriger le skill en place plutôt que de proposer un
nouveau skill. Inclus dans la catégorie `skill-underuse` quand la cause est
identifiable dans le frontmatter/description du skill.

## Catégories de constats

| Catégorie | Détection |
|---|---|
| `loop` | Boucles de ré-essai, relectures répétées du même fichier, corrections en chaîne |
| `cache-invalidation` | Relectures intégrales causées par des invalidation de cache/édits minimes |
| `context-bloat` | Chargement de fichiers volumineux/innombrables, contexte saturé |
| `manual-injection` | Injection manuelle de contexte redondant (coller un fichier entier au lieu de cibler) |
| `skill-underuse` | Pattern récurrent qui dispose d'un skill existant jamais chargé |
| `command-underuse` | Tâche répétée qui dispose d'une commande jamais invoquée |
| `command-candidate` | Séquence répétée N fois dans la session qui mérite une commande |
| `skill-candidate` | Workflow coûteux reproductible ailleurs qui mérite un skill |
| `skill-improvement` | Skill existant dont l'usage est défaillant (frontmatter/description mal calibré) — boucle raffinement R7 |
| `model-mismatch` | Modèle surdimensionné pour la tâche (coût/min actif anormal) |
| `command-improvement` | Session coûteuse lancée par une commande existante → garde-fous manquants |
| `environment-change` | Constat dont la cible est hors `project_root` ou non écrasable (report-only) |

## Schéma du fichier findings (JSON strict, archive)

```jsonc
{
  "schema_version": 1,
  "period": { "start": "...", "end": "..." },
  "generated_at": "...",
  "findings": [
    {
      "session_id": "ses_xxx",            // null si pas lié à une session
      "category": "context-bloat",
      "severity": "high",                  // high | medium | low
      "description": "une phrase",
      "evidence_summary": "≤ 200 caractères — PARAPHRASE, jamais de citation verbatim"
                                           // (règle copyright : pas de copie du transcript)
      "recommendation": "...",
      "recommendation_type": "prompting-habit | environment-change | skill-candidate | skill-improvement | command-candidate | command-improvement",
      "impact_order_of_magnitude": "small | medium | large",
      "source": "new",                    // new | carried (v6.0.n)
      "carried_from": "2026-08-16-ace20d4b"  // requis si source=carried (run d'origine)
    }
  ]
}
```

## Règles de rédaction

- **Paraphrase obligatoire** : jamais de citation verbatim du transcript, jamais de titre/session/ticket inventé
- Une preuve chiffrée (tokens, coût, répétitions) dans `evidence_summary` si disponible
- Sélection des sessions déjà faite : l'audit n'ajoute pas de sessions hors `audited`
- Constats triés : sévérité DESC puis impact
- **`source` obligatoire** : `new` si le constat émerge de ce run, `carried` si
  c'est la reprise d'un constat déjà émis (décision tranchée) — dans ce cas
  `carried_from` = identifiant du run d'origine pour traçabilité ; le rapport
  affichera « repris de <run> » sur ces constats