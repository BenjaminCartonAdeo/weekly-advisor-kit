# weekly-telemetry-aggregator

Moteur Python du kit weekly-advisor : agrégation de télémétrie OpenCode (lecture
SQLite locale), veille écosystème (npm/GitHub/MCP/RSS), insights, lint harness et
rendu du rapport hebdomadaire — 100 % déterministe, zéro LLM.

- Spécification : `opencode-weekly-advisor` (racine du kit)
- CLI : `python -m weekly_telemetry_aggregator --help` (sous-commandes `run`,
  `releases`, `watch-context`, `watch-validate`, `insights`, `report-prep`,
  `report-assemble`, `doctor`, …)
- Développement : `uv run pytest -q` · `uv run ruff check .`

L'étape `harness` lit `harness_include` dans la configuration et exécute
`harness-eval` sur une projection temporaire project-relative. Le profil par défaut
`advisory` couvre les surfaces de politique et la documentation des skills ; les
fichiers `.opencode/` hors allowlist sont reportés dans
`harness_include.unscoped_files` et ne sont pas exposés au scanner.
