# INSTALL.md — Installation & validation du kit

Guide pas à pas pour installer, configurer et valider `weekly-advisor-kit` sur un poste.
Temps total : ~10 min (hors run complet de test : 30-45 min).

> **Installation pilotée par agent ?** Utilisez [`INSTALL_PROMPT.md`](INSTALL_PROMPT.md) :
> collez son contenu dans une session OpenCode (substituez `<TARGET>`), ou demandez à un
> agent de lire `https://raw.githubusercontent.com/BenjaminCartonAdeo/weekly-advisor-kit/master/INSTALL_PROMPT.md`
> et de l'exécuter. Ce guide reste la référence humaine (explications, dépannage, cron).

> Vue d'ensemble et architecture : [`README.md`](README.md) · Contrat complet : `opencode-weekly-advisor`

## 1. Prérequis (binaires du poste)

| Binaire | Minimum | Installation | Vérification |
|---|---|---|---|
| `opencode` | ≥ 1.18 | `curl -fsSL https://opencode.ai/install \| bash` | `opencode --version` |
| `uv` | — | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `uv --version` |
| `harness-eval` | 7.9.0 (épinglé) | `uv tool install harness-eval` — paquet **PyPI**, pas npm | `harness-eval --version` (le doctor alerte en cas de dérive) |
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
uv sync --project .opencode/plugins/weekly-advisor-engine --all-extras
```

Le venv vit dans le projet moteur (`.opencode/plugins/weekly-advisor-engine/.venv`) —
le plugin enveloppe le résout seul, aucune variable d'environnement. `--all-extras`
installe le dev (`pytest`, `ruff`) ; sans lui le run fonctionne mais pas les tests.

### 2.3 Configuration — adaptation OBLIGATOIRE avant tout run

Fichier : `.opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json`

| Clé | Rôle | Valeur commitée |
|---|---|---|
| `project_root` | **Chemin ABSOLU du repo audité** — celui qui contient `.opencode/` | placeholder `/path/to/weekly-advisor-kit` ✋ |
| `output_dir` | Chemin ABSOLU des artefacts (rapports JSON/MD ; chaque run écrit dans `runs/<date>-<uuid8>/`, alias `runs/current`) | placeholder `/path/to/weekly-advisor-kit/reports` ✋ |
| `report_dir` | (optionnel) Répertoire de la **copie utilisateur** du rapport — défaut `~/weekly-reports/weekly-report-latest.md` ; `""` désactive la publication (v6.0.l) | absent → défaut activé |
| `kit_root` | (optionnel) Worktree du kit pour la synchro best-effort des drafts auto-rédigés (`commit-draft`, v6.0.l) | absent → désactivé |
| `harness_include` | Profil et globs allowlistés pour l'étape `harness` | `advisory` (policy + documentation) |
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

Toutes les autres clés ont des défauts raisonnables (fenêtre 7 j, budgets, seuils,
veille `watch`). Personnalisez `watch` (sources de veille) et les budgets selon votre usage.
Le profil `strict` limite le lint aux surfaces de politique et aux entrypoints de
plugins ; `advisory` ajoute `.opencode/skills/**/SKILL.md`, leurs références et leurs
exemples. Les chemins sont toujours relatifs au projet et la projection est temporaire.
Les exclusions générées/vendor (`node_modules`, moteur, venv, caches, bytecode,
`.git`, `dist/build/coverage`) restent obligatoires ; `exclude_patterns` ne peut qu'en
ajouter.
Un fichier `.opencode/` qui ne correspond à aucun profil apparaît comme warning dans le
digest (`harness_include.unscoped_files`) au lieu d'être scanné silencieusement.

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
uv run pytest -q     # 234 tests — tout doit passer
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

- Durée : 30-45 min (12 étapes ; `weekly_harness` ≈ 100 s à elle seule)
- Artefact attendu : `<output_dir>/runs/current/weekly-report-<date>.md` (le répertoire du run
  actif — `runs/current/` est un alias stable) — **c'est le signal** du run
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
- **Heartbeat externe recommandé** (ex. healthchecks.io) pingé en début de run : seul
  maillon qui alerte si le cron lui-même ne tourne pas (spéc §0.5)

## 3. Mise à jour

```sh
git pull
uv sync --project .opencode/plugins/weekly-advisor-engine --all-extras
cd .opencode/plugins/weekly-advisor-engine && uv run pytest -q
cd ../../..
opencode run --agent weekly-advisor --model <votre-modèle> \
    --dir . "Exécute weekly_doctor et donne son verdict"
```

`doctor` est à relancer après chaque mise à jour (contrat spec §1.12).

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
| `No module named pytest` | install sans `--all-extras` | `uv sync --project .opencode/plugins/weekly-advisor-engine --all-extras` |
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
uv run pytest tests -q

# lint python
uvx ruff check weekly_telemetry_aggregator tests

# édition du plugin TS (optionnel) : typages @opencode-ai/plugin
cd .opencode && bun install   # puis éditer plugins/weekly-advisor.ts
```

Ajouter un tool = ajouter une sous-commande CLI (`cli.py`) **puis** l'exposer dans le
plugin : le CLI reste testable sans opencode, le plugin est une enveloppe sans logique.
