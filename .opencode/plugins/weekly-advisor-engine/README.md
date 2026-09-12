# weekly-telemetry-aggregator

Moteur Python du kit weekly-advisor : agrégation de télémétrie OpenCode (lecture
SQLite locale), veille écosystème (npm/GitHub/MCP/RSS), insights, lint harness et
rendu du rapport hebdomadaire — 100 % déterministe, zéro LLM.

- Spécification : `doc/spec/README.md` (racine du kit) — architecture : `doc/architecture/README.md`
- CLI : `python -m weekly_telemetry_aggregator --help` (sous-commandes `run`,
  `releases`, `watch-context`, `watch-validate`, `harness-remediate`, `insights`, `report-prep`,
  `report-assemble`, `doctor`, …)
- Développement : depuis ce dossier, `uv run python -m pytest -q` · `uv run ruff check .`.
  Les workers utilisent cette commande canonique (jamais `uv run pytest` ni
  `uv run rtk pytest`) ; `--collect-only -q` est réservé à un fallback diagnostique
  unique et borné après vérification d'un sélecteur ciblé.

L'étape `harness` lit `harness_include` dans la configuration et exécute
`harness-eval` sur une projection temporaire project-relative. Le profil par défaut
`advisory` couvre les surfaces de politique et la documentation des skills ; les
fichiers `.opencode/` hors allowlist sont reportés dans
`harness_include.unscoped_files` et ne sont pas exposés au scanner.
