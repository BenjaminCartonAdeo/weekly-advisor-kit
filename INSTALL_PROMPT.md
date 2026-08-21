# INSTALL_PROMPT.md — Installation autonome du kit weekly-advisor

kit_version: 6.1
source_repo: https://github.com/BenjaminCartonAdeo/weekly-advisor-kit
clone_url: https://github.com/BenjaminCartonAdeo/weekly-advisor-kit.git

> Usage : colle ce fichier dans une session OpenCode (remplace `<TARGET>`), ou demande
> à un agent : « Lis https://raw.githubusercontent.com/BenjaminCartonAdeo/weekly-advisor-kit/master/INSTALL_PROMPT.md et exécute-le. »

## 1. Mission

Installe le kit weekly-advisor (version `6.1` ci-dessus) dans le repo local `<TARGET>`,
adapte sa configuration à ce repo, valide l'installation par des portes déterministes et
écris un rapport. Ne fais **rien d'autre**.

## 2. Variables — les seules substitutions autorisées

- `<TARGET>` : chemin **absolu** du repo cible (repo git local ; sans espaces ni caractères spéciaux)
- `<DATE>` : date du jour au format `YYYY-MM-DD`

## 3. Périmètre

**AUTORISÉ :**
- `/tmp` (téléchargement, extraction)
- `<TARGET>/.opencode/` (copie du kit), `<TARGET>/reports/` (rapport d'installation)
- réseau sortant limité à github.com (`github.com`, `raw.githubusercontent.com`)
- commandes : `git clone` / `git rev-parse` (lecture), `uv sync`, `sed` (config),
  `python`/`pytest` du venv moteur, `node` (smoke), `cp`, `rm`, `cmp`, `grep`, `mkdir`

**INTERDIT (violation = arrêt immédiat + rapport KO) :**
- crontab : **ne jamais** installer/modifier — la ligne est seulement *proposée* en fin de run
- `git commit`, `push`, `checkout`, `rebase`, `reset` — partout, y compris `<TARGET>`
- `sudo`, droits admin, modification de fichiers hors périmètre (dotfiles, autres repos)
- modification de la config OpenCode globale, secrets, tokens, `auth login`
- réécriture des fichiers produits : config après adaptation, JSON de rapport, sorties de doctor/pytest
- exécution du pipeline complet (`weekly_run`, `weekly_harness`, `weekly_report_*`, …) — l'installation n'est pas la revue
- toute commande non listée dans ce document ; en cas de doute : **STOP + rapport**, jamais de devinette

## 4. Prérequis (vérifier chacun — un seul manquant = échec fatal avant toute écriture)

| Binaire | Minimum | Vérification |
|---|---|---|
| `git` | — | `git --version` |
| `uv` | ≥ 0.4 | `uv --version` |
| `python3` | ≥ 3.11 | `python3 --version` |
| `node` | ≥ 18 | `node --version` |
| `harness-eval` | 7.9.0 (épinglé) | `harness-eval --version` doit afficher `7.9.x` |
| `opencode` | ≥ 1.18 | `opencode --version` — **manquant = warning non fatal** (le doctor le signale) |

Si `harness-eval` est absent ou ≠ 7.9.x : `uv tool install harness-eval`, puis revérifier.
Si `opencode` est absent : continuer avec un warning dans le rapport.

## 5. Étapes — ordre figé, arrêt immédiat dès qu'une condition attendue n'est pas remplie

### 5.0 Preflight cible

```sh
git -C <TARGET> rev-parse --is-inside-work-tree   # doit afficher `true`, sinon STOP
test -d <TARGET>/.opencode && echo "EXISTS" || echo "ABSENT"   # note l'état (réinstallation)
```

### 5.1 Récupération du kit — clone frais, jamais de tarball

> GitHub met en cache les tarballs codeload ~5 min après un push : une archive relue
> peut être périmée. `git clone --depth 1` est toujours frais et fournit le SHA exact.

```sh
rm -rf /tmp/weekly-advisor-kit-src
git clone --depth 1 --branch master https://github.com/BenjaminCartonAdeo/weekly-advisor-kit.git \
  /tmp/weekly-advisor-kit-src
SRC=/tmp/weekly-advisor-kit-src
```

### 5.2 Vérification du clone (toutes obligatoires, une seule KO = STOP)

```sh
grep -m1 "^kit_version:" "$SRC/INSTALL_PROMPT.md"   # doit contenir `6.1`
for f in INSTALL.md README.md opencode-weekly-advisor \
  .opencode/plugins/weekly-advisor.ts \
  .opencode/agents/weekly-advisor/weekly-advisor.md \
  .opencode/agents/harness-remediator/harness-remediator.md \
  .opencode/skills/harness-remediation/SKILL.md \
  .opencode/commands/harness-remediate.md \
  .opencode/plugins/weekly-advisor-engine/pyproject.toml \
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/__init__.py; do
  test -f "$SRC/$f" || { echo "MANQUANT: $f"; exit 2; }
done
git -C "$SRC" rev-parse HEAD
# note le SHA affiché → `source_sha` du rapport (audit de reproductibilité)
```

### 5.3 Détection de conflit — jamais d'écrasement silencieux

Parcours chaque fichier de `$SRC/.opencode` (exclusions : `.venv/`, `__pycache__/`,
`.pytest_cache/`, `.ruff_cache/`, `node_modules/`, `.git/`) :
- `<TARGET>/<chemin>` absent → liste **NEW** (à copier)
- présent et `cmp -s` OK → identique, rien à faire
- présent et différent → **CONFLIT** : STOP immédiat + rapport avec la liste complète

Fais de même pour `README.md`, `INSTALL.md`, `opencode-weekly-advisor` à la racine de `<TARGET>`.
Si `<TARGET>/.opencode` était absent (5.0), il n'y a aucun conflit possible.

### 5.4 Copie

```sh
cp -a "$SRC/.opencode/." <TARGET>/.opencode/
cp -a "$SRC/README.md" "$SRC/INSTALL.md" "$SRC/opencode-weekly-advisor" <TARGET>/
```

(5.3 garantit qu'aucun fichier préexistant n'est écrasé → aucune perte.)

### 5.5 Environnement moteur

```sh
uv sync --project <TARGET>/.opencode/plugins/weekly-advisor-engine --all-extras
```

Succès sans erreur, sinon STOP (rollback 5.8).

### 5.6 Configuration — adaptation obligatoire

```sh
cp <TARGET>/.opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json \
   /tmp/weekly-advisor-kit-src/config.pre-install
sed -i "s|/path/to/weekly-advisor-kit|<TARGET>|g" \
   <TARGET>/.opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json
grep -n "project_root\|output_dir" <TARGET>/.opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json
```

Les deux valeurs doivent montrer `<TARGET>` (project_root) et `<TARGET>/reports` (output_dir).
**Ne modifie rien d'autre** : `harness_include`, budgets, `watch` restent les valeurs du kit.

### 5.7 Validation — toutes obligatoires

**1. Doctor (cible)**

```sh
cd <TARGET>/.opencode/plugins/weekly-advisor-engine
.venv/bin/python -m weekly_telemetry_aggregator doctor
```

- rc `0` ou `1` = OK (warning `opencode` hors PATH acceptable)
- rc `2` = **FATAL** → rollback (5.8) + rapport KO

**2. Smoke plugin (depuis le clone, pas la cible)**

```sh
cd /tmp/weekly-advisor-kit-src
node scripts/plugin-smoke.mjs
```

Doit terminer sur `SMOKE OK — plugin chargé et exécuté (closure, lookback, ancre)`, sinon STOP.

**3. Tests moteur (cible)**

```sh
cd <TARGET>/.opencode/plugins/weekly-advisor-engine
.venv/bin/python -m pytest tests/ -q
```

100 % passed, zéro failed, sinon STOP.

**4. Enregistrement agent (warning seulement)** — si `opencode` est disponible :

```sh
opencode agent list
```

`weekly-advisor` doit apparaître ; sinon warning dans le rapport (non bloquant).

### 5.8 Rollback — uniquement sur échec fatal (5.5 → 5.7)

- supprime **uniquement** les fichiers de la liste **NEW** (5.3) copiés par ce run
- restaure la config : `cp /tmp/weekly-advisor-kit-src/config.pre-install <chemin config>`
- ne touche **jamais** un fichier préexistant (5.3 l'a garanti)
- nettoie `/tmp/weekly-advisor-kit-src` sauf le rapport de rollback

### 5.9 Rapport d'installation

Écris `<TARGET>/reports/weekly-install-<DATE>.json` :

```jsonc
{
  "schema_version": 1,
  "kit_version": "6.1",
  "source_sha": "<SHA de 5.2>",
  "target": "<TARGET>",
  "date": "<DATE>",
  "phases": [ { "id": "…", "status": "ok|failed|skipped", "detail": "…" } ],
  "validations": { "doctor_rc": 0, "smoke": "SMOKE OK", "pytest": "passed" },
  "warnings": [],
  "conflicts": [],
  "rolled_back": false,
  "conclusion": "INSTALL OK"
}
```

`conclusion` = `INSTALL OK` ou `INSTALL KO: <phase fautive>`. Ne réécris jamais ce fichier ensuite.

### 5.10 Résumé final (à l'écran, rien d'autre)

- `INSTALL OK` / `INSTALL KO: <phase>`
- validations : doctor rc, smoke, pytest (vert / rouge)
- avertissements éventuels
- **ligne crontab PROPOSÉE** (jamais installée) — remplace `<TARGET>` et les chemins PATH réels :

```
30 18 * * 1 PATH=/home/<user>/.cargo/bin:/home/<user>/.opencode/bin:/home/<user>/.local/bin:/usr/local/bin:/usr/bin:/bin /bin/sh -c 'export WEEKLY_NO_BROWSER=1; log=$HOME/log/weekly-advisor-$(date -Is | tr ":" "-").log; { echo "START $(date -Is)"; /usr/bin/flock -n /tmp/weekly-advisor.lock /usr/bin/timeout --signal=TERM --kill-after=5m 90m "$(command -v opencode)" run --agent weekly-advisor --dir <TARGET> "Lance la revue hebdomadaire"; rc=$?; echo "END $(date -Is) exit=$rc"; exit "$rc"; } >"$log" 2>&1'
```

- rappels : `opencode auth login` si non fait ; un premier run manuel (`opencode run --agent weekly-advisor --dir <TARGET>`) est recommandé avant de brancher le cron

## 6. Contrat anti-hallucination — relis-le avant de commencer

1. Aucune commande hors de ce document ; substitutions limitées à `<TARGET>` et `<DATE>`.
2. Les sorties attendues sont **littérales** (`true`, `SMOKE OK`, pytest passed, rc 0/1) — un écart est un échec, pas une adaptation.
3. Ne réécris jamais : config après adaptation, JSON de rapport, sorties de doctor/pytest, digest.
4. Ne lance jamais le pipeline ni la revue hebdomadaire.
5. Aucun commit, push, checkout, crontab, sudo, secret ; aucune édition hors périmètre.
6. En cas d'ambiguïté : **STOP et rapporte la question** — ne devine pas, ne « corrige » pas le prompt.
