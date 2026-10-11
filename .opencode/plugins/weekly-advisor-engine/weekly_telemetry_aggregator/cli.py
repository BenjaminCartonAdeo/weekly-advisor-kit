"""CLI entry point for weekly-telemetry-aggregator (Part 0 §3, Part 1 §5).

Facade : les handlers vivent dans des sous-modules (`cli_core`, `cli_report`,
`cli_watch`, `cli_skill_curate`, `cli_debug`) et sont ré-importés ici sous leur
propre nom, car `build_parser` fait `globals()[func.__name__]`. Seuls `_cmd_run`,
`build_parser` et `main` restent définis dans ce module (invariants 1-3 du plan
de split) ; la table `_SUBCOMMANDS` vit dans `cli_commands` et est instanciée ici
avec `_cmd_run` (voir `cli_commands.build_subcommands`).

Subcommands: run (default), show-session, releases, watch-context, watch-distill,
watch-validate, insights, report-prep, report-blocks-check, report-assemble, harness,
harness-remediate, audit-candidates, draft-candidates, commit-draft, doctor, self-cost,
skill-curate, debug-rule.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__

# --------------------------------------------------------------------------
# Ré-exports de la façade : chaque handler déplacé doit rester un attribut de
# `cli` sous son propre nom (contrat tests/test_cli_surface.py). `build_parser`
# fait `globals()[func.__name__]`, donc la résolution doit rester dans CE
# module — voir l'invariant 1 du plan de split.
# --------------------------------------------------------------------------
from .cli_commands import build_subcommands
from .cli_core import (  # noqa: F401
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
    _deduplicate_worker_statuses,
    _load_cfg,
    _out_dir,
    _positive_int,
)
from .cli_debug import (  # noqa: F401
    _cmd_debug_rule,
    _debug_collect_paths,
    _debug_collect_values,
    _debug_context_finding,
    _debug_dsl_functions,
    _debug_type_name,
)
from .cli_report import (  # noqa: F401
    _cmd_report_assemble,
    _cmd_report_blocks_check,
    _cmd_report_blocks_draft,
    _cmd_report_prep,
)
from .cli_skill_curate import (  # noqa: F401
    _archive_skill,
    _auto_load,
    _auto_load_catalog,
    _cmd_skill_curate,
    _global_skill_roots,
    _normalize_skill_relative,
    _path_is_global_or_unresolvable,
    _path_within,
    _read_json_arg,
    _skill_dirs_for,
)
from .cli_watch import (  # noqa: F401
    _cmd_watch_context,
    _cmd_watch_distill,
    _cmd_watch_validate,
    _load_previous_carried_over,
)
from .main import run


def _cmd_run(args, cfg) -> int:
    rc = run(
        cfg,
        anchor=args.anchor,
        top_sessions_limit=args.top_sessions_limit,
        include_subagents=args.include_subagents,
        fail_on_missing_telemetry=args.fail_on_missing_telemetry,
        lookback_days=args.lookback_days,
    )
    # `run()` predates the final JOIN contract and reports every partial worker
    # warning as rc=1.  Re-read the source-of-truth summary once it exists so
    # valid transcript-truncated/recovered/report-only inputs remain observable
    # without becoming a counted failure.  Fatal rc=2 is never downgraded.
    if rc >= 2 or rc < 0:
        return rc
    if rc not in (0, 1):
        return rc
    from .main import _parse_anchor
    from .report import (
        _audit_artifact_declarations,
        _branch_applicability,
        _join_status_records,
        applicable_summary_rc,
        validate_required_artifacts,
    )

    run_time = _parse_anchor(args.anchor)
    date = run_time.strftime("%Y-%m-%d")
    out = _out_dir(cfg, date)
    timings_path = out / f"weekly-timings-{date}.json"
    timings: object = None
    if timings_path.is_file():
        try:
            timings = json.loads(timings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            timings = None
    gate = validate_required_artifacts(
        out,
        date,
        applicability=_branch_applicability(timings, date, out=out),
        dynamic_audit_artifacts=_audit_artifact_declarations(timings),
    )
    if gate["status"] != "pass":
        return 2
    summary_path = out / f"weekly-summary-{date}.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return 2
    if not isinstance(summary, dict):
        return 2
    additional_records: list[object] = []
    if timings is not None:
        additional_records = _join_status_records(timings)
    return applicable_summary_rc(
        summary,
        out=out,
        date=date,
        additional_records=additional_records,
        project_root=cfg.project_root,
        fallback_rc=rc,
    )


# ------------------------------------------------------------------ parser


# La table est de la donnée : elle vit dans `cli_commands` et reçoit `_cmd_run`
# (défini ici, invariant 3) pour l'entrée `run`. `build_parser` continue de
# résoudre chaque handler via `globals()[func.__name__]` dans CE namespace.
_SUBCOMMANDS = build_subcommands(_cmd_run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weekly-telemetry-aggregator",
        description=f"Pipeline opencode-weekly-advisor v{__version__} — télémétrie, veille, insights, cohérence, rapport (spec v6.0.l).",
    )
    parser.add_argument(
        "--config",
        help="Path to weekly-telemetry-config.json (default: <cwd>/weekly-telemetry-config.json)",
    )
    parser.add_argument(
        "--anchor",
        help="ISO-8601 run_time (default: now UTC); identique pour toutes les sous-commandes",
    )
    parser.add_argument("--output-dir", help="Override output_dir from config")
    parser.add_argument(
        "--lookback-days",
        type=_positive_int,
        help="Override de run : fenêtre en jours (défaut = config lookback_days)",
    )

    # Global flags are available both BEFORE the subcommand (parser) and AFTER
    # (each subparser reuses parent), so `run --anchor X` == `--anchor X run`.
    global_parent = argparse.ArgumentParser(add_help=False)
    # default=SUPPRESS: if a subcommand omits the flag, argparse does NOT reset it
    # to None, so a value parsed on the main parser (before the subcommand) is kept.
    global_parent.add_argument("--config", help=argparse.SUPPRESS, default=argparse.SUPPRESS)
    global_parent.add_argument("--anchor", help=argparse.SUPPRESS, default=argparse.SUPPRESS)
    global_parent.add_argument("--output-dir", help=argparse.SUPPRESS, default=argparse.SUPPRESS)
    global_parent.add_argument(
        "--lookback-days", help=argparse.SUPPRESS, default=argparse.SUPPRESS, type=_positive_int
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    # run is the default subcommand (spec Partie 0 §3). Default all its flags so a
    # bare invocation resolves every attribute _cmd_run reads from the namespace.
    parser.set_defaults(
        command="run",
        func=_cmd_run,
        top_sessions_limit=None,
        include_subagents=None,
        fail_on_missing_telemetry=False,
        session_id=None,
        include_children=False,
        extract_dir=None,
        file=None,
        kind=None,
        baseline_summary=None,
    )

    for name, help_text, func, arguments in _SUBCOMMANDS:
        # Résolution tardive : les tests remplacent les handlers après import.
        handler = globals()[func.__name__]
        command_parser = sub.add_parser(name, parents=[global_parent], help=help_text)
        for flags, options in arguments:
            command_parser.add_argument(*flags, **options)
        command_parser.set_defaults(func=handler)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows : forcer UTF-8 sur stdout/stderr (consoles cp1252/cp850 sinon) ;
    # no-op sur les plateformes déjà UTF-8.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    from .main import EXIT_TOTAL_FAILURE

    try:
        cfg = _load_cfg(args)
        return args.func(args, cfg)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        # v6.2 spec §4 : toute exception non gérée est fatale (exit 2 = stop),
        # jamais un « dégradé, je continue » — message explicite sur stderr.
        print(f"FATAL: {exc}", file=sys.stderr)
        return EXIT_TOTAL_FAILURE


if __name__ == "__main__":
    sys.exit(main())
