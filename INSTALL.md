# INSTALL.md — Installation & validation du kit

Guide pas à pas pour installer, configurer et valider `weekly-advisor-kit` sur un poste.
Temps total : ~10 min (hors run complet de test : 30-45 min).

> **Installation pilotée par agent ?** Utilisez [`INSTALL_PROMPT.md`](INSTALL_PROMPT.md) :
> collez son contenu dans une session OpenCode (substituez `<TARGET>`), ou demandez à un
> agent de lire `https://raw.githubusercontent.com/BenjaminCartonAdeo/weekly-advisor-kit/master/INSTALL_PROMPT.md`
> et de l'exécuter. Ce guide reste la référence humaine (explications, dépannage, cron).

> Vue d'ensemble et architecture : [`README.md`](README.md) · Contrat complet : `doc/spec-opencode-weekly-advisor`

## 1. Prérequis (binaires du poste)

| Binaire | Minimum | Installation | Vérification |
|---|---|---|---|
| `opencode` | ≥ 1.18 | `curl -fsSL https://opencode.ai/install \| bash` | `opencode --version` |
| `uv` | — | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `uv --version` |
| `harness-eval` | ≥ 7.9.0 | `uv tool install harness-eval` — paquet **PyPI**, pas npm | `harness-eval --version` (versions supérieures acceptées ; le doctor alerte sous le minimum ; absent → étape 5 fatale, gate de portabilité du drafting ignorée avec note ⚠) |
| `git`, `python` | ≥ 3.11 | gestionnaire du poste | `git --version`, `python3 --version` |
| `bun` | optionnel | framework officiel | **non requis au runtime** — utile seulement pour éditer le plugin TS (typages `@opencode-ai/plugin`) ; opencode fournit le module au chargement |

**Auth modèle** (une fois par poste) : `opencode auth login` — le modèle est passé par
`--model` (cron, quickstart), jamais en dur dans l'agent.

## 2. Étapes

### 2.1 Clone

```sh
git clone https://github.com/BenjaminCartonAdeo/weekly-advisor-kit.git && cd weekly-advisor-kit
```

### 2.2 venv + moteur

```sh
uv sync --project .opencode/plugins/weekly-advisor-engine --extra dev
```

Le venv vit dans le projet moteur (`.opencode/plugins/weekly-advisor-engine/.venv`) —
le plugin enveloppe le résout seul, aucune variable d'environnement. L'extra `dev`
est **OBLIGATOIRE** (`pytest`, `ruff`) : sans lui ces outils sont désinstallés et
les tests/lint ne peuvent pas tourner.

### 2.3 Configuration — adaptation OBLIGATOIRE avant tout run

Fichier : `.opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json`

| Clé | Rôle | Valeur commitée |
|---|---|---|
| `project_root` | **Chemin ABSOLU du repo audité** — celui qui contient `.opencode/` | placeholder `/path/to/weekly-advisor-kit` ✋ |
| `output_dir` | Chemin ABSOLU des artefacts (rapports JSON/MD ; chaque run écrit dans `runs/<date>-<uuid8>/`, alias `runs/current`) | placeholder `/path/to/weekly-advisor-kit/reports` ✋ |
| `html_report_dir` | (optionnel) Dossier du **rapport HTML autonome** publié à chaque assemble — défaut `<project_root>/reports/html` (`weekly-report-latest.html` + copie datée) ; `""` désactive la génération (v6.1) | absent → défaut activé |
| `open_browser` | Ouverture automatique du rapport HTML dans le navigateur après l'assemble — mettre `false`, ou poser la variable d'environnement `WEEKLY_NO_BROWSER=1` pour un cron headless | `true` |
| `kit_root` | (optionnel) Worktree du kit pour la synchro best-effort des drafts auto-rédigés (`commit-draft`, v6.0.l) | absent → désactivé |
| `harness_include` | Profil et globs allowlistés pour l'étape `harness` (projection étendue au harnais détecté) | `advisory` (policy + documentation) |
| `session_sources` | Sources de sessions actives (liste d'objets `{type, ...}` : `opencode`, `claude-code`, `copilot-vscode`) ; clé extra `cost_rate_usd_per_mtok` = surcharge du taux d'estimation | `[{"type": "opencode"}]` |
| `draft_targets` | Cible de drafting mono-cible : liste de harnais (override), `[]` (legacy toutes cibles), absent/invalide (détection auto par marqueurs) | détection auto |
| `harness_auto_fix_rules` | Règles explicitement autorisées pour l'application automatique | `[]` (aucune) |
| `harness_auto_fix_max_files` | Nombre maximum de fichiers modifiés par remédiation | `1` |
| `git_name` / `git_email` | Identité des commits auto-rédigés (drafting) | `Weekly Advisor` / `weekly-advisor@localhost` (déjà neutres) |

Les deux placeholders sont **volontaires** : tant qu'ils ne sont pas adaptés, `weekly_doctor`
échoue proprement (exit 2) au lieu de lancer un run sur un mauvais chemin.

```sh
sed -i 's|/path/to/weekly-advisor-kit|/home/<TOI>/Dev/weekly-advisor-kit|g' \
    .opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json
# ou éditez à la main : nano .opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json
```

> **Installation manuelle** : substituez les **DEUX** placeholders (`project_root` **et**
> `output_dir`). L'installation agent-driven les adapte automatiquement — pas la manuelle.
> Un placeholder oublié est détecté par `weekly_doctor` avec un PROBLEM dédié :
> « config jamais adaptée à cette installation — substituer project_root/output_dir
> dans weekly-telemetry-config.json (placeholders /path/to/ détectés) ».

Toutes les autres clés ont des défauts raisonnables (fenêtre 7 j, budgets, seuils,
veille `watch`). `~` est supporté des deux côtés — le moteur Python (`expanduser()`)
et le plugin TS (`os.homedir()`) l'expandent avec la même sémantique ; un chemin
absolu reste recommandé. Personnalisez `watch` (sources de veille) et les budgets selon votre usage.

#### Veille : `watch_distill` (étape 2.2) et sources radar

La clé `watch_distill` pilote la **distillation déterministe** de l'écosystème
(étape 2.2) — valeurs commitées par défaut :

| Sous-clé | Rôle | Défaut |
|---|---|---|
| `enabled` | `false` → étape sautée (exit 2, le flux aval retombe sur l'écosystème complet) | `true` |
| `top_n` | Nombre max de fiches candidates écrites | `30` |
| `quotas` | Répartition `{new, improvable, resurfaced}` des fiches | `{12, 8, 5}` |
| `weights` | Poids du scoring (`authority, relevance, freshness, multi_source, traction`) | `{25, 30, 20, 15, 10}` |
| `memory_file` | Mémoire inter-run (JSONL), chemin relatif à `output_dir` | `watch-memory.jsonl` |
| `retention_weeks` | Purge mémoire au-delà de N semaines | `26` |
| `min_candidates` | Seuil sous lequel la skill 3.5 déclenche le filet B (phase 0) | `20` |

Les fiches bloquées sécurité ne sont jamais soumises au LLM : elles vivent dans
l'**annexe sécurité** du findings final (écrite par `watch-validate`, étape 3.6).

Une entrée `"type": "radar"` dans `watch` interroge un serveur MCP déclaré dans
le fichier `opencode.json` **à la racine du projet** (clé `mcp.<name>.url`) —
le kit est livré avec `agents-radar` déjà déclaré ; sans cette déclaration, la
source se dégrade en warning avec repli RSS (`rss_fallback`).
Le profil `strict` limite le lint aux surfaces de politique et aux entrypoints de
plugins ; `advisory` ajoute `.opencode/skills/**/SKILL.md`, leurs références et leurs
exemples. Les chemins sont toujours relatifs au projet et la projection est temporaire.
Les exclusions générées/vendor (`node_modules`, moteur, venv, caches, bytecode,
`.git`, `dist/build/coverage`) restent obligatoires ; `exclude_patterns` ne peut qu'en
ajouter.
Un fichier `.opencode/` qui ne correspond à aucun profil apparaît comme warning dans le
digest (`harness_include.unscoped_files`) au lieu d'être scanné silencieusement.

#### Drafting mono-cible & gate de portabilité

**Placement mono-cible** : chaque projet est rattaché à **un** harnais de drafting,
résolu par détection de marqueurs au `project_root` (priorité `claude-code` >
`opencode` > `copilot-vscode` > `codex`), surchargeable par `draft_targets` (`[]` =
mode legacy). Aucun marqueur → défaut `opencode` + warning du doctor (exit 1).

| Marqueur projet | Harnais | Cibles de projection des drafts |
|---|---|---|
| `.claude/` | claude-code | `.claude/skills` |
| `.opencode/` | opencode | `.opencode/skills` |
| `.github/prompts/` ou `.github/skills/` | copilot-vscode | `.github/prompts`, `.github/skills` |
| `.agents/` | codex | `.agents` |

**Zéro symlink** : la projection étape 5 et les artefacts générés sont de vraies
copies, jamais de liens.

**Gate de portabilité** (`weekly_commit_draft`) : avant chaque commit de draft,
`harness-eval skill-verify` est lancé avec `cwd` = **racine projet** (chargement des
règles custom `.harness-eval/rules/portability.yaml`, ids `custom/portability/*`).
- ≥ 1 finding `error` → **commit refusé, fix manuel requis** ;
- warnings seuls → commit autorisé, note jointe ;
- binaire `harness-eval` absent → **note ⚠ gate ignorée** (fail-soft — gap d'install, signalé par le doctor) ;
- timeout, crash du scanner ou sortie illisible → **commit refusé** (« gate non exécutable ») — jamais de faux vert.

**Comportement par artefact (honnête)** : `harness-eval skill-verify` n'inspecte que
les dossiers **skills** (`SKILL.md` + fichiers frères). Pour une **command**, la gate
est **skippée explicitement** — non applicable en harness-eval 7.10.1 — et le résultat
du tool affiche une note de skip visible (« Gate portabilité non applicable aux
commands… ») : le commit part sans gate, jamais de silence ni de faux vert.

### 2.4 Permissions user + auth (une fois par poste)

```sh
mkdir -p ~/.config/opencode
# ~/.config/opencode/opencode.json :
# { "permission": { "*": "allow" } }
opencode auth login
```

`"*": "allow"` est requis en **non-interactif** : une permission `ask` est auto-rejetée en
run cron (mesuré v5.32).

### 2.5 Validation — tests + doctor

```sh
cd .opencode/plugins/weekly-advisor-engine
uv run python -m pytest -q     # 517 tests — tout doit passer
cd ../../..
opencode run --agent weekly-advisor --model <votre-modèle> \
    --dir . "Exécute weekly_doctor et donne son verdict"
```

Attendu : **« Kit OK ... Prêt pour un run »** (exit 0). Sinon, les causes sont listées
(exit 1 = avertissements, exit 2 = blocant — voir §6 Dépannage).

`--model` est explicite : en non-interactif, sans modèle résolu par la config, le run
échoue.

### 2.6 Run complet de test (recommandé)

```sh
opencode run --port 4097 --agent weekly-advisor --model <votre-modèle> \
    --dir . "Lance la revue hebdomadaire"
```

- Durée : 30-45 min (ordre figé en 17 étapes ; `weekly_harness` ≈ 100 s à elle seule)
- Artefact attendu : le **rapport HTML** `<project_root>/reports/html/weekly-report-<date>.html`
  (+ `weekly-report-latest.html`) — il s'ouvre automatiquement dans votre navigateur ;
  l'archive `<output_dir>/runs/current/weekly-report-<date>.md` (alias stable `runs/current/`)
  reste **le signal** du run. Cron headless ? Exportez `WEEKLY_NO_BROWSER=1`
- Si un run a déjà eu lieu aujourd'hui : les données du jour sont exclues des totaux et le
  drafting aura 0 candidat (comportement normal) — pour un run pleinement informatif,
  lancer le premier run un lundi avec une semaine d'historique

### 2.7 Cron (production)

```cron
SHELL=/bin/bash
PATH=/home/<TOI>/.local/bin:/usr/local/bin:/usr/bin:/bin
0 6 * * 1 opencode run --port 4096 --agent weekly-advisor \
    --model <votre-modèle> \
    --dir /home/<TOI>/Dev/weekly-advisor-kit "Lance la revue hebdomadaire" \
    >> /var/log/weekly-advisor.log 2>&1
```

- **Rien de plus sur PATH** : la pipeline résout elle-même python (`<moteur>/.venv/bin/python`)
  et `harness-eval` via le plugin ; seul `git` doit rester résolu (défaut)
- **Signal** : rapport présent = run terminé ; **rapport absent** = échec → alerter ;
  le log contient la cause
- **Cron headless** : exporter `WEEKLY_NO_BROWSER=1` dans la ligne crontab (ou `open_browser: false`
  en config) — sinon l'agent tente d'ouvrir un navigateur à chaque run (tentative sans
  conséquence sur un serveur sans affichage, mais un warning est loggé)
- **Heartbeat externe recommandé** (ex. healthchecks.io) pingé en début de run : seul
  maillon qui alerte si le cron lui-même ne tourne pas (spéc §0.5)

### 2.8 Windows

Support natif : le plugin résout l'interpréteur Python dans cet ordre — `WEEKLY_PYTHON`,
puis `<moteur>/.venv/Scripts/python.exe` (layout venv Windows), puis `.venv/bin/python`
(POSIX/WSL). Le venv créé par `uv sync` vit donc en `.venv\Scripts\` sans action de votre
côté, et les flux stdout/stderr sont forcés en UTF-8.

- **Secours** : si le venv vit ailleurs, définir `WEEKLY_PYTHON=C:\chemin\vers\python.exe`
- **Planification** (équivalent du cron §2.7, une ligne) :

```powershell
schtasks /Create /SC WEEKLY /D LUN /ST 06:00 /TN weekly-advisor /TR "opencode run --port 4096 --agent weekly-advisor --model <votre-modele> --dir C:\Dev\weekly-advisor-kit \"Lance la revue hebdomadaire\""
```

- **WSL** : déjà couvert par la doc Linux (§2) — sous WSL, ignorez cette section et suivez
  les étapes POSIX (le venv sera en `.venv/bin/python`, détecté automatiquement).

## 3. Mise à jour

```sh
git pull
uv sync --project .opencode/plugins/weekly-advisor-engine --extra dev
cd .opencode/plugins/weekly-advisor-engine && uv run python -m pytest -q
cd ../../..
opencode run --agent weekly-advisor --model <votre-modèle> \
    --dir . "Exécute weekly_doctor et donne son verdict"
```

`doctor` est à relancer après chaque mise à jour (contrat spec §4 — capacité « Diagnostic » et §5.1).

## 4. Désinstallation

```sh
crontab -e   # retirer la ligne weekly-advisor
rm -rf <clone>   # supprime venv, moteur, artefacts locaux
rm ~/.config/opencode/opencode.json   # si vous ne voulez pas garder la permission allow
```

Rien d'autre à nettoyer : aucun service, aucun fichier hors du repo et de `output_dir`.

## 5. Pièges connus

- **Tests** : uniquement depuis le dossier moteur — depuis la racine, l'import `tests`
  échoue (sensibilité au cwd, documentée)
- **Assemble consomme le draft** : un `weekly_report_assemble` réussi supprime
  `weekly-report-draft` — pour un nouvel assemble, relancer `weekly_report_prep` d'abord
  (sinon erreur RC=2 « draft inexistant »)
- **Passe 6.5** : les `weekly-coherence-findings-<date>.json` existants du jour sont
  conservés (archive) — la fraîcheur est garantie au premier run du jour
- **Re-run du même jour** : écrase le summary ; si la fenêtre diffère (> 1 h), la baseline
  insights du run suivant est perdue — le CLI avertit (garde v5.31.c)
- **Auto-exclusion** : le run s'exclut lui-même des totaux (session active + filtre de
  titre `advisor_run_title`) — pas de pollution, mais un 2ᵉ run le même jour est vide

## 6. Dépannage

| Symptôme | Cause | Solution |
|---|---|---|
| doctor : `PROBLEM: project_root ... ne contient pas .opencode/` (exit 2) | config non adaptée (placeholder) | adapter `project_root` (§2.3) |
| doctor : `PROBLEM: output_dir non accessible ... '/path'` (exit 2) | `output_dir` placeholder | adapter `output_dir` (§2.3) |
| `No module named pytest` | install sans l'extra `dev` | `uv sync --project .opencode/plugins/weekly-advisor-engine --extra dev` |
| tool : `venv introuvable: ...` | venv absent | §2.2 |
| tool : `moteur introuvable: ... (structure du kit corrompue)` | `.opencode/` incomplet | re-cloner proprement |
| doctor : `opencode introuvable` | PATH du shell d'appel (cron minimal) | prérequis §1 + PATH du cron |
| doctor : base non détectée / « version d'OpenCode non supportée » | opencode < 1.18, DB jamais initialisée, ou schéma inconnu | mettre à jour opencode ; lancer `opencode` une fois ; `opencode auth login` pour initialiser la base |
| `exit 2` sur un tool | étape bloquée (base verrouillée, schéma, réseau...) | lire le message (dernières lignes remontées par le plugin) ; lancer `weekly_doctor` |
| run complet sans rapport final | exit 2 d'une étape → arrêt sans rapport (par conception) | log du cron : cause exacte |
| modèle non résolu en non-interactif | aucun `--model` ni config modèle | passer `--model` explicitement (quickstart/cron) |
| warning doctor « config introuvable au cwd ni au project_root » | CLI lancé **sans** `--config` hors layout kit | normal en CLI autonome ; via l'agent (qui passe `--config`), ce warning n'apparaît pas (v6.0.a) |

## 7. Développement

```sh
# tests (depuis le dossier moteur)
cd .opencode/plugins/weekly-advisor-engine
uv run python -m pytest -q

# lint python
uvx ruff check weekly_telemetry_aggregator tests

# édition du plugin TS (optionnel) : typages @opencode-ai/plugin
cd .opencode && bun install   # puis éditer plugins/weekly-advisor.ts
```

Ajouter un tool = ajouter une sous-commande CLI (`cli.py`) **puis** l'exposer dans le
plugin : le CLI reste testable sans opencode, le plugin est une enveloppe sans logique.
