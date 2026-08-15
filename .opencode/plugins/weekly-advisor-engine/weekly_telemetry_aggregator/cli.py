"""CLI entry point for weekly-telemetry-aggregator (Part 0 §3, Part 1 §5).

Subcommands: run (default), show-session, releases, insights,
report-prep, report-assemble, commit-draft, doctor, self-cost.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .main import doctor, run, self_cost


def _load_cfg(args) -> object:

    cfg = load_config(args.config)
    if getattr(args, "output_dir", None):
        cfg.output_dir = Path(args.output_dir).expanduser()
    return cfg


# ------------------------------------------------------------------ command handlers


def _cmd_run(args, cfg) -> int:
    return run(
        cfg,
        anchor=args.anchor,
        force=args.force,
        top_sessions_limit=args.top_sessions_limit,
        include_subagents=args.include_subagents,
        fail_on_missing_telemetry=args.fail_on_missing_telemetry,
    )


def _cmd_show_session(args, cfg) -> int:
    from .main import EXIT_TOTAL_FAILURE
    from .sqlite_reader import DataSourceError, detect_db
    from .transcript import render_session

    try:
        _path, adapter = detect_db(cfg.opencode_db_path)
    except DataSourceError as exc:
        print(f"show-session: FATAL: {exc} — lancer doctor", file=sys.stderr, flush=True)
        return EXIT_TOTAL_FAILURE
    try:
        text = render_session(adapter, args.session_id, include_children=args.include_children)
        sys.stdout.write(text)
        if args.extract_dir:
            target = Path(args.extract_dir) / f"transcript-extract-{args.session_id}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            print(f"\nshow-session: extract écrit -> {target}", flush=True)
    finally:
        adapter.conn.close()
    return 0


def _cmd_audit_candidates(args, cfg) -> int:
    """Partie 3 §2 — sélection déterministe des sessions à auditer (archive JSON)."""
    from .candidates import select_audit_candidates
    from .main import _parse_anchor
    from .writer import write_json_atomic

    run_time = _parse_anchor(args.anchor)
    date = run_time.strftime("%Y-%m-%d")
    summary_path = cfg.output_dir / f"weekly-summary-{date}.json"
    if not summary_path.is_file():
        print(
            f"audit-candidates: summary inexistante {summary_path} — lancer run d'abord",
            file=sys.stderr,
            flush=True,
        )
        return 2
    import json as _json

    summary = _json.loads(summary_path.read_text(encoding="utf-8"))
    candidates = select_audit_candidates(
        summary,
        top_sessions_limit=cfg.top_sessions_limit,
        cost_per_active_minute_min=cfg.audit.cost_per_active_minute_min,
        cache_efficiency_gap=cfg.audit.cache_efficiency_gap,
    )
    audited = candidates[: max(0, cfg.audit_max_sessions)]
    unaudited = candidates[max(0, cfg.audit_max_sessions) :]
    data = {
        "schema_version": 1,
        "date": date,
        "audited": audited,
        "unaudited": unaudited,
        "limit": cfg.audit_max_sessions,
    }
    out_path = cfg.output_dir / f"weekly-audit-candidates-{date}.json"
    write_json_atomic(out_path, data)
    print(
        f"audit-candidates: {len(candidates)} candidats ({len(audited)} audités / "
        f"{len(unaudited)} non traités) file={out_path}",
        flush=True,
    )
    return 0


def _cmd_draft_candidates(args, cfg) -> int:
    """Partie 4 §3 — candidats skill/command à drafter depuis les findings (archive)."""
    from .candidates import select_draft_candidates
    from .main import _parse_anchor
    from .writer import write_json_atomic

    run_time = _parse_anchor(args.anchor)
    date = run_time.strftime("%Y-%m-%d")
    findings_path = cfg.output_dir / f"weekly-quality-findings-{date}.json"
    findings = None
    if findings_path.is_file():
        import json as _json

        findings = _json.loads(findings_path.read_text(encoding="utf-8"))
    candidates = select_draft_candidates(findings, max_candidates=cfg.max_candidates_per_run)
    data = {
        "schema_version": 1,
        "date": date,
        "candidates": candidates,
        "limit": cfg.max_candidates_per_run,
        "findings_file": findings_path.name if findings is not None else None,
    }
    out_path = cfg.output_dir / f"weekly-draft-candidates-{date}.json"
    write_json_atomic(out_path, data)
    print(
        f"draft-candidates: {len(candidates)} candidat(s) (plafond {cfg.max_candidates_per_run}) "
        f"file={out_path}",
        flush=True,
    )
    return 0


def _cmd_releases(args, cfg) -> int:
    from .releases import run as releases_run
    from .writer import write_json_atomic

    data, rc = releases_run(cfg, anchor=args.anchor)
    run_time = data["generated_at"]
    out_path = cfg.output_dir / f"weekly-ecosystem-{run_time[:10]}.json"
    write_json_atomic(out_path, data)
    print(
        f"releases: new_items={len(data['new_items'])} core_changes={len(data['core_changes'])} file={out_path}",
        flush=True,
    )
    return rc


def _cmd_insights(args, cfg) -> int:
    from .insights import run as insights_run

    return insights_run(cfg, anchor=args.anchor, baseline_summary_path=args.baseline_summary)


def _cmd_report_prep(args, cfg) -> int:
    from .main import EXIT_OK, EXIT_TOTAL_FAILURE
    from .report import report_prep

    path, ctx = report_prep(cfg, anchor=args.anchor)
    if ctx is None:
        print(
            "report-prep: FATAL: aucune donnée (summary absente) — lancer run d'abord",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_TOTAL_FAILURE
    print(f"report-prep: draft {path}", flush=True)
    return EXIT_OK


def _cmd_report_assemble(args, cfg) -> int:
    from .report import report_assemble

    path, warnings, rc = report_assemble(cfg, anchor=args.anchor)
    if path is None:
        return rc
    for w in warnings:
        print(f"report-assemble: WARNING: {w}", flush=True)
    print(f"report-assemble: {path}", flush=True)
    return rc


def _cmd_report_blocks_draft(args, cfg) -> int:
    from .report import report_blocks_draft

    path, warnings, rc = report_blocks_draft(cfg, anchor=args.anchor)
    if path is None:
        return rc
    for w in warnings:
        print(f"report-blocks-draft: WARNING: {w}", flush=True)
    print(f"report-blocks-draft: {path}", flush=True)
    return rc


def _cmd_harness(args, cfg) -> int:
    from .main import harness

    return harness(cfg, anchor=args.anchor)


def _cmd_commit_draft(args, cfg) -> int:
    from .safe_git_write import commit_draft

    ok, msg = commit_draft(cfg, Path(args.file), args.kind)
    print(f"commit-draft: {'OK' if ok else 'KO'}: {msg}", flush=True)
    return 0 if ok else 1


def _cmd_doctor(args, cfg) -> int:
    return doctor(cfg, config_loaded=getattr(args, "config", None) is not None)


def _cmd_self_cost(args, cfg) -> int:
    return self_cost(cfg, anchor=args.anchor)


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weekly-telemetry-aggregator",
        description="Pipeline opencode-weekly-advisor v5.31 — télémétrie, veille, insights, cohérence, rapport.",
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

    # Global flags are available both BEFORE the subcommand (parser) and AFTER
    # (each subparser reuses parent), so `run --anchor X` == `--anchor X run`.
    global_parent = argparse.ArgumentParser(add_help=False)
    # default=SUPPRESS: if a subcommand omits the flag, argparse does NOT reset it
    # to None, so a value parsed on the main parser (before the subcommand) is kept.
    global_parent.add_argument("--config", help=argparse.SUPPRESS, default=argparse.SUPPRESS)
    global_parent.add_argument("--anchor", help=argparse.SUPPRESS, default=argparse.SUPPRESS)
    global_parent.add_argument("--output-dir", help=argparse.SUPPRESS, default=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    # run is the default subcommand (spec Partie 0 §3). Default all its flags so a
    # bare invocation resolves every attribute _cmd_run reads from the namespace.
    parser.set_defaults(
        command="run",
        func=_cmd_run,
        force=False,
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

    p_run = sub.add_parser(
        "run",
        parents=[global_parent],
        help="Aggregate OpenCode telemetry into weekly-summary-<date>.json (default)",
    )
    p_run.add_argument(
        "--force", action="store_true", help="Overwrite an existing weekly-summary-<date>.json"
    )
    p_run.add_argument("--top-sessions-limit", type=int, help="Override top_sessions_limit")
    p_run.add_argument(
        "--include-subagents", dest="include_subagents", action="store_true", default=None
    )
    p_run.add_argument("--no-subagents", dest="include_subagents", action="store_false")
    p_run.add_argument(
        "--fail-on-missing-telemetry",
        action="store_true",
        help="Exit 1 as soon as a session read fails",
    )
    p_run.set_defaults(func=_cmd_run)

    p_show = sub.add_parser(
        "show-session", parents=[global_parent], help="Render a session transcript (Partie 0 §3)"
    )
    p_show.add_argument("session_id")
    p_show.add_argument(
        "--include-children",
        action="store_true",
        help="Include child sessions (subagents) via parent_id",
    )
    p_show.add_argument(
        "--extract-dir",
        help="Also write transcript-extract-<session_id>.md into this directory (Partie 3 §6.b)",
    )
    p_show.set_defaults(func=_cmd_show_session)

    p_rel = sub.add_parser(
        "releases", parents=[global_parent], help="Ecosystem watch + core changes (Partie 2)"
    )
    p_rel.set_defaults(func=_cmd_releases)

    p_ins = sub.add_parser(
        "insights",
        parents=[global_parent],
        help="Deltas, alerts, maintenance rules R1-R4 (Partie 6)",
    )
    p_ins.add_argument(
        "--baseline-summary",
        help="Previous summary used when no prior run exists (P1.1, v5.28)",
    )
    p_ins.set_defaults(func=_cmd_insights)

    p_prep = sub.add_parser(
        "report-prep",
        parents=[global_parent],
        help="Render deterministic report sections (Partie 7a)",
    )
    p_prep.set_defaults(func=_cmd_report_prep)

    p_asm = sub.add_parser(
        "report-assemble",
        parents=[global_parent],
        help="Inject LLM blocks into the draft (Partie 7c)",
    )
    p_asm.set_defaults(func=_cmd_report_assemble)

    p_harness = sub.add_parser(
        "harness",
        parents=[global_parent],
        help="Step 5: harness-eval harness-lint → weekly-harness-digest-<date>.json (v5.28)",
    )
    p_harness.set_defaults(func=_cmd_harness)

    p_blocks = sub.add_parser(
        "report-blocks-draft",
        parents=[global_parent],
        help="Deterministic section-4 blocks draft (v5.28, P5.1)",
    )
    p_blocks.set_defaults(func=_cmd_report_blocks_draft)

    p_ac = sub.add_parser(
        "audit-candidates",
        parents=[global_parent],
        help="Partie 3 §2: deterministic audit-candidate selection from weekly-summary",
    )
    p_ac.set_defaults(func=_cmd_audit_candidates)

    p_dc = sub.add_parser(
        "draft-candidates",
        parents=[global_parent],
        help="Partie 4 §3: skill/command-candidate findings, capped, severity DESC",
    )
    p_dc.set_defaults(func=_cmd_draft_candidates)

    p_cd = sub.add_parser(
        "commit-draft",
        parents=[global_parent],
        help="Validate + commit an auto-drafted skill/command (Partie 4 §7)",
    )
    p_cd.add_argument("--file", required=True, help="Absolute path to the SKILL.md or command .md")
    p_cd.add_argument("--kind", choices=("skill", "command", "fix"), required=True)
    p_cd.set_defaults(func=_cmd_commit_draft)

    p_doctor = sub.add_parser(
        "doctor", parents=[global_parent], help="Diagnose the installation (Partie 1 §12)"
    )
    p_doctor.set_defaults(func=_cmd_doctor)

    p_self = sub.add_parser(
        "self-cost",
        parents=[global_parent],
        help="Cost of the pipeline's own run session (Partie 1 §12)",
    )
    p_self.set_defaults(func=_cmd_self_cost)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = _load_cfg(args)
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
