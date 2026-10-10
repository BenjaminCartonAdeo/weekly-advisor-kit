"""Handlers de veille — extrait de `cli.py` (split cli, Task 4).

Quatre fonctions : `_load_previous_carried_over` (réutilisé par
`_cmd_audit_candidates`, resté dans `cli.py`) et les trois handlers watch.
Déplacés verbatim.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def _out_dir(cfg, date: str) -> Path:
    """Active run directory for a dated command (v6.0.k F1); legacy root fallback.

    Copie locale du helper de ``cli`` : le référencer — même paresseusement via
    ``from . import cli`` — créerait une arête ``cli_watch -> cli`` donc un cycle
    détecté par ``tests/test_import_graph.py`` (la façade ré-importe ce module).
    L'appel reste identique : seul l'import feuille ``.run_state`` est utilisé.
    Task 6 remplacera ceci par ``from .cli_core import _out_dir`` (arête
    descendante, acyclique).
    """
    from .run_state import resolve_active_run_dir

    return resolve_active_run_dir(cfg.output_dir, date)


def _audit_candidate_paths(root: Path) -> list[Path]:
    """Every on-disk ``weekly-audit-candidates-<date>.json`` a run may have left.

    La fonction d'origine inlinait ces trois globs; les nommer garde
    `_load_previous_carried_over` lisible sans changer la recherche.
    """
    return [
        *root.glob("weekly-audit-candidates-*.json"),
        *root.glob("runs/*/weekly-audit-candidates-*.json"),
        *root.glob("runs/*/legacy/weekly-audit-candidates-*.json"),
    ]


def _dated_candidates(paths: list[Path]) -> list[tuple[str, Path]]:
    """Pair every dated candidate with its ISO date, dropping undated names."""
    dated: list[tuple[str, Path]] = []
    for path in paths:
        match = re.search(r"(\d{4}-\d{2}-\d{2})\.json$", path.name)
        if match:
            dated.append((match.group(1), path))
    return dated


def _latest_before(dated: list[tuple[str, Path]], current_date: str) -> Path | None:
    """Most recent candidate strictly older than ``current_date``, else None."""
    earlier = [item for item in dated if item[0] < current_date]
    return sorted(earlier)[-1][1] if earlier else None


def _latest_outside(dated: list[tuple[str, Path]], exclude_dir: Path) -> Path | None:
    """Most recent candidate whose parent is not ``exclude_dir``, else None."""
    outside = [item for item in dated if item[1].parent != exclude_dir]
    return sorted(outside)[-1][1] if outside else None


def _carried_records(target: Path) -> list[dict]:
    """Read ``carried_over`` then ``unaudited`` from ``target``; [] on failure."""
    try:
        previous = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    carried = (previous or {}).get("carried_over") or (previous or {}).get("unaudited") or []
    return [c for c in carried if isinstance(c, dict) and c.get("session_id")]


def _load_previous_carried_over(
    output_dir: Path, exclude_dir: Path, current_date: str
) -> list[dict]:
    """`carried_over` du run précédent (reprise P2) — best-effort, [] sinon.

    Cherche le `weekly-audit-candidates-<date>.json` le plus récent strictement
    antérieur (racine, `runs/*/`, `runs/*/legacy/`), avec fallback même-date
    hors run courant (reruns/tests). Lit `carried_over` puis `unaudited`.
    """
    dated = _dated_candidates(_audit_candidate_paths(Path(output_dir)))
    target = _latest_before(dated, current_date) or _latest_outside(dated, exclude_dir)
    if target is None:
        return []
    return _carried_records(target)


def _cmd_watch_context(args, cfg) -> int:
    """Join the dated ecosystem report with the local project worktree.

    T6 : si le snapshot ``watch-candidates-<date>.json`` du run existe, le
    contexte est scoppé aux candidats + résiduels et le fichier fusionné
    ``watch-candidates-enriched-<date>.json`` (fiches × état local + hints)
    est écrit à côté. Snapshot absent/corrompu → flux legacy inchangé.
    """
    from .main import EXIT_OK, EXIT_TOTAL_FAILURE, _parse_anchor
    from .util import load_jsonc
    from .watch_context import (
        build_watch_context,
        enrich_candidates,
        load_ecosystem_report,
    )
    from .watch_inventory import build_local_inventory
    from .writer import write_json_atomic

    run_time = _parse_anchor(args.anchor)
    date = run_time.strftime("%Y-%m-%d")
    out = _out_dir(cfg, date)
    ecosystem_path = (
        Path(args.ecosystem).expanduser()
        if args.ecosystem
        else out / f"weekly-ecosystem-{date}.json"
    )
    ecosystem, error = load_ecosystem_report(ecosystem_path)
    if ecosystem is None:
        # v6.0.l (E6) : l'ordre des étapes est documenté (2 puis 2.5) ; si la
        # dépendance manque quand même, le message explique la reprise — exit 2
        # conservé car watch-validate/pipeline ont besoin du contexte produit ici.
        print(
            f"watch-context: DÉPENDANCE: {error} — exécuter weekly_releases d'abord, "
            "puis relancer watch-context (ordre d'étapes requis, pas une fatalité moteur)",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_TOTAL_FAILURE

    project_root = cfg.project_root or Path.cwd()
    candidates_path = out / f"watch-candidates-{date}.json"
    candidates = load_jsonc(candidates_path) if candidates_path.is_file() else None
    extra_keywords = tuple(getattr(cfg, "release_keywords", None) or ())
    context = build_watch_context(
        project_root,
        ecosystem,
        generated_at=run_time,
        ecosystem_path=ecosystem_path,
        candidates_path=candidates_path,
        extra_keywords=extra_keywords,
    )
    out_path = out / f"weekly-watch-context-{date}.json"
    write_json_atomic(out_path, context)
    enriched_note = ""
    if candidates is not None:
        inventory = build_local_inventory(project_root)
        enriched = enrich_candidates(
            candidates,
            context,
            ecosystem,
            inventory["items"],
            now=run_time,
            extra_keywords=extra_keywords,
        )
        if enriched is not None:
            enriched_path = out / f"watch-candidates-enriched-{date}.json"
            # indent par défaut (2) : fichier lu par l'agent en 3.5 — un JSON
            # mono-ligne est tronqué par le Read (2000 car/ligne) et a causé un
            # exit 2 injustifié le 2026-08-25 15:32.
            write_json_atomic(enriched_path, enriched)
            enriched_note = f" enriched={len(enriched['candidates'])}+{len(enriched['residual'])}"
            print(f"watch-context: enriched file={enriched_path}", flush=True)
        else:
            print(
                "watch-context: WARNING: watch-candidates illisible/invalide — "
                "pas de fichier enrichi, flux legacy conservé",
                file=sys.stderr,
                flush=True,
            )
    for warning in context["warnings"]:
        print(f"watch-context: WARNING: {warning}", flush=True)
    print(
        f"watch-context: items={len(context['market_matches'])} "
        f"declared_plugins={context['counts']['declared_plugins']}{enriched_note} "
        f"file={out_path}",
        flush=True,
    )
    return EXIT_OK


def _cmd_watch_distill(args, cfg) -> int:
    """Étape 2.2 — distill déterministe : fiches top-N + quotas + mémoire."""
    from .watch_distill import run as distill_run

    result, rc = distill_run(cfg, anchor=args.anchor)
    if rc != 0:
        # 1 = exception moteur, 2 = dépendance absente / étape désactivée :
        # dans les deux cas l'orchestration aval retombe sur le flux legacy
        # (relance sans 2.2) — jamais une fatalité du pipeline.
        reason = "; ".join(result.get("warnings") or []) or "raison inconnue"
        print(
            f"watch-distill: DÉGRADÉ (mode={result.get('mode')}): {reason}",
            file=sys.stderr,
            flush=True,
        )
        return rc
    for warning in result["warnings"]:
        print(f"watch-distill: WARNING: {warning}", flush=True)
    print(
        f"watch-distill: candidats={len(result['candidates'])} "
        f"annexe_securite={len(result['security_annex'])} "
        f"memoire_ignoree={result['dropped_memory']} "
        f"quotas={result['quotas_applied']}",
        flush=True,
    )
    return 0


def _cmd_watch_validate(args, cfg) -> int:
    """Validate raw watch findings against the anchor-dated local context.

    v7 : câble les entrées optionnelles du validateur — snapshot
    ``watch-candidates-<date>.json`` du run (annexe sécurité + fiches
    suspicious), mémoire inter-run (même fichier que le distill,
    ``watch_distill.memory_file`` relatif à ``output_dir``) et racine projet
    (coercition des cibles locales hors inventaire). Snapshot absent →
    validation legacy sans annexe, jamais une erreur.
    """
    from .main import EXIT_OK, EXIT_PARTIAL, EXIT_TOTAL_FAILURE, _parse_anchor
    from .watch_distill import DEFAULT_MEMORY_FILE
    from .watch_validation import (
        load_raw_findings,
        load_watch_context,
        validate_findings,
    )
    from .writer import write_json_atomic

    run_time = _parse_anchor(args.anchor)
    date = run_time.strftime("%Y-%m-%d")
    out = _out_dir(cfg, date)
    context_path = out / f"weekly-watch-context-{date}.json"
    raw_path = out / f"weekly-watch-findings-raw-{date}.json"

    wd_cfg = getattr(cfg, "watch_distill", None)
    memory_file = Path(getattr(wd_cfg, "memory_file", None) or DEFAULT_MEMORY_FILE)
    memory_path = memory_file if memory_file.is_absolute() else Path(cfg.output_dir) / memory_file
    candidates_path = out / f"watch-candidates-{date}.json"
    project_root = cfg.project_root or Path.cwd()

    context, context_error = load_watch_context(context_path)
    if context is None:
        # v6.0.l (E6) : contexte absent → validation dégradée (exit 1), jamais
        # FATAL — l'écosystème est une entrée optionnelle du run (une panne
        # releases/watch-context ne doit pas tuer le rapport).
        print(
            f"watch-validate: WARNING: {context_error} — validation dégradée "
            "(contexte absent, recommandations non vérifiées)",
            file=sys.stderr,
            flush=True,
        )
    raw_findings, raw_error = load_raw_findings(raw_path)
    if raw_findings is None:
        print(f"watch-validate: FATAL: {raw_error}", file=sys.stderr, flush=True)
        return EXIT_TOTAL_FAILURE

    result = validate_findings(
        raw_findings,
        context,
        date=date,
        memory_path=memory_path,
        candidates_path=candidates_path,
        project_root=project_root,
    )
    out_path = out / f"weekly-watch-findings-{date}.json"
    write_json_atomic(out_path, result)
    counts = result["validation"]["counts"]
    annex = result.get("security_annex")
    annex_note = f" bloques={annex['blocked_count']}" if isinstance(annex, dict) else ""
    print(
        f"watch-validate: accepted={counts['accepted']} rejected={counts['rejected']} "
        f"downgraded={counts['downgraded']}{annex_note} file={out_path}",
        flush=True,
    )
    return EXIT_PARTIAL if counts["rejected"] else EXIT_OK
