# weekly-telemetry-aggregator

Moteur Python du kit weekly-advisor : agrégation de télémétrie OpenCode (lecture
SQLite locale), veille écosystème (npm/GitHub/MCP/RSS), insights, lint harness et
rendu du rapport hebdomadaire — 100 % déterministe, zéro LLM.

- Spécification : `opencode-weekly-advisor` (racine du kit)
- CLI : `python -m weekly_telemetry_aggregator --help` (sous-commandes `run`,
  `releases`, `insights`, `report-prep`, `report-assemble`, `doctor`, …)
- Développement : `uv run pytest -q` (177 tests) · `uv run ruff check .`
