"""Handlers de rapport — extraits de `cli.py` (split cli, Task 5).

Quatre handlers : `_cmd_report_prep`, `_cmd_report_assemble`,
`_cmd_report_blocks_draft`, `_cmd_report_blocks_check`. Déplacés verbatim.

Le trio d'auto-load (`_read_json_arg` / `_auto_load` / `_auto_load_catalog`) n'est
PAS repris ici : il est consommé par le seul `_cmd_skill_curate` et a migré avec
lui dans `cli_skill_curate`. `cli.py` continue de le ré-exporter depuis ce module.

`_out_dir` vient de `cli_core` (arête descendante `cli_report -> cli_core`,
acyclique : `cli_core` n'importe jamais `cli_report`).
"""

from __future__ import annotations

import json
import sys

from .cli_core import _out_dir


def _cmd_report_prep(args, cfg) -> int:
    from .main import EXIT_TOTAL_FAILURE, _parse_anchor
    from .report import (
        _audit_artifact_declarations,
        _branch_applicability,
        applicable_summary_rc,
        report_prep,
        validate_required_artifacts,
    )

    path, ctx = report_prep(cfg, anchor=args.anchor)
    if ctx is None:
        print(
            "report-prep: FATAL: aucune donnée (summary absente) — lancer run d'abord",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_TOTAL_FAILURE
    print(f"report-prep: draft {path}", flush=True)
    run_time = _parse_anchor(args.anchor)
    date = run_time.strftime("%Y-%m-%d")
    out = _out_dir(cfg, date)
    timings: object = None
    timings_path = out / f"weekly-timings-{date}.json"
    if timings_path.is_file():
        try:
            timings = json.loads(timings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            timings = None
    if (
        validate_required_artifacts(
            out,
            date,
            applicability=_branch_applicability(timings, date, out=out),
            dynamic_audit_artifacts=_audit_artifact_declarations(timings),
        )["status"]
        != "pass"
    ):
        return EXIT_TOTAL_FAILURE
    summary_path = out / f"weekly-summary-{date}.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return EXIT_TOTAL_FAILURE
    return applicable_summary_rc(
        summary,
        out=out,
        date=date,
        project_root=cfg.project_root,
        fallback_rc=0,
    )


def _cmd_report_assemble(args, cfg) -> int:
    from .report import report_assemble

    path, warnings, rc = report_assemble(cfg, anchor=args.anchor)
    if path is None:
        for warning in warnings:
            print(f"report-assemble: FATAL: {warning}", file=sys.stderr, flush=True)
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


def _cmd_report_blocks_check(args, cfg) -> int:
    """Valide `weekly-report-blocks-<date>.md` SANS le consommer (étape 7b.1).

    Contrepartie de `report-assemble` : l'assemble SUPPRIME le draft à chaque
    réussite, donc corriger un bloc rejeté impose un `report-prep` complet —
    le coût observé sur le run 2026-10-01 (2 assemble ratés, 1 prep de plus).
    Ce check applique le MÊME validateur que l'assemble
    (`report.validate_llm_blocks`) et rend les violations numérotées, sans
    écrire ni supprimer quoi que ce soit : le fichier reste assemblable tel quel.

    rc : 0 conforme ; 1 non conforme (violations listées, bloc rejetable par
    l'assemble) ; 2 prose absente/illisible (rien à valider).
    """
    from .main import EXIT_OK, EXIT_PARTIAL, EXIT_TOTAL_FAILURE, _parse_anchor
    from .report import validate_llm_blocks
    from .util import load_json, read_text

    date = _parse_anchor(args.anchor).strftime("%Y-%m-%d")
    out = _out_dir(cfg, date)
    blocks = out / f"weekly-report-blocks-{date}.md"
    text = read_text(blocks)
    if text is None:
        print(
            f"report-blocks-check: FATAL: prose absente ou illisible : {blocks}",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_TOTAL_FAILURE

    findings = load_json(out / f"weekly-quality-findings-{date}.json")
    insights = load_json(out / f"weekly-insights-{date}.json")
    violations, coverage = validate_llm_blocks(text, findings, insights)
    n_lines = len(text.splitlines())
    n_words = len(text.split())
    # `validate_llm_blocks` ne couvre pas le plancher de mots : c'est
    # `_assemble_quality_block` qui rejette en dessous. Même seuil, même calcul
    # (`len(text.split())`), sinon le check validerait un bloc que l'assemble
    # rejetterait quand même — exactement le faux vert qu'on veut supprimer.
    if n_words < cfg.blocks_min_words:
        violations = [
            *violations,
            f"bloc trop court ({n_words} mots < {cfg.blocks_min_words}) — "
            f"détailler les constats, ne pas résumer",
        ]

    for warning in coverage:
        print(f"report-blocks-check: WARNING: {warning}", flush=True)
    if violations:
        print(
            f"report-blocks-check: NON CONFORME — {len(violations)} violation(s), "
            f"{n_lines} lignes, {n_words} mots — {blocks} (NON consommé) :",
            flush=True,
        )
        for index, violation in enumerate(violations, start=1):
            print(f"  {index}. {violation}", flush=True)
        return EXIT_PARTIAL
    print(
        f"report-blocks-check: CONFORME — {blocks} ({n_lines} lignes, {n_words} mots) — "
        "fichier NON consommé, assemblable tel quel",
        flush=True,
    )
    return EXIT_OK
