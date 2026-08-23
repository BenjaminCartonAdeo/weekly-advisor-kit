# Proposition — Kit multi-harnais : placement mono-cible, portabilité et gate harness-eval

> **Statut : DRAFT** — décisions actées (brief §2, fait foi). Document autonome :
> état, décisions, architecture livrée, limites. Les sections obsolètes de
> l'ancienne proposition (multi-cibles par artefact, projection par symlinks,
> lint maison) sont **abandonnées** et ne reviennent pas.
>
> Portée : documentation uniquement. Aucun code livré avec ce draft — le moteur
> reste sur sa baseline (368 tests PASS).

---

## 1. État

Le kit lisait jusqu'ici la seule télémétrie OpenCode (SQLite locale) et lintait
`.opencode/` via une projection allowlistée copiée dans un répertoire temporaire.
Deux besoins montent :

1. **Multi-harnais** — la télémétrie (et les drafts) concernent aussi Claude Code,
   Copilot (VS Code / CLI) et Codex. Les sessions de ces harnais vivent dans des
   formats hétérogènes (JSONL `~/.claude/projects`, `workspaceStorage` VS Code, …).
2. **Portabilité** — les artefacts générés par le drafting doivent pouvoir être
   consommés par plusieurs harnais sans modifications manuelles, sans chemins
   absolus, sans dépendance au nom technique des commandes du kit.

Ce document acte le modèle final : **placement mono-cible par projet**, **zéro
symlink**, **contenu universel** des artefacts, **qualité via harness-eval natif**
(et non un lint maison), **Copilot CLI ciblant SKILL.md**. L'architecture livrée
par le moteur reflète ces décisions (providers multi-sources, ids canoniques,
baseline findings, matrice de remédiation 5.5, coûts estimés par harnais).

---

## 2. Décisions actées

### 2.1 Placement mono-cible par projet

**UN harnais cible par projet.** Résolution dans l'ordre : override config >
détection par marqueurs > défaut `opencode`. La liste vide `[]` restaure le mode
legacy (toutes les cibles connues, comportement historique).

| Marqueur (répertoire au `project_root`) | Harnais détecté | Cibles de projection des drafts |
|---|---|---|
| `.claude/` | `claude-code` | `.claude/skills` |
| `.opencode/` | `opencode` | `.opencode/skills` |
| `.github/prompts/` **ou** `.github/skills/` | `copilot-vscode` | `.github/prompts`, `.github/skills` |
| `.agents/` | `codex` | `.agents` |

**Priorité en cas de marqueurs multiples** (le premier gagne) :
`claude-code` > `opencode` > `copilot-vscode` > `codex`.

**Aucun marqueur trouvé** → repli `opencode` avec un warning explicite affiché par
le doctor (« aucun marqueur de harnais trouvé — défaut opencode appliqué pour la
projection des drafts »).

**Override config optionnel** : la clé `draft_targets` accepte une liste de harnais
(→ override), la valeur `[]` (→ legacy) ; absente ou invalide → détection
automatique.

### 2.2 Zéro symlink

**Rejet utilisateur acté** : aucun mécanisme du multi-harnais n'utilise de
symlinks. La projection de l'étape 5, l'injection du contenu engine et le scan de
la gate de portabilité **copient réellement les fichiers** (les symlinks de
dossier ne sont pas suivis, les fichiers symlinkés sont ignorés). Un artefact
généré est un fichier réel autonome, portable par copie.

### 2.3 Contenu universel des artefacts générés — 5 règles

Tout artefact généré (skill/command draft) respecte 5 règles de portabilité,
applicables quel que soit le harnais cible :

1. **Frontmatter minimal** — `name` + `description` seuls ; `metadata`,
   `license`, `compatibility` tolérés. Tout autre champ frontmatter de premier
   niveau est interdit (le surplus va sous `metadata:`).
2. **Outils par nom conceptuel, jamais d'identifiant en dur** — les identifiants
   techniques `weekly_*` sont **interdits** dans les artefacts générés : les
   commandes du kit sont référencées par leur nom conceptuel.
3. **Section « Comment invoquer » multi-plateforme obligatoire** — l'invocation
   est décrite pour plusieurs harnais, jamais pour un seul.
4. **Chemins relatifs projet** — aucun chemin absolu (`/home/`, `/Users/`,
   `/root/`, `~/`, `C:\...`).
5. **Scripts auto-contenus** — tout script référencé est inclus/copié avec
   l'artefact ; interdits les chemins hors projet (`../`, `~/`, `/usr|/opt|/etc|/var`)
   et les scripts distants `http(s)://`.

### 2.4 Qualité via harness-eval natif — pas de lint maison

La portabilité est vérifiée par **harness-eval natif** (≥ 7.9.0 ; 7.10.1 installé),
jamais par un script de lint maison.

- **Règles kit-shipped** : `.harness-eval/rules/portability.yaml` (5 règles,
  format YAML natif harness-eval, ids `custom/portability/*`). Sous tout preset
  non vide, seules les règles dont l'id commence par `custom/` restent actives
  sans config explicite — **le préfixe `custom/` est obligatoire**.
- **Gate dans `weekly_commit_draft`** (avant tout commit de draft) :
  - `cwd` = **racine projet obligatoire** (chargement des règles custom depuis
    `<racine>/.harness-eval/rules/` ; lancé ailleurs : 0 règle → gate aveugle) ;
  - lecture du rapport JSON `findings[].details[]`, filtrage `custom/portability/*` ;
  - **≥ 1 finding `error` → commit REFUSÉ, fix manuel requis** ;
  - warnings seuls → commit autorisé, note jointe au résultat ;
  - **binaire absent / sortie illisible → note ⚠ « gate ignorée » (fail-soft)** —
    le doctor du kit signale déjà l'absence du binaire.
- **Couverture 7.10.1 : skills-only** — les règles YAML ne s'appliquent en
  pratique qu'aux skills (les surfaces commands/agents ne sont pas inspectées ;
  limite documentée §4).

### 2.5 Copilot CLI — pas de prompt files → cibler SKILL.md

Copilot CLI ne consomme pas de prompt files : l'artefact cible est **SKILL.md**.
Les cibles de projection `copilot-vscode` couvrent `.github/prompts` **et**
`.github/skills` (SKILL.md y est lu par Copilot CLI).

---

## 3. Architecture livrée

### 3.1 Providers de sessions multi-sources

- **Protocol `SessionProvider`** (`providers/base.py`) : vue lecture-seule uniforme
  sur les sessions d'un harnais — `list_sessions`, `session_steps`,
  `session_tools`, `session_aggregates`, `session_parts`, `find_session_by_title`,
  `check_schema`, `close`. Toutes les méthodes prennent/retournent des ids
  canoniques ; un id brut est toléré en entrée.
- **Registry auto-discovery** (`providers/registry.py`) : scan `pkgutil` de
  `providers/implementations/` ; chaque module expose `PROVIDER_TYPE` et une
  factory `build_provider(source_cfg, cfg)`. Fail-soft : un provider indisponible
  ne bloque pas les autres.
- **Implémentations** :
  - `OpenCodeProvider` — base SQLite locale, adaptateur unifié V1/V2 (existant
    réutilisé tel quel) ;
  - `ClaudeCodeProvider` — transcripts JSONL de `~/.claude/projects`
    (un fichier = une session) ; pas de coûts enregistrés → estimation (§3.7) ;
  - `CopilotVSCodeProvider` — sessions Copilot Chat VS Code (`workspaceStorage`,
    `state.vscdb` optionnel) ; sessions vides fréquentes → métadonnées seules.

### 3.2 Identifiants canoniques `<harness>:<id>`

Chaque session exposée porte un id canonique namespacé `"<harness>:<session_id>"`
(+ tag `harness`). `show-session` accepte **le canonique et le brut** :
`split_canonical_session_id` décompose, un id sans préfixe reste toléré
(rétrocompatibilité).

### 3.3 Doctor multi-providers

Le doctor énumère les sources actives de `session_sources`, vérifie leur schéma
(migrations pour les providers SQLite) et affiche la cible de drafting résolue
(`cibles de drafting: <harnais> (<mode>)`) plus la surface de remédiation 5.5.
**Aucune source disponible → exit 2** (« aucune source de sessions disponible —
vérifier session_sources / bases locales »). `harness-eval` absent du PATH →
warning (étape 5 dégradée), pas un blocage du doctor.

### 3.4 Projection étape 5 étendue au harnais détecté

L'étape `weekly_harness` étend la projection au-delà de `.opencode/` :

- le harnais résolu (`resolve_draft_targets`) détermine des racines
  supplémentaires (`harness_extra_roots`) et des répertoires d'injection
  (`DRAFT_HARNESS_TARGETS`) ;
- le contenu engine est injecté dans la projection (copies réelles, **zéro
  symlink**) ; les chemins créés sans existence réelle dans le projet sont
  tracés comme **orphelins** (`draft_targets.orphan_files`) ;
- le digest enrichi porte `draft_targets` (mode, harnais, warning, racines
  supplémentaires, fichiers orphelins, surface 5.5).

### 3.5 Baseline findings `weekly-harness-baseline.json`

Artefact ancré à la **racine de `output_dir`** (survit aux runs) :

- premier run → snapshot courant = baseline (`status: created`) ;
- runs suivants → baseline relue telle quelle (`status: reused`), **findings
  nouveaux** depuis la baseline listés.

### 3.6 Matrice de remédiation 5.5 — projection / portability / combined

Décision déterministe sur la surface où la remédiation peut s'appliquer :

| Harnais résolus | Surface 5.5 | Raison |
|---|---|---|
| cible unique `opencode` | `projection` | surface native `.opencode` déjà couverte |
| cible unique connue hors `.opencode` | `portability` | remédiation conditionnée au mapping `portability.yaml` |
| plusieurs cibles **avec** `opencode` | `combined` | projection `.opencode` + mapping portability pour les autres |
| plusieurs cibles **sans** `opencode` | `portability` | mapping requis |
| entrée vide / harnais inconnu | `projection` (repli sûr) | raison explicite |

### 3.7 Coûts estimés par harnais (cost_estimates)

Quand un harnais n'enregistre pas de prix (`cost=None`), un coût **estimé** est
calculé : `total_tokens × taux du harnais` (USD par million de tokens, ordres de
grandeur blended des grilles publiques, jamais des montants facturés).

| Harnais | Taux par défaut (USD/Mtok) |
|---|---|
| défaut global | 5.0 |
| `opencode` | 9.0 |
| `copilot-vscode` | 2.5 |
| autres harnais | défaut global |

**Surcharge par source** : clé extra `cost_rate_usd_per_mtok` dans l'entrée
correspondante de `session_sources` (illisible → défaut conservé).

### 3.8 Configuration (clés nouvelles)

| Clé | Rôle | Défaut |
|---|---|---|
| `session_sources` | Sources de sessions actives (liste d'objets `{type, ...}`) | `[{"type": "opencode"}]` |
| `draft_targets` | Cible de drafting : liste de harnais (override), `[]` (legacy), absent/invalide (détection auto) | détection auto |
| `cost_rate_usd_per_mtok` (clé extra d'une source) | Surcharge du taux d'estimation | taux par harnais (§3.7) |

---

## 4. Limites connues

- **Couverture gate skills-only (harness-eval 7.10.1)** : les règles YAML custom
  ne s'appliquent en pratique qu'aux skills (YamlRule early-return quand
  `skill.raw_content` est vide, or le contexte n'a pas de skill parsé pour
  command/agent/hooks). Les surfaces `.github/prompts/**`, `.claude/commands/**`,
  `*/agents/**` ne sont **pas** couvertes par les règles custom en 7.10.1 —
  limite amont, documentée.
- **Moteur à patterns positifs** : pas de détection d'absence globale. L'absence
  TOTALE de section « Comment invoquer » n'est pas détectable ; la règle 3
  détecte les violations visibles (titre mono-harnais, titre générique sans
  annonce multi-plateforme).
- **Règle 1 heuristique** : le frontmatter est filtré par regex ligne + stoplist
  de mots de prose courants — limite heuristique assumée (évite les faux positifs,
  ne rattrape pas tout).
- **Coûts Claude Code** : non enregistrés dans les JSONL → toujours estimés
  (jamais facturés).
- **Copilot VS Code** : sessions vides fréquentes (`requests: []`) → métadonnées
  seules, transcripts vides propres.
- **Gate fail-soft** : binaire `harness-eval` absent → la gate de portabilité est
  ignorée avec note ⚠ (qualité conditionnelle ; le doctor signale l'absence).
- **Version plancher 7.9.0, pas un pin** : versions supérieures acceptées ; la
  compatibilité du format est garantie par la validation de structure du digest
  au chargement.

---

## 5. Hors périmètre

- **Aucun accès à des fichiers auth/OAuth** : tokens Copilot, `apps.json` et
  équivalents ne sont jamais lus ni listés (ni documentés ici).
- **JetBrains/Nitrite** (`~/.config/github-copilot/**`) : hors périmètre des
  providers.
- **Ce draft ne livre pas de code** : le moteur reste sur sa baseline
  (368 tests PASS) ; les sections obsolètes de l'ancienne proposition
  (multi-cibles, symlinks, lint maison) sont abandonnées.

---

*Document autonome — décisions actées brief §2. Pour l'installation : [`README.md`](README.md) / [`INSTALL.md`](INSTALL.md).*