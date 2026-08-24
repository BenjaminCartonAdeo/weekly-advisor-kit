# weekly-advisor-kit

[![CI](https://github.com/BenjaminCartonAdeo/weekly-advisor-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/BenjaminCartonAdeo/weekly-advisor-kit/actions/workflows/ci.yml)

Revue hebdomadaire automatisée de votre usage d'OpenCode : chaque semaine, le kit
analyse votre télémétrie locale, surveille l'écosystème des plugins/skills, audite
vos sessions coûteuses et produit **un rapport HTML interactif** — `weekly-report-<date>.html`
dans `<projet>/reports/html/` (page autonome : dashboard KPI, filtres, graphiques, dark
mode — ouverte automatiquement dans votre navigateur en fin de run), avec archive MD
`weekly-report-<date>.md` dans le répertoire du run (`reports/runs/<date>-<uuid>/`,
alias stable `reports/runs/current/`).

Chaque lundi, il vous dit : combien vous avez dépensé, ce qui a coûté cher et pourquoi,
quels skills/commands sont inutilisés ou redondants, quoi surveiller dans l'écosystème,
et quelles corrections peuvent être auto-rédigées (proposées, jamais imposées).

Le kit s'articule autour d'**un dossier cœur** `.opencode/` : deux agents dédiés, 6 skills
qualitatifs, 3 commands, un **plugin enveloppe** (16 tools `weekly_*`) et un
**moteur Python** déterministe (`weekly-telemetry-aggregator`) ; à la racine : la spec,
les guides d'installation (humain + agent) et les scripts de validation.

**Spécification** : [`opencode-weekly-advisor`](opencode-weekly-advisor) (le contrat complet, 8 parties)
**Installation pas à pas (humain)** : [`INSTALL.md`](INSTALL.md)
**Installation pilotée par agent** : [`INSTALL_PROMPT.md`](INSTALL_PROMPT.md) — colle le contenu dans une session OpenCode ou demande à un agent de lire la raw URL (`raw.githubusercontent.com/BenjaminCartonAdeo/weekly-advisor-kit/master/INSTALL_PROMPT.md`)

## Ce que fait le kit

Le cœur est **100 % déterministe, zéro LLM** : le moteur Python lit directement la
télémétrie locale des harnais actifs (base SQLite OpenCode, transcripts JSONL
Claude Code, sessions Copilot VS Code — sources configurables via `session_sources` ;
pas de SDK, pas de serveur) et produit des JSON reproductibles. Le LLM n'intervient
que sur les étapes qualitatives, encadrées par des skills dédiés et des contrats
anti-hallucination.

| Étape | Contenu | Exécuté par |
|---|---|---|
| 1 · Télémétrie | coûts, tokens, cache, modèles, sessions top/outliers, usage tools/skills/commands, prompts répétés, subagents | `weekly_run` (déterministe) |
| 2 · Veille écosystème | npm, topics GitHub, registre MCP, releases OpenCode, repos/listes/RSS suivis | `weekly_releases` (déterministe) |
| 3 · Audit qualitatif | sessions candidates (coût, outliers, boucles, cache, prompts répétés) → constats archivés | skill `weekly-quality-audit` |
| 3.5 · Veille critique | marché × environnement × constats → recommandations adopt / improve / ignore | skill `weekly-watch-review` |
| 4 · Auto-drafting | candidats skills/commands → rédaction + commit sécurisé et scoped (gate portabilité `skill-verify` avant commit) | skill `weekly-drafting` + `weekly_commit_draft` |
| 5 · Lint | `harness-eval` sur une projection étendue au harnais détecté (règles `portability.yaml`, baseline findings) | `weekly_harness` (déterministe) |
| 5.5 · Remédiation | findings harness → décisions et corrections bornées, sans commit automatique | skill `harness-remediation` + `weekly_harness_remediate` |
| 6 · Insights | deltas vs semaine précédente, alertes budget/cache/spikes, maintenance R1-R4 | `weekly_insights` (déterministe) |
| 6.5 · Cohérence | état déclaratif (skills/agents) vs usage réel | skill `weekly-coherence-review` |
| 7 · Rapport | sections déterministes + prose LLM validée → **`weekly-report-<date>.md`** | `weekly_report_prep` / `weekly_report_blocks_draft` / `weekly_report_assemble` |
| 8 · Coût propre | coût de la session du pipeline lui-même (annexe du rapport) | `weekly_self_cost` (déterministe) |

Le rapport final est **le signal** : le cron (ou vous) l'attend chaque semaine — s'il
est absent, quelque chose s'est mal passé. Codes de sortie : `0` complet, `1` partiel
(warnings tolérés), `2` fatal (stoppe sans rapport). En cas de doute sur le poste :
`weekly_doctor` diagnostique base, config et binaires.

## Quickstart

Prérequis : `opencode` ≥ 1.18 avec un modèle authentifié (`opencode auth login`), `uv` —
détails et alternatives : [`INSTALL.md`](INSTALL.md).
`harness-eval` est requis pour exécuter l'étape 5 (le `doctor` signale son absence et
`weekly_harness` échoue fatalement) ; la gate de portabilité du drafting est
fail-soft (binaire absent → note ⚠, commit autorisé). `gh` reste optionnel pour les
repos privés de la veille et cette source se dégrade en warning.

Le lint utilise par défaut le profil `advisory` de `harness_include` : surfaces de
politique (`AGENTS`, agents, commands, configurations et entrypoints plugins) plus
documentation des skills (`SKILL.md`, références et exemples). Le moteur copie ces
seuls fichiers dans une projection temporaire, **étendue au harnais détecté**
(marqueurs projet, voir § Harnais & portabilité) avec injection du contenu engine ;
`node_modules`, le moteur lui-même, les venv/caches et autres artefacts ne sont
donc jamais exposés à `harness-eval`. La projection **copie réellement** les
fichiers (zéro symlink) et trace les orphelins ; les fichiers `.opencode/` hors
allowlist sont signalés dans `harness_include.unscoped_files` et ne sont pas
scannés.

```sh
git clone https://github.com/BenjaminCartonAdeo/weekly-advisor-kit.git && cd weekly-advisor-kit
uv sync --project .opencode/plugins/weekly-advisor-engine --extra dev
# 1 fichier à adapter : project_root + output_dir (chemins absolus)
$EDITOR .opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json
opencode run --agent weekly-advisor "Lance la revue hebdomadaire"
```

- Comptez 10-30 min pour un run complet (télémétrie, réseau et lint compris) ; à la fin,
  le **rapport HTML s'ouvre dans votre navigateur** (`<project_root>/reports/html/weekly-report-latest.html`)
  — l'archive MD reste dans `<output_dir>/runs/current/weekly-report-<date>.md`
- Première fois ? Remplacez la dernière commande par
  `opencode run --agent weekly-advisor "Exécute weekly_doctor et donne son verdict"` —
  il valide base, config et binaires avant le premier run
- Modèle : celui par défaut d'opencode, ou `--model <votre-modèle>` — jamais en dur dans le kit
- Les placeholders `/path/to/...` de la config sont volontaires : tant qu'ils ne sont
  pas adaptés, `weekly_doctor` échoue proprement au lieu de lancer un run sur un mauvais chemin
- **Fenêtre du run** : par défaut `lookback_days` de la config (7 j). Besoin d'une autre
  fenêtre ? Dites-le en toutes lettres (« revue des 3 dernières semaines ») — l'agent
  déduit `N semaines → N×7` et passe `--lookback-days` en override du run, **la config
  n'est jamais réécrite** (v6.0.b)

## Harnais & portabilité

**Placement mono-cible** : chaque projet est rattaché à **un** harnais de drafting,
résolu par détection de marqueurs (priorité `claude-code` > `opencode` >
`copilot-vscode` > `codex`), surchargeable par la config `draft_targets` (`[]` =
mode legacy, toutes cibles). Aucun marqueur → défaut `opencode` + warning du doctor.

| Marqueur projet | Harnais | Cibles de projection des drafts |
|---|---|---|
| `.claude/` | claude-code | `.claude/skills` |
| `.opencode/` | opencode | `.opencode/skills` |
| `.github/prompts/` ou `.github/skills/` | copilot-vscode | `.github/prompts`, `.github/skills` |
| `.agents/` | codex | `.agents` |

**Zéro symlink** : la projection et les artefacts générés sont de vraies copies.

**Portabilité des artefacts générés** (règles `.harness-eval/rules/portability.yaml`,
ids `custom/portability/*`) : frontmatter `name`+`description` seuls · outils par
nom conceptuel (identifiants `weekly_*` interdits) · section « Comment invoquer »
multi-plateforme obligatoire · chemins relatifs projet · scripts auto-contenus.

**Gate de commit** : `weekly_commit_draft` lance `harness-eval skill-verify` (cwd =
racine projet) avant tout commit — ≥ 1 finding `error` → **commit refusé, fix
manuel** ; warnings seuls → note ; binaire absent → note ⚠ (gate ignorée).
**Limite connue** : en 7.10.1 la gate couvre les **skills** uniquement (commands /
agents non inspectés).

## Cron (rappel)

```cron
0 6 * * 1 opencode run --port 4096 --agent weekly-advisor --model <votre-modèle> --dir /chemin/du/kit "Lance la revue hebdomadaire" >> /var/log/weekly-advisor.log 2>&1
```

Rien d'autre sur PATH : le plugin résout python et `harness-eval`. Le rapport final
`<output_dir>/runs/current/weekly-report-<date>.md` est le **signal** du cron (alerte si absent) —
`runs/current` est l'alias stable du run actif (`run_state.json` à la racine de `output_dir`).
**Pour l'utilisateur** : un **rapport HTML autonome** (un seul fichier, zéro CDN) est publié
à chaque run dans `<project_root>/reports/html/` — `weekly-report-<date>.html` (historique)
et `weekly-report-latest.html` (écrasé à chaque run ; config `html_report_dir` pour changer
l'endroit, `""` pour désactiver) — pas besoin de connaître `output_dir` ni `runs/`. Il
s'ouvre automatiquement dans le navigateur à la fin du run (`open_browser: false`, ou
variable d'environnement `WEEKLY_NO_BROWSER=1` pour un cron headless).
Détails et heartbeat recommandé : [`INSTALL.md`](INSTALL.md) §2.7.

## Structure

```
racine/
├── INSTALL.md / INSTALL_PROMPT.md / README.md   ← guides d'installation (humain / agent) et vue d'ensemble
├── proposition-draft-multi-harnais.md           ← proposition multi-harnais (décisions actées, architecture, limites)
├── opencode-weekly-advisor                      ← la spec (contrat complet)
├── scripts/plugin-smoke.mjs                     ← smoke test du plugin (validation d'installation)
├── .harness-eval/rules/portability.yaml         ← règles custom harness-eval (portabilité des artefacts, ids custom/portability/*)
└── .opencode/
    ├── agents/weekly-advisor/weekly-advisor.md   ← orchestration : ordre figé, invariants, bash: deny
    ├── agents/harness-remediator/harness-remediator.md ← remédiation bornée, sans edit/bash
    ├── skills/{weekly-*,harness-remediation}/SKILL.md ← 6 étapes qualitatives (chargées à la demande)
    ├── commands/weekly-review.md / weekly-report.md / harness-remediate.md
    └── plugins/
        ├── weekly-advisor.ts                     ← plugin enveloppe : 16 tools weekly_* (chemins dérivés)
        └── weekly-advisor-engine/                ← moteur Python (package, config, tests, providers multi-harnais)
```

## Documentation

| Document | Contenu |
|---|---|
| [`opencode-weekly-advisor`](opencode-weekly-advisor) | La spec complète (8 parties) — le contrat du pipeline : orchestration, télémétrie, veille, audit, drafting, lint, insights, rapport |
| [`proposition-draft-multi-harnais.md`](proposition-draft-multi-harnais.md) | Proposition multi-harnais : décisions actées (mono-cible, zéro symlink, portabilité, gate harness-eval), architecture livrée, limites |
| [`INSTALL_PROMPT.md`](INSTALL_PROMPT.md) | Installation **pilotée par agent** : prompt autonome à coller ou à lire via raw URL — clone, config, doctor, smoke, pytest, rapport |
| [`INSTALL.md`](INSTALL.md) | Installation pas à pas pour un humain : prérequis, config, validation, cron, mise à jour, dépannage |
| [`README.md`](README.md) | Ce fichier — vue d'ensemble |

## Limites & compatibilité

- **Schéma SQLite d'opencode** : le moteur lit directement les tables internes
  (`session_v2`/`part` ou `session`/`event`), avec pins de schéma vérifiés au run
  (tables/colonnes requises, compteur de migrations). Ce n'est **pas une API publique** :
  une mise à jour majeure d'opencode peut rompre la collecte télémétrique jusqu'à la mise
  à jour du kit — `weekly_doctor` diagnostique base, config et binaires.
- **Veille (étape 2)** : dépend d'APIs publiques (npm, GitHub, registre MCP) — rate-limits
  possibles, warnings par source tolérés, le run continue.
- **`harness-eval` minimum 7.9.0** (v6.1.a) : les versions supérieures sont acceptées sans avertissement —
  la compatibilité du format est garantie par la validation de structure du digest au chargement
  (volet harness dégradé avec message explicite si le format change).

## Développement

```sh
# le projet uv (pyproject + uv.lock) vit dans le dossier moteur
cd .opencode/plugins/weekly-advisor-engine
uv run python -m pytest -q  # 435 tests
uv run ruff check .          # lint
uv run ruff format --check . # format
```

CI GitHub Actions (`.github/workflows/ci.yml`) : install lockfile `--frozen`, lint,
format, 435 tests, packaging et syntaxe du plugin TS — vérifiés sur chaque push/PR.
