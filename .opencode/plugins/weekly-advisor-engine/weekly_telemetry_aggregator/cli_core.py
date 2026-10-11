"""Handlers et helpers « core » — extraits de `cli.py` (split cli, Task 6).

Quatorze noms : les helpers de façade/config (`_load_cfg`, `_out_dir`,
`_deduplicate_worker_statuses`, `_positive_int`) et les handlers non rattachés à
un cluster (show-session, audit/draft-candidates, releases, insights, harness,
harness-remediate, commit-draft, doctor, self-cost). Déplacés verbatim.

`_cmd_run` reste volontairement dans `cli.py` (invariant 3 du plan) : `build_parser`
fait `globals()[func.__name__]` et `tests/test_cli.py` patche `cli.run` puis appelle
`_cmd_run`, dont la résolution doit rester dans le namespace de la façade.

`_load_previous_carried_over` appartient au cluster veille (`cli_watch`, Task 4) :
`_cmd_audit_candidates` le consomme, donc l'arête `cli_core -> cli_watch` est
descendante et acyclique. En regard, `cli_watch` garde sa copie locale de
`_out_dir` : lui faire importer `cli_core` créerait le cycle
`cli_core -> cli_watch -> cli_core` que refuse `tests/test_import_graph.py`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .cli_watch import _load_previous_carried_over
from .config import load_config
from .costing import self_cost
from .doctor import doctor


def _load_cfg(args) -> object:
    cfg = load_config(args.config)
    if getattr(args, "output_dir", None):
        cfg.output_dir = Path(args.output_dir).expanduser()
    return cfg


# ------------------------------------------------------------------ command handlers


def _out_dir(cfg, date: str) -> Path:
    """Active run directory for a dated command (v6.0.k F1); legacy root fallback."""
    from .run_state import resolve_active_run_dir

    return resolve_active_run_dir(cfg.output_dir, date)


_COMPACT_WORKER_FIELDS = ("session_id", "rc", "truncated", "worker_status", "status")
_WORKER_TRIGGER_FIELDS = ("rc", "truncated", "worker_status")


def _compact_worker_record(record: object) -> tuple[str, dict] | None:
    """Return ``(session_key, compact)`` for a usable worker record, else None."""
    if not isinstance(record, dict):
        return None
    session_id = record.get("session_id")
    if not session_id or not any(key in record for key in _WORKER_TRIGGER_FIELDS):
        return None
    compact = {field: record[field] for field in _COMPACT_WORKER_FIELDS if field in record}
    return str(session_id), compact


def _merge_worker_rc(merged: dict, compact: dict) -> None:
    """Keep the worst ``rc``; an unparsable value counts as failure (rc=1)."""
    try:
        rc = int(compact["rc"])
    except (TypeError, ValueError):
        rc = 1
    merged["rc"] = max(int(merged.get("rc", 0) or 0), rc)


def _merge_worker_status_fields(merged: dict, compact: dict) -> None:
    """Merge ``worker_status``/``status``, never erasing a truncation."""
    for field in ("worker_status", "status"):
        if field in compact and compact[field]:
            # Truncation provenance is monotonic: a later healthy record
            # must not erase a previously observed truncation.
            if field == "worker_status" and merged.get(field) == "truncated":
                continue
            merged[field] = compact[field]


def _merge_worker_record(merged: dict, compact: dict) -> None:
    """Fold one compact record into ``merged``, never weakening a failure."""
    if "session_id" in compact:
        merged["session_id"] = compact["session_id"]
    if "rc" in compact:
        _merge_worker_rc(merged, compact)
    if "truncated" in compact:
        merged["truncated"] = bool(merged.get("truncated", False) or compact["truncated"])
    _merge_worker_status_fields(merged, compact)


def _deduplicate_worker_statuses(records: list[object]) -> list[dict]:
    """Return one compact worker outcome record per session."""
    by_session: dict[str, dict] = {}
    for record in records:
        item = _compact_worker_record(record)
        if item is None:
            continue
        key, compact = item
        _merge_worker_record(by_session.setdefault(key, {}), compact)
    return list(by_session.values())


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"doit être >= 1 (reçu {value})")
    return parsed


def _cmd_show_session(args, cfg) -> int:
    import warnings

    from .main import EXIT_TOTAL_FAILURE
    from .providers import build_providers
    from .providers.implementations.opencode import OpenCodeSessionProvider
    from .sqlite_reader import DataSourceError, detect_db
    from .transcript import MAX_EXTRACT_BYTES, render_session

    providers = build_providers(cfg)
    if not providers:
        # Repli historique : aucune source active → base OpenCode locale.
        warnings.warn(
            "aucune source de sessions active — repli sur la base OpenCode locale "
            f"({cfg.opencode_db_path})",
            stacklevel=2,
        )
        try:
            _path, adapter = detect_db(cfg.opencode_db_path)
        except DataSourceError as exc:
            print(f"show-session: FATAL: {exc} — lancer doctor", file=sys.stderr, flush=True)
            return EXIT_TOTAL_FAILURE
        providers = [OpenCodeSessionProvider(_path, adapter)]
    try:
        # render_session route vers le provider du harnais déduit de l'id canonique.
        # max_extract_bytes borne le texte rendu (évite l'OOM exit=137 sur sessions
        # géantes) ; --extract-dir et le stdout sont préservés.
        text = render_session(
            providers,
            args.session_id,
            include_children=args.include_children,
            max_extract_bytes=MAX_EXTRACT_BYTES,
        )
        sys.stdout.write(text)
        if args.extract_dir:
            target = Path(args.extract_dir) / f"transcript-extract-{args.session_id}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            print(f"\nshow-session: extract écrit -> {target}", flush=True)
    finally:
        for provider in providers:
            provider.close()
    return 0


def _cmd_audit_candidates(args, cfg) -> int:
    """Partie 3 §2 — sélection déterministe des sessions à auditer (archive JSON)."""
    from .candidates import (
        prepend_carried_over,
        select_audit_candidates,
        split_audit_candidates,
    )
    from .main import EXIT_TOTAL_FAILURE, _parse_anchor
    from .writer import write_json_atomic

    run_time = _parse_anchor(args.anchor)
    date = run_time.strftime("%Y-%m-%d")
    out = _out_dir(cfg, date)
    summary_path = out / f"weekly-summary-{date}.json"
    if not summary_path.is_file():
        print(
            f"audit-candidates: summary inexistante {summary_path} — lancer run d'abord",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_TOTAL_FAILURE
    import json as _json

    summary = _json.loads(summary_path.read_text(encoding="utf-8"))
    candidates = select_audit_candidates(
        summary,
        top_sessions_limit=cfg.top_sessions_limit,
        cost_per_active_minute_min=cfg.audit.cost_per_active_minute_min,
        cache_efficiency_gap=cfg.audit.cache_efficiency_gap,
    )
    candidates = prepend_carried_over(
        candidates, _load_previous_carried_over(cfg.output_dir, out, date)
    )
    audited, carried_over = split_audit_candidates(candidates, cfg.audit_max_sessions)
    unaudited = carried_over  # alias legacy : mêmes sessions, intention reprise explicite
    data = {
        "schema_version": 1,
        "date": date,
        "audited": audited,
        "unaudited": unaudited,
        "carried_over": carried_over,
        "limit": cfg.audit_max_sessions,
        # Preserve worker outcomes even when candidates are bounded or output
        # was compacted by the orchestrator.
        "worker_statuses": _deduplicate_worker_statuses(
            [
                *(summary.get("worker_statuses") or []),
                *((summary.get("selection") or {}).get("worker_statuses") or []),
                *candidates,
            ]
        ),
    }
    out_path = out / f"weekly-audit-candidates-{date}.json"
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
    out = _out_dir(cfg, date)
    findings_path = out / f"weekly-quality-findings-{date}.json"
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
    out_path = out / f"weekly-draft-candidates-{date}.json"
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

    data, rc = releases_run(cfg, anchor=args.anchor, lookback_days=args.lookback_days)
    run_time = data["generated_at"]
    out = _out_dir(cfg, run_time[:10])
    out_path = out / f"weekly-ecosystem-{run_time[:10]}.json"
    write_json_atomic(out_path, data)
    print(
        f"releases: new_items={len(data['new_items'])} core_changes={len(data['core_changes'])} file={out_path}",
        flush=True,
    )
    return rc


def _cmd_insights(args, cfg) -> int:
    from .insights import run as insights_run

    return insights_run(cfg, anchor=args.anchor, baseline_summary_path=args.baseline_summary)


def _cmd_harness(args, cfg) -> int:
    from .main import harness

    return harness(cfg, anchor=args.anchor)


def _cmd_harness_remediate(args, cfg) -> int:
    from .harness_remediation import run as remediation_run

    return remediation_run(
        cfg,
        proposal_path=Path(args.proposal),
        mode=args.mode,
        anchor=args.anchor,
    )


def _cmd_commit_draft(args, cfg) -> int:
    from .safe_git_write import commit_draft

    ok, msg = commit_draft(cfg, Path(args.file), args.kind)
    print(f"commit-draft: {'OK' if ok else 'KO'}: {msg}", flush=True)
    return 0 if ok else 1


def _cmd_doctor(args, cfg) -> int:
    return doctor(cfg, config_loaded=getattr(args, "config", None) is not None)


def _cmd_self_cost(args, cfg) -> int:
    return self_cost(cfg, anchor=args.anchor)
