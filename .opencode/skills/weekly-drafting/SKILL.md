---
name: weekly-drafting
description: Auto-drafting des skills et commands (étape 4 du weekly-advisor) — généralisation depuis les transcripts, cible harnais unique résolue par le kit, contenu universel multi-plateforme, gate de portabilité avant chaque commit.
---

# Weekly Drafting — étape 4

Généralise les patterns coûteux des sessions en **skills/commands réutilisables** et
portables, avec commit direct traçable. Jamais de correction du code applicatif :
écriture dans les répertoires de harnais du projet uniquement.

## Flux

1. Candidats : la commande de candidats du kit produit un JSON plafonné dans
   `<output_dir>/runs/current/` (skill-candidate / command-candidate / command-improvement,
   sévérité DESC).
2. Pour chaque candidat : lire l'extrait de transcript du run
   (`transcript-extract-<session_id>.md`, fallback transcription à la demande),
   **généraliser (jamais verbatim)**, rédiger l'artefact dans la cible résolue
   (voir « Cible unique »).
3. Vérifier le chevauchement contre les artefacts existants du projet → si `overlaps_with`
   non vide **OU** collision de nom ⇒ **PATCH le skill existant (umbrella consolidation)** :
   **ÉDITE** le skill existant (pose `target_skill_id`, `action=patch`), fusionne le contenu
   nouveau dans l'existant ; **ne crée PAS de doublon**. Si aucun chevauchement ⇒ création
   normale via commit-draft.
4. Commit via la sous-commande commit-draft du plugin (`--kind skill|command --file <absolu>`) :
   la **gate de portabilité** s'exécute d'abord (voir plus bas), puis validation frontmatter,
   pré-checks git, add scopé, message construit depuis le frontmatter ; **1 commit par écriture** ;
   échec → exit 1, fichier conservé, signaler au rapport.

## Cible unique — résolution du harnais

Le drafting écrit dans LE harnais cible du projet (décision mono-cible), jamais au jugé :

| Harnais | Répertoires cibles (relatifs à la racine projet) |
|---|---|
| claude-code | .claude/skills |
| opencode | .opencode/skills |
| copilot-vscode | .github/prompts · .github/skills |
| codex | .agents |

- Résolution approuvée : `resolve_draft_targets(project_root, config)` renvoie le mode,
  les harnais cibles et un warning éventuel. Priorité : **override config > détection par
  marqueurs > défaut opencode**.
- Détection par marqueurs (.claude/, .opencode/, .github/prompts/, .agents/) ; plusieurs
  marqueurs → priorité claude > opencode > copilot > codex ; aucun marqueur → défaut
  opencode avec warning explicite, à signaler au rapport.
- Override : liste imposée par la configuration du kit.
- Mode legacy (liste vide en configuration) : toutes les cibles du tableau ci-dessus,
  ordre de priorité conservé (comportement historique).
- Zéro symlink : projection réelle des fichiers dans la cible, jamais de lien.
- Le digest du run expose la décision (`draft_targets.surface_decision`) : s'y fier plutôt
  que re-détecter manuellement.

## Contenu universel des artefacts générés — règles impératives

Tout artefact généré doit rester portable d'un harnais et d'une machine à l'autre.
Règles de génération NON NÉGOCIABLES :

1. **Frontmatter étendu** — `name` et `description` restent les seuls champs requis ;
   tout artefact doit porter le bloc `metadata` complet ci-dessous (schéma canonique,
   à respecter à la lettre) :

   ```yaml
   metadata:
     authored_by: opencode-weekly-advisor
     authored_at: "ISO-8601"
     origin: weekly-background            # user|bundled|weekly-foreground|weekly-background
     write_context: "<court>"             # optionnel
     confidence: medium                   # high|medium|low
     skill_id: "skill_<8 hex>"            # = 'skill_' + sha256(nom.normalize().lower())[:8]
     source_sessions: ["ses_xxx"]
     overlaps_with: []                    # désormais = CIBLE de PATCH (merge), pas blocage
     target_agents: ["<agent>"]
     last_verified_at: null               # ISO|null
     verification: none                   # comment validé, ou 'none' explicite (gate validate_draft / R6)
     usage: { last_loaded: null, load_count: 0 }
     ttl_policy: decay                    # decay|pin|null
   ```

   Mint déterministe du `skill_id` : `skill_` + 8 premiers hex de
   `sha256(nom.strip().lower())`. `origin` par défaut `weekly-background`
   (`weekly-foreground` si issu d'un nudge direct de l'utilisateur). `confidence`
   par défaut `medium`. Le champ `metadata.verification` DOIT être renseigné
   (comment le skill a été validé, ou `none` explicite) — il est vérifié par la
   gate `validate_draft` (R6).
2. **Outils par nom conceptuel** — aucun identifiant technique propre au kit ou à un
   harnais dans le corps : dire « la commande de collecte hebdomadaire », pas son nom interne.
3. **Section « Comment invoquer » multi-plateforme obligatoire** — tout skill généré décrit
   son invocation pour plusieurs harnais (OpenCode, Claude Code, Codex, Cursor…), jamais pour un seul.
4. **Chemins relatifs au projet** — aucun chemin absolu (home utilisateur, racines système,
   lecteur Windows) ; variable d'environnement documentée si vraiment indispensable.
5. **Scripts auto-contenus** — tout script référencé vit avec l'artefact (ex. `scripts/`)
   et s'appelle en chemin relatif projet ; ni script hors du projet, ni script distant.

6. **Do NOT capture (anti-learning)** — ne JAMAIS créer de skill à partir de :
   - un échec transitoire (erreur réseau, timeout, crash éphémère) ;
   - une prohibition spécifique à l'environnement (chemin/absence propre à une machine) ;
   - un récit one-off (anecdote sans pattern reproductible) ;
   - un secret (password / token / credential / api key) ;
   - une référence PR/ticket (JIRA, GH-, PR #…).
   Ces patterns ne généralisent pas : émettre un constat `environment-change` (report-only)
   ou ignorer — jamais en faire un artefact.

Format skill (agentskills.io) — gabarit conforme :

```yaml
---
name: string            # == nom du dossier (requis)
description: string     # une ligne, déclenche le chargement à la demande (requis)
metadata:
  authored_by: opencode-weekly-advisor
  authored_at: "ISO-8601"
  origin: weekly-background            # user|bundled|weekly-foreground|weekly-background
  write_context: "<court>"             # optionnel
  confidence: medium                   # high|medium|low
  skill_id: "skill_<8 hex>"            # = 'skill_' + sha256(nom.strip().lower())[:8]
  source_sessions: ["ses_xxx"]        # traçabilité > 6 mois
  overlaps_with: []                   # CIBLE de PATCH (merge), pas blocage
  target_agents: ["<agent-cible>"]    # public du skill
  last_verified_at: null              # ISO|null
  verification: none                  # comment validé, ou 'none' (gate validate_draft / R6)
  usage: { last_loaded: null, load_count: 0 }
  ttl_policy: decay                  # decay|pin|null
---
# <Nom>
## Quand utiliser
## Comment invoquer l'artefact généré (multi-plateforme)
## Procédure
## Points d'attention
```

Format command :

```yaml
---
description: string     # une ligne obligatoire — déclenche la suggestion
# agent et model restent optionnels selon le harnais : ne jamais les figer
---
Corps généralisé, $ARGUMENTS pour les paramètres variables
```

## Gate de portabilité au commit

La sous-commande commit-draft exécute `harness-eval skill-verify` sur l'artefact avant
chaque commit (règles custom portability du kit) :

- au moins une erreur ⇒ **commit refusé**, liste des findings (règle, ligne, suggestion),
  statut « fix manuel requis » : corriger le fichier puis relancer — jamais ignorer ;
- warnings seuls ⇒ commit autorisé, note jointe au résultat ;
- binaire harness-eval absent ⇒ gate ignorée (fail-soft, non bloquant) ; le doctor du kit
  signale déjà cette absence.

Limite connue (moteur 7.10.1) : couverture **skills seulement** — commands et agents ne
sont pas inspectés par ces règles ; ne pas compenser par un lint maison.

## Public cible (question obligatoire : QUI utilisera ce skill ?)

- Cible identifiable (typiquement les agents qui reproduisent le pattern observé) :
  1. renseigner metadata.target_agents ;
  2. référencer le skill dans le corps des agents ciblés DU HARNAIS CIBLE (section
     « pour toute tâche X, charger le skill Y », chemins relatifs projet) — un skill
     jamais référencé ni chargé sera proposé au retrait par la remédiation ;
  3. commands : invocation manuelle `/nom`, aucune référence requise.
- Aucune cible identifiable (skill générique) : target_agents vide, chargement piloté par
  la description — le noter.
- command-improvement : **éditer** la commande existante (conserver sa structure ; ajouter
  les garde-fous de coût recommandés : périmètre obligatoire, budget d'itérations, mode
  audit), même passage commit-draft — modification, pas création ; ne jamais réécrire à blanc.

## Périmètre worktree / projet

- Commande ciblée par improvement : résolue DANS LE PROJET d'abord (la copie projet gagne
  toujours) ; absente du projet → hors périmètre : constat environment-change
  (report-only), pas de draft, pas de lecture.
- Toute lecture/écriture hors worktree est impossible (permissions de l'agent) — ne jamais
  tenter ; un échec de permission n'est jamais fatal : constater, signaler au rapport,
  continuer l'ordre figé (exit 1 partiel, pas 2).
- Session dont project_path est hors project_root ⇒ constat environment-change
  (report-only) — rien n'est écrit dans le projet courant.
