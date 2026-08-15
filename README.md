# weekly-advisor-kit

Pipeline de revue hebdomadaire d'usage OpenCode, distribuable en **un seul dossier** :
`.opencode/` contient l'agent, les 5 skills d'étape, les 2 commands, le **plugin enveloppe**
(tools `weekly_*` → moteur Python) et le **moteur** Python (`weekly-telemetry-aggregator`).

Spec : `opencode-weekly-advisor` · **Installation : [`INSTALL.md`](INSTALL.md)**

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
    └── weekly-advisor-engine/                ← moteur Python (package, config, tests — 171 tests)
```

## Documentation

| Document | Contenu |
|---|---|
| [`INSTALL.md`](INSTALL.md) | Installation pas à pas : prérequis, config, validation, cron, mise à jour, dépannage |
| `opencode-weekly-advisor` | La spec complète (8 parties) — le contrat du pipeline : orchestration, télémétrie, veille, audit, drafting, lint, insights, rapport |
| `README.md` | Ce fichier — vue d'ensemble |

## Cron (rappel)

```cron
0 6 * * 1 opencode run --port 4096 --agent weekly-advisor \
    --model opencode/deepseek-v4-flash-free \
    --dir /home/<TOI>/Dev/weekly-advisor-kit "Lance la revue hebdomadaire" \
    >> /var/log/weekly-advisor.log 2>&1
```

Rien d'autre sur PATH : le plugin résout python et `harness-eval`. Le rapport final
`<output_dir>/weekly-report-<date>.md` est le **signal** du cron (alerte si absent).
Détails et heartbeat recommandé : [`INSTALL.md`](INSTALL.md) §2.7.

## Développement

```sh
uv pip install --python .venv/bin/python -e ".opencode/plugins/weekly-advisor-engine[dev]"
# tests : depuis le DOSSIER MOTEUR (les tests importent `tests` depuis le cwd)
cd .opencode/plugins/weekly-advisor-engine
../../../.venv/bin/python -m pytest tests -q    # 171 tests
```