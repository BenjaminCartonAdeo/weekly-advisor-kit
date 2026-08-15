# weekly-advisor-kit

Pipeline de revue hebdomadaire d'usage OpenCode, distribuable en **un seul dossier** :
`.opencode/` contient l'agent, les 5 skills d'étape, les 2 commands, le **plugin enveloppe**
(tools `weekly_*` → moteur Python) et le **moteur** Python (`weekly-telemetry-aggregator`).

Spec : `opencode-weekly-advisor-5` · Décisions : `opencode-weekly-advisor-CHANGELOG.md`

**Validé** : run complet **exit 0 sur Adeo le 15 août 2026** (12 étapes, outils du plugin,
drafting honnête — 0 candidat → aucun commit forcé). Déployé en production sur Adeo
(migration commitée `2605479`) ; ce kit en est le miroir de distribution.

## Structure

```
.opencode/
├── agents/weekly-advisor/weekly-advisor.md   ← squelette : ordre figé, invariants, zéro bash
├── skills/weekly-*/SKILL.md                  ← 5 étapes LLM (chargées à la demande)
├── commands/weekly-review.md / weekly-report.md
├── plugins/
│   ├── weekly-advisor.ts                     ← plugin enveloppe : 13 tools weekly_* (chemins dérivés)
│   └── weekly-advisor-engine/                ← moteur Python + config + tests (170 tests)
└── package.json                              ← @opencode-ai/plugin (typages + helper tool)
```

## Installation sur un poste

```sh
git clone <repo-du-kit> weekly-advisor-kit && cd weekly-advisor-kit
# 1. venv du kit (hors .opencode, gitignoré) :
uv venv .venv
uv pip install --python .venv/bin/python -e .opencode/plugins/weekly-advisor-engine
# 2. config : project_root (repo audité), output_dir, git_name/git_email
nano .opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json
# 3. permissions user (non-interactif) : "permission": {"*": "allow"} dans ~/.config/opencode/opencode.json
#    + auth modèle : opencode auth login  (le modèle passe par --model du cron, jamais dans l'agent)
# 4. binaires du poste : opencode ≥1.18, harness-eval 7.9.0, git, python ≥3.11 + uv
# 5. tests + diagnostic (depuis le dossier moteur — les tests importent `tests` depuis le cwd) :
cd .opencode/plugins/weekly-advisor-engine
../../../.venv/bin/python -m pytest -q
cd ../../..
opencode run --agent weekly-advisor --dir . "Exécute weekly_doctor et donne son verdict"
```

> `.npmrc` local : le registre npm public est requis pour `@opencode-ai/plugin` (introuvable
> sur les registres internes) — **sauf si** votre registre interne proxifie npm public
> (vérifié chez Adeo : jfrog résout `@opencode-ai/plugin` sans `.npmrc`). Le venv et
> `node_modules` sont gitignorés.

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
(alerte si absent). Pièges connus : permission user `ask` auto-rejetée en non-interactif
(mesuré v5.32 — prévoir `"permission": {"*": "allow"}` en user) ; draft consommé par
l'assemble (relancer `weekly_report_prep` avant un nouvel assemble) ; passe 6.5 : les
coherence-findings du jour existants sont conservés (archive) — fraîcheur garantie au
premier run du jour.

## Développement

```sh
uv pip install --python .venv/bin/python -e .opencode/plugins/weekly-advisor-engine[dev]
# tests : depuis le DOSSIER MOTEUR (les tests importent `tests` depuis le cwd)
cd .opencode/plugins/weekly-advisor-engine
../../../.venv/bin/python -m pytest tests -q
```