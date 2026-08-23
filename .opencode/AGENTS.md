# weekly-advisor-kit

Kit du plugin weekly-advisor : revue hebdomadaire des sessions d'agents de code —
collecte télémétrique, audit qualité, veille écosystème, drafting de skills,
remédiation harness-eval et rapport final.

## Navigation

- `README.md` — présentation et usage général
- `INSTALL.md` — installation, configuration et exécution des étapes
- `ARCHITECTURE.md` — architecture du kit et du pipeline
- `opencode-weekly-advisor` — spec du plugin (fichier plat à la racine)

## Structure

- `.opencode/skills/` — skills du plugin weekly-advisor, une par étape du pipeline
- `.opencode/plugins/weekly-advisor-engine/` — moteur Python (agrégation, décisions)
- `.opencode/agents/` et `.opencode/commands/` — harnais de travail
- `.harness-eval/rules/` — règles custom du scanner harness-eval (ne pas modifier)

## Usage

Les étapes s'exécutent via les sous-commandes du plugin (voir `INSTALL.md`).
Chaque skill décrit son étape : sélection des candidats, transcription, veille,
drafting, remédiation, rapport. Vérifier la sortie du scanner harness-eval avant
de déclarer un livrable valide.