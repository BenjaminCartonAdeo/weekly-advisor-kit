"""Table des sous-commandes du CLI — extraite de `cli.py` (split cli, Task 7).

La table est de la **donnée** : `build_parser` reste dans `cli.py` et résout
chaque handler par `globals()[func.__name__]` dans le namespace de `cli` (et non
d'ici). Déplacer la table ne déplace donc pas la résolution ; `cli.py` continue de
ré-exporter chaque handler sous son propre nom, ce qui garde
`monkeypatch.setattr(cli, "<nom>", ...)` actif.

Pourquoi l'entrée `run` est injectée via `build_subcommands(cmd_run)` plutôt que
d'être importée ici comme les autres :

- `_cmd_run` doit rester **défini** dans `cli.py` (invariant 3 du plan) : il
  résout `run` comme global de `cli`, et `tests/test_cli.py` patche
  `weekly_telemetry_aggregator.cli.run` puis appelle `_cmd_run`.
- Un `from .cli import _cmd_run` ici inverserait le DAG et créerait un cycle
  `cli <-> cli_commands` — module-level *et* différé — que refuse
  `tests/test_import_graph.py`. La variante `from . import cli as _cli` +
  `_cli.run(...)` dans `_cmd_run` produirait exactement le même cycle différé.

Les 18 autres entrées sont déplacées verbatim, chaque handler importé depuis son
module propriétaire (`cli_core` / `cli_report` / `cli_watch` /
`cli_skill_curate` / `cli_debug`).
"""

from __future__ import annotations

from collections.abc import Callable

from .cli_core import (
    _cmd_audit_candidates,
    _cmd_commit_draft,
    _cmd_doctor,
    _cmd_draft_candidates,
    _cmd_harness,
    _cmd_harness_remediate,
    _cmd_insights,
    _cmd_releases,
    _cmd_self_cost,
    _cmd_show_session,
    _positive_int,
)
from .cli_debug import _cmd_debug_rule
from .cli_report import (
    _cmd_report_assemble,
    _cmd_report_blocks_check,
    _cmd_report_blocks_draft,
    _cmd_report_prep,
)
from .cli_skill_curate import _cmd_skill_curate
from .cli_watch import (
    _cmd_watch_context,
    _cmd_watch_distill,
    _cmd_watch_validate,
)


def build_subcommands(cmd_run: Callable) -> tuple:
    """Assemble la table `(nom, aide, handler, options)` complète.

    `cmd_run` est le `_cmd_run` défini dans `cli.py` : l'entrée `run` le porte
    telle quelle pour que `test_cli_surface` vérifie `vars(cli)[name] is func`.
    """
    return (
        (
            "run",
            "Aggregate OpenCode telemetry into weekly-summary-<date>.json (default)",
            cmd_run,
            (
                (
                    ("--top-sessions-limit",),
                    {"type": int, "help": "Override top_sessions_limit"},
                ),
                (
                    ("--include-subagents",),
                    {
                        "dest": "include_subagents",
                        "action": "store_true",
                        "default": None,
                    },
                ),
                (
                    ("--no-subagents",),
                    {"dest": "include_subagents", "action": "store_false"},
                ),
                (
                    ("--fail-on-missing-telemetry",),
                    {
                        "action": "store_true",
                        "help": "Exit 1 as soon as a session read fails",
                    },
                ),
            ),
        ),
        (
            "show-session",
            "Render a session transcript (Partie 0 §3)",
            _cmd_show_session,
            (
                (("session_id",), {}),
                (
                    ("--include-children",),
                    {
                        "action": "store_true",
                        "help": "Include child sessions (subagents) via parent_id",
                    },
                ),
                (
                    ("--extract-dir",),
                    {
                        "help": "Also write transcript-extract-<session_id>.md into this directory (Partie 3 §6.b)"
                    },
                ),
            ),
        ),
        (
            "releases",
            "Ecosystem watch + core changes (Partie 2)",
            _cmd_releases,
            (),
        ),
        (
            "watch-context",
            "Join weekly-ecosystem with project plugins/skills/commands/agents",
            _cmd_watch_context,
            (
                (
                    ("--ecosystem",),
                    {"help": "Override the anchor-derived weekly-ecosystem-<date>.json input path"},
                ),
            ),
        ),
        (
            "watch-distill",
            "Deterministic ecosystem distill: fuse, screen, score, quota top-N fiches (étape 2.2)",
            _cmd_watch_distill,
            (),
        ),
        (
            "watch-validate",
            "Validate raw watch findings against the local dated watch context",
            _cmd_watch_validate,
            (),
        ),
        (
            "insights",
            "Deltas, alerts, maintenance rules R1-R4 (Partie 6)",
            _cmd_insights,
            (
                (
                    ("--baseline-summary",),
                    {"help": "Previous summary used when no prior run exists (P1.1, v5.28)"},
                ),
            ),
        ),
        (
            "report-prep",
            "Render deterministic report sections (Partie 7a)",
            _cmd_report_prep,
            (),
        ),
        (
            "report-assemble",
            "Inject LLM blocks into the draft (Partie 7c)",
            _cmd_report_assemble,
            (),
        ),
        (
            "harness",
            "Step 5: scoped harness-eval lint → weekly-harness-digest-<date>.json",
            _cmd_harness,
            (),
        ),
        (
            "harness-remediate",
            "Deterministic, gated harness proposal dry-run or remediation",
            _cmd_harness_remediate,
            (
                (
                    ("--proposal",),
                    {
                        "required": True,
                        "help": "JSON proposal file under output_dir",
                    },
                ),
                (
                    ("--mode",),
                    {
                        "choices": ("dry-run", "apply"),
                        "default": "dry-run",
                        "help": "dry-run is the safe default; apply requires every gate",
                    },
                ),
            ),
        ),
        (
            "report-blocks-draft",
            "Deterministic section-4 blocks draft (v5.28, P5.1)",
            _cmd_report_blocks_draft,
            (),
        ),
        (
            "report-blocks-check",
            "Validate the section-4 prose file WITHOUT consuming it (step 7b.1)",
            _cmd_report_blocks_check,
            (),
        ),
        (
            "audit-candidates",
            "Partie 3 §2: deterministic audit-candidate selection from weekly-summary",
            _cmd_audit_candidates,
            (),
        ),
        (
            "draft-candidates",
            "Partie 4 §3: skill/command-candidate findings, capped, severity DESC",
            _cmd_draft_candidates,
            (),
        ),
        (
            "commit-draft",
            "Validate + commit an auto-drafted skill/command/agent (Partie 4 §7)",
            _cmd_commit_draft,
            (
                (
                    ("--file",),
                    {
                        "required": True,
                        "help": "Absolute path to the SKILL.md, command or agent .md",
                    },
                ),
                (
                    ("--kind",),
                    {
                        "choices": ("skill", "command", "fix", "agent"),
                        "required": True,
                    },
                ),
            ),
        ),
        (
            "doctor",
            "Diagnose the installation (Partie 1 §12)",
            _cmd_doctor,
            (),
        ),
        (
            "self-cost",
            "Cost of the pipeline's own run session (Partie 1 §12)",
            _cmd_self_cost,
            (),
        ),
        (
            "debug-rule",
            "Read-only rule playground: evaluate a rule/expression, list fields, field distributions",
            _cmd_debug_rule,
            (
                (
                    ("action",),
                    {
                        "choices": ("evaluate", "fields", "distributions"),
                        "help": "evaluate <rule-id|expression> | fields | distributions [field]",
                    },
                ),
                (
                    ("target",),
                    {
                        "nargs": "?",
                        "default": None,
                        "help": "evaluate: rule-id or DSL expression; distributions: optional field filter",
                    },
                ),
                (
                    ("--rules-dir",),
                    {
                        "dest": "rules_dir",
                        "help": "Override the rules directory (default: built-in rules/)",
                    },
                ),
                (
                    ("--top",),
                    {
                        "type": _positive_int,
                        "default": 10,
                        "help": "distributions: number of top values per field (default: 10)",
                    },
                ),
            ),
        ),
        (
            "skill-curate",
            "Curation/décroissance skills (R4 curation/GC + R8 TTL) — dry-run par défaut",
            _cmd_skill_curate,
            (
                (
                    ("--coherence",),
                    {"help": "JSON: findings de cohérence (tag_action pertinents)"},
                ),
                (
                    ("--catalog",),
                    {"help": "JSON: catalogue de skills (skill_id, metadata.origin/ttl_policy)"},
                ),
                (
                    ("--usage",),
                    {
                        "help": "JSON: usage_records pour TTL (fallback inter-run .watch-memory.jsonl si absent)"
                    },
                ),
                (
                    ("--runs-seen",),
                    {
                        "type": int,
                        "default": 0,
                        "help": "Nombre de runs consécutifs observés",
                    },
                ),
                (
                    ("--stale-days",),
                    {
                        "type": int,
                        "default": 90,
                        "help": "Seuil d'obsolescence last_loaded (jours)",
                    },
                ),
                (
                    ("--apply",),
                    {
                        "action": "store_true",
                        "help": "Exécute (sinon dry-run, imprime seulement)",
                    },
                ),
            ),
        ),
    )
