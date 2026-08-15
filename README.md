# weekly-advisor-kit

Pipeline de revue hebdomadaire d'usage OpenCode, distribuable en **un seul dossier** :
`.opencode/` contient l'agent, les 5 skills d'étape, les 2 commands, le **plugin enveloppe**
(tools `weekly_*` → moteur Python) et le **moteur** Python (`weekly-telemetry-aggregator`).

Spec : `opencode-weekly-advisor-5` · Décisions : `opencode-weekly-advisor-CHANGELOG.md`

**Validé** : run complet **exit 0 sur Adeo le 15 août 2026** (12 étapes, outils du plugin,
drafting honnête — 0 candidat → aucun commit forcé). Déployé en production sur Adeo ;
ce kit en est le miroir de distribution.

## Quickstart (6 commandes)

```sh
git clone https://github.com/BenjaminCartonAdeo/weekly-advisor-kit.git && cd weekly-advisor-kit
uv venv .venv && uv pip install --python .venv/bin/python -e ".opencode/plugins/weekly-advisor-engine[dev]"
# ÉDITE la config — project_root et output_dir sont en placeholder (/path/to/...) :
#   nano .opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json
cd .opencode/plugins/weekly-advisor-engine && ../../../.venv/bin/python -m pytest -q && cd ../../..
opencode run --agent weekly-advisor --model opencode/deepseek-v4-flash-free --dir . "Exécute weekly_doctor et donne son verdict"
```

## Structure

```
.opencode/
├── agents/weekly-advisor/weekly-advisor.md   ← squelette : ordre figé, invariants, bash: deny
├── skills/weekly-*/SKILL.md                  ← 5 étapes LLM (chargées à la demande)
├── commands/weekly-review.md / weekly-report.md
└── plugins/
    ├── weekly-advisor.ts                     ← plugin enveloppe : 13 tools weekly_* (chemins dérivés)
    └── weekly-advisor-engine/                ← moteur Python (package, config, tests — 170 tests)
```

## Installation sur un poste (pas à pas)

**Prérequis** (binaires du poste) :

| Binaire | Minimum | Installation |
|---|---|---|
| `opencode` | ≥ 1.18 | `curl -fsSL https://opencode.ai/install \| bash` |
| `uv` | — | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| `harness-eval` | 7.9.0 | `uv tool install harness-eval` (paquet PyPI, pas npm) |
| `git`, `python` | ≥ 3.11 | gestionnaire du poste |
| `bun` | optionnel | **non requis au runtime** — utile seulement pour éditer le plugin TS (typages `@opencode-ai/plugin`) ; opencode fournit le module au chargement |

```sh
git clone https://github.com/BenjaminCartonAdeo/weekly-advisor-kit.git && cd weekly-advisor-kit

# 1. venv du kit (hors .opencode, gitignoré) — le suffixe [dev] installe pytest :
uv venv .venv
uv pip install --python .venv/bin/python -e ".opencode/plugins/weekly-advisor-engine[dev]"

# 2. CONFIG — adaptation OBLIGATOIRE avant tout run (2 clés commitées en placeholder
#    "/path/to/weekly-advisor-kit") :
nano .opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json
#    - project_root : chemin ABSOLU du repo audité (ici : votre clone)
#    - output_dir   : chemin ABSOLU des artefacts (rapports JSON/MD)
#    - git_name / git_email : identité des commits auto-rédigés (déjà neutres)
#    Si vous lancez un run sans adapter : weekly_doctor échoue (project_root/.opencode absent).

# 3. permissions user + auth modèle (une fois par poste) :
#    - "permission": {"*": "allow"} dans ~/.config/opencode/opencode.json
#      (non-interactif : une permission `ask` est auto-rejetée — mesuré v5.32)
#    - opencode auth login   (le modèle passe par --model du cron, jamais dans l'agent)

# 4. tests + diagnostic (depuis le DOSSIER MOTEUR — les tests importent `tests` depuis le cwd) :
cd .opencode/plugins/weekly-advisor-engine
../../../.venv/bin/python -m pytest -q        # 170 tests — tout doit passer
cd ../../..
opencode run --agent weekly-advisor --model opencode/deepseek-v4-flash-free \
    --dir . "Exécute weekly_doctor et donne son verdict"
#    attendu : « Kit OK ... Prêt pour un run » — sinon, les causes sont listées (exit 1/2).
```

## Cron

```cron
SHELL=/bin/bash
PATH=/home/<TOI>/.local/bin:/usr/local/bin:/usr/bin:/bin
0 6 * * 1 opencode run --port 4096 --agent weekly-advisor \
    --model opencode/deepseek-v4-flash-free \
    --dir /home/<TOI>/Dev/weekly-advisor-kit "Lance la revue hebdomadaire" \
    >> /var/log/weekly-advisor.log 2>&1
```

Plus **rien à mettre sur PATH** pour la pipeline : le plugin résout lui-même le python
(`<worktree>/.venv/bin/python`) et le moteur — le cron n'a besoin que d'`opencode` et
`git`. Le rapport final : `<output_dir>/weekly-report-<date>.md` — c'est le signal du cron
(alerte si absent). Pièges connus : draft consommé par l'assemble (relancer
`weekly_report_prep` avant un nouvel assemble) ; passe 6.5 : les coherence-findings du
jour existants sont conservés (archive) — fraîcheur garantie au premier run du jour.

## Développement

```sh
uv pip install --python .venv/bin/python -e ".opencode/plugins/weekly-advisor-engine[dev]"
# tests : depuis le DOSSIER MOTEUR (les tests importent `tests` depuis le cwd)
cd .opencode/plugins/weekly-advisor-engine
../../../.venv/bin/python -m pytest tests -q
```