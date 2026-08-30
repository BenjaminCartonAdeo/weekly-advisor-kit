"""CLI entry point for weekly-telemetry-aggregator (Part 0 §3, Part 1 §5).

Subcommands: run (default), show-session, releases, watch-context, watch-distill,
watch-validate, insights, report-prep, report-assemble, harness, harness-remediate,
audit-candidates, draft-candidates, commit-draft, doctor, self-cost, skill-curate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from . import __version__
from .config import load_config
from .costing import self_cost
from .curation import (
    _skill_fields,
    build_catalog_from_skills,
    catalog_entry_is_complete,
    decide_actions,
    normalize_curation_findings,
    read_carry,
    select_catalog_entry,
    ttl_archive_candidates,
)
from .main import doctor, run


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


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"doit être >= 1 (reçu {value})")
    return parsed


def _cmd_run(args, cfg) -> int:
    return run(
        cfg,
        anchor=args.anchor,
        top_sessions_limit=args.top_sessions_limit,
        include_subagents=args.include_subagents,
        fail_on_missing_telemetry=args.fail_on_missing_telemetry,
        lookback_days=args.lookback_days,
    )


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
    from .candidates import select_audit_candidates
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
    audited = candidates[: max(0, cfg.audit_max_sessions)]
    unaudited = candidates[max(0, cfg.audit_max_sessions) :]
    data = {
        "schema_version": 1,
        "date": date,
        "audited": audited,
        "unaudited": unaudited,
        "limit": cfg.audit_max_sessions,
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
        build_local_inventory,
        build_watch_context,
        enrich_candidates,
        load_ecosystem_report,
    )
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


def _read_json_arg(path) -> object | None:
    """Lit un fichier JSON fourni en argument CLI; None si non fourni."""
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"fichier introuvable: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _auto_load(out: Path, output_dir: Path, date: str, stem: str) -> dict | None:
    """Charge un artefact de run par date, avec repli legacy (racine output_dir).

    Cherche ``<out>/<stem>-<date>.json`` puis un glob ``<stem>-*.json`` dans
    ``out`` (run actif), enfin dans ``output_dir`` racine (mode legacy). Retourne
    le dict ou ``None`` si introuvable. Ne lève jamais.
    """
    primary = out / f"{stem}-{date}.json"
    if primary.is_file():
        return _read_json_arg(str(primary))
    candidates = sorted(out.glob(f"{stem}-*.json"), reverse=True)
    if not candidates:
        candidates = sorted(Path(output_dir).glob(f"{stem}-*.json"), reverse=True)
    for path in candidates:
        data = _read_json_arg(str(path))
        if isinstance(data, dict):
            return data
    return None


def _auto_load_catalog(out: Path, cfg, date: str) -> list[dict]:
    """Catalogue de skills pour l'auto-load (Phase 1), sans piping LLM.

    Priorité : ``--catalog`` (override, géré par l'appelant) → ``weekly-summary``
    du run (champ ``skill_catalog`` si présent, sinon ``skill_catalog_entries``
    mappés) → scan disque autorité (``build_catalog_from_skills``) qui lit
    ``metadata.origin``/``ttl_policy``/``usage``. Retourne [] si tout absent.
    """
    summary = _auto_load(out, cfg.output_dir, date, "weekly-summary")
    if isinstance(summary, dict):
        catalog = summary.get("skill_catalog")
        if isinstance(catalog, list) and catalog:
            return [entry for entry in catalog if isinstance(entry, dict)]
        entries = summary.get("skill_catalog_entries")
        if isinstance(entries, list) and entries:
            catalog: list[dict] = []
            for raw_entry in entries:
                if not isinstance(raw_entry, dict):
                    continue
                entry = dict(raw_entry)
                raw_metadata = entry.get("metadata")
                metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
                # ``skill_catalog_entries`` is serialized from a dataclass in
                # older summaries, while newer producers may put lifecycle
                # fields at either level.  Preserve every protection field
                # instead of replacing metadata with an empty mapping.
                for key in (
                    "skill_id",
                    "origin",
                    "ttl_policy",
                    "usage",
                    "last_verified_at",
                    "verification",
                ):
                    if key not in entry:
                        continue
                    value = entry[key]
                    current = metadata.get(key)
                    # Lifecycle protection is monotonic when producers disagree:
                    # user/pin may never be weakened by a nested/flattened row.
                    if (
                        (key == "origin" and str(value).strip().casefold() == "user")
                        or (key == "ttl_policy" and str(value).strip().casefold() == "pin")
                        or key not in metadata
                        or current in (None, "")
                    ):
                        metadata[key] = value
                skill_id = entry.get("skill_id") or entry.get("name") or metadata.get("skill_id")
                if not isinstance(skill_id, str) or not skill_id.strip():
                    continue
                entry["skill_id"] = skill_id.strip()
                entry["metadata"] = metadata
                catalog.append(entry)
            if catalog:
                return catalog
    return build_catalog_from_skills(cfg.project_root)


def _skill_dirs_for(cfg) -> list[Path]:
    """Project-local skill roots eligible for future apply moves.

    ``main._skill_dirs`` also returns the global OpenCode skills directory for
    telemetry discovery.  Curation deliberately narrows that universe to the
    three project roots so ``--apply`` can never mutate global skills.
    """
    try:
        project_root = (cfg.project_root or Path.cwd()).expanduser().resolve()
    except (OSError, RuntimeError):
        # Apply must fail closed when even the project root cannot be resolved.
        return []
    roots = [
        project_root / ".opencode" / "skills",
        project_root / ".claude" / "skills",
        project_root / ".agents" / "skills",
    ]
    global_roots = _global_skill_roots()
    return [root for root in roots if not _path_is_global_or_unresolvable(root, global_roots)]


def _global_skill_roots() -> tuple[Path, ...]:
    """All supported user-level skill roots, resolved for symlink protection."""

    return (
        Path.home() / ".config" / "opencode" / "skills",
        Path.home() / ".claude" / "skills",
        Path.home() / ".agents" / "skills",
    )


def _path_is_safely_within(path: Path, root: Path) -> bool:
    """Return true only when containment is proven without an OS error."""
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _path_is_within(path: Path, root: Path) -> bool:
    """Containment predicate for deny-list checks; resolution errors deny access."""

    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError):
        return True
    except ValueError:
        return False
    return True


def _path_is_global_or_unresolvable(path: Path, global_roots: tuple[Path, ...]) -> bool:
    """Deny a path inside a global root, or one whose resolution is uncertain."""

    return any(_path_is_within(path, global_root) for global_root in global_roots)


def _normalize_skill_relative(skill_id: str) -> Path | None:
    """Normalize one relative skill id without changing its identity."""

    requested = str(skill_id or "").strip()
    if not requested or "\\" in requested or requested.startswith("/") or requested.startswith("~"):
        return None
    raw_parts = requested.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        return None
    normalized = Path(*raw_parts)
    if normalized.is_absolute() or normalized.parts != tuple(raw_parts):
        return None
    if "_archive" in normalized.parts:
        return None
    return normalized


def _archive_skill(
    skill_id: str,
    skills_dirs: list[Path],
    date: str,
    mover,
    *,
    catalog_entry: Mapping[str, object] | None = None,
) -> tuple[str, str | None]:
    """Déplace un skill (dir) vers ``<skills_dir>/_archive/<date>/<id>`` (idempotent).

    Retourne le statut de ``safe_git_move``. Aucun delete. Si introuvable sur
    disque, retourne ``("missing", None)`` (compté skipped, jamais fatale).
    """
    requested_path = _normalize_skill_relative(skill_id)
    if requested_path is None:
        return "missing", "skill_id absent or unsafe"
    if not isinstance(date, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) is None:
        return "missing", "date archive absente ou unsafe"
    global_roots = _global_skill_roots()
    candidates: list[tuple[Path, Path, Path]] = []
    for skills_dir in skills_dirs:
        if _path_is_global_or_unresolvable(skills_dir, global_roots):
            continue
        src = skills_dir / requested_path
        dst = skills_dir / "_archive" / date / requested_path
        try:
            # Check every resolved path before consulting exists/is_dir.  In
            # particular, an existing destination symlink must not short-circuit
            # the global-root and project-root containment gates.
            if any(
                _path_is_global_or_unresolvable(candidate, global_roots)
                for candidate in (src, src / "SKILL.md", dst, dst.parent)
            ):
                return "missing", "skill source/destination hors périmètre projet"
            if (
                not _path_is_safely_within(src, skills_dir)
                or not _path_is_safely_within(src / "SKILL.md", skills_dir)
                or not _path_is_safely_within(dst.parent, skills_dir)
            ):
                return "missing", "skill source/destination hors périmètre projet"
            destination_exists = dst.exists()
            source_exists = src.is_dir()
        except (OSError, RuntimeError) as exc:
            return "rejected", f"chemin source illisible: {exc}"
        if destination_exists or source_exists:
            candidates.append((skills_dir, src, dst))

    if not candidates:
        return "missing", None
    if len(candidates) > 1:
        return "ambiguous", "skill_id existe dans plusieurs racines projet"

    _skills_dir, src, dst = candidates[0]
    # A previous successful run leaves the source absent but destination present.
    # Report ``exists`` rather than ``missing`` and never attempt a second move.
    if dst.exists():
        return "exists", f"{dst} déjà présent (idempotent)"
    if not src.is_dir():
        return "missing", "skill source absent ou non-répertoire"
    catalog_sid, catalog_origin, catalog_ttl = _skill_fields(catalog_entry)
    if catalog_origin == "user" or catalog_ttl == "pin":
        return "protected", "catalogue protège cette skill (origin=user ou ttl_policy=pin)"
    if catalog_entry is None or not catalog_entry_is_complete(catalog_entry):
        # A name-only or absent catalogue entry is sufficient for a dry-run
        # proposal, never for an apply move.  Still inspect the source so a
        # stale proposal cannot hide user/pinned protection behind missing data.
        from .safe_git_write import validate_skill_source

        _valid, metadata, detail = validate_skill_source(src / "SKILL.md")
        if metadata.get("origin") == "user" or metadata.get("ttl_policy") == "pin":
            return "protected", detail
        return "unverified", "catalogue incomplet ou absent; archive apply refusé"
    # Re-read frontmatter on every apply. A stale catalogue must never weaken
    # source validation or protection metadata.
    from .safe_git_write import validate_skill_source

    valid, metadata, detail = validate_skill_source(src / "SKILL.md")
    if metadata.get("origin") == "user" or metadata.get("ttl_policy") == "pin":
        return "protected", detail
    if not valid:
        return "unverified", detail
    if (
        catalog_sid != skill_id
        or metadata.get("origin") != catalog_origin
        or metadata.get("ttl_policy") != catalog_ttl
    ):
        return "unverified", "catalogue et frontmatter source incohérents"
    # Revalidate immediately before the move: symlink/race changes after the
    # discovery checks must fail closed, including project-root containment.
    project_root = _skills_dir.parents[2]
    try:
        if any(
            _path_is_global_or_unresolvable(candidate, global_roots)
            or not _path_is_safely_within(candidate, project_root)
            for candidate in (
                _skills_dir,
                src,
                src / "SKILL.md",
                dst,
                dst.parent,
            )
        ):
            return "rejected", "skill source/destination hors périmètre projet"
        if not src.is_dir() or dst.exists():
            return "rejected", "état source/destination modifié avant déplacement"
    except (OSError, RuntimeError) as exc:
        return "rejected", f"chemin source illisible: {exc}"
    try:
        return mover(src, dst)
    except (OSError, ValueError, RuntimeError) as exc:
        return "rejected", str(exc)


def _cmd_skill_curate(args, cfg) -> int:
    """Curation/décroissance des skills (R4 curation/GC + R8 TTL).

    DRY-RUN par défaut (imprime les décisions, n'écrit rien, ne déplace rien).
    Auto-load des entrées (Phase 1) : sans ``--coherence``/``--catalog``, lit
    depuis le run actif (``runs/current`` ou legacy) — aucun piping LLM requis.
    ``--apply`` exécute les archives (move vers ``_archive/<date>/``, idempotent,
    jamais delete) et consigne un manifeste ; ``merge``/``reference``/``pin``/
    ``delete``/``recalibrate`` restent PROPOSITIONS (aucune op fs).
    ``origin='user'`` jamais touché.
    """

    from .run_state import resolve_active_run_dir
    from .safe_git_write import safe_git_move
    from .util import parse_anchor as _parse_anchor

    engine_dir = Path(__file__).resolve().parent
    apply = bool(getattr(args, "apply", False))
    stale_days = int(getattr(args, "stale_days", 90) or 90)

    # --- date + run dir (base de l'auto-load) ---
    run_time = _parse_anchor(getattr(args, "anchor", None))
    date = run_time.strftime("%Y-%m-%d")
    out = resolve_active_run_dir(cfg.output_dir, date)

    # --- Phase 1 : auto-discovery des entrées (override --coherence/--catalog) ---
    coherence_raw = _read_json_arg(getattr(args, "coherence", None))
    if coherence_raw is None:
        coherence_raw = _auto_load(out, cfg.output_dir, date, "weekly-coherence-findings")
    # Normalization is centralized in curation.py: structured R4 signals and
    # legacy comma-separated archive findings become individual records once.
    coherence = normalize_curation_findings(coherence_raw or [])

    catalog = _read_json_arg(getattr(args, "catalog", None))
    if catalog is None:
        catalog = _auto_load_catalog(out, cfg, date)
    catalog = catalog if isinstance(catalog, list) else []

    usage = _read_json_arg(getattr(args, "usage", None))
    runs_seen = int(getattr(args, "runs_seen", 0) or 0)

    # Carry inter-run: si usage non fourni, on lit le fallback de l'engine.
    if usage is None:
        watch_cfg = getattr(cfg, "watch_distill", None)
        configured_memory = getattr(watch_cfg, "memory_file", None)
        carry = read_carry(
            engine_dir,
            output_dir=cfg.output_dir,
            memory_file=configured_memory,
        )
        runs_seen = max(runs_seen, carry["runs_seen"])
        usage = [
            {
                "skill_id": sid,
                "origin": u.get("origin"),
                "ttl_policy": u.get("ttl_policy"),
                "usage": {
                    "last_loaded": u.get("last_loaded"),
                    "load_count": u.get("load_count", 0),
                },
            }
            for sid, u in carry.get("usage", {}).items()
        ]

    usage = usage if isinstance(usage, list) else []
    archive_ids = ttl_archive_candidates(usage, runs_seen, stale_days=stale_days)
    ttl_findings = [
        {
            "tag_action": "archive",
            "target_skill_id": sid,
            "reason": "TTL stale/zero-load",
            "_source": "ttl",
        }
        for sid in archive_ids
    ]
    decisions = decide_actions([*coherence, *ttl_findings], catalog)

    mode = "APPLY" if apply else "DRY-RUN"
    print(f"=== skill-curate ({mode}) ===", flush=True)
    print(f"  run_dir: {out}", flush=True)
    print(
        f"  coherence={len(coherence)} catalog={len(catalog)} "
        f"decisions={len(decisions)} ttl-archive={len(archive_ids)}",
        flush=True,
    )
    for d in decisions:
        print(f"  decision: {d['action']:>10} {d['target_skill_id']} — {d['reason']}", flush=True)
    print(
        f"  archive candidates ({len(archive_ids)}): {', '.join(archive_ids) or '<none>'}",
        flush=True,
    )

    # --- Phase 2 : dry-run or gated apply execution ---
    skills_dirs = _skill_dirs_for(cfg)
    applied = 0
    proposed = 0
    skipped = 0
    skipped_user = 0
    archive_pending = 0
    move_status_counts = {
        "moved": 0,
        "exists": 0,
        "missing": 0,
        "error": 0,
        "rejected": 0,
        "ambiguous": 0,
        "protected": 0,
        "unverified": 0,
        "not_attempted": 0,
    }
    manifest_decisions: list[dict] = []

    def _record(sid, action, reason, source, status, move_status):
        manifest_decisions.append(
            {
                "skill_id": sid,
                "target_skill_id": sid,
                "action": action,
                "reason": reason,
                "source": source,
                "status": status,
                "move_status": move_status,
            }
        )

    # One final decision per skill is guaranteed by decide_actions.  Archive
    # operations are the only filesystem mutation and only happen in apply mode.
    for d in decisions:
        sid = d["target_skill_id"]
        action = d["action"]
        reason = d["reason"]
        source = d.get("source") or "coherence"
        move_status = "not_attempted"
        move_status_counts[move_status] += 1
        if action == "skip":
            print(f"  SKIP: {sid} — {reason}", flush=True)
            skipped += 1
            if "user-origin" in reason:
                skipped_user += 1
            _record(sid, action, reason, source, "skipped", move_status)
            continue
        if action == "archive":
            if apply:
                catalog_entry = select_catalog_entry(catalog, sid)
                move_status, _ = _archive_skill(
                    sid,
                    skills_dirs,
                    date,
                    safe_git_move,
                    catalog_entry=catalog_entry,
                )
                move_status_counts["not_attempted"] -= 1
                move_status_counts.setdefault(move_status, 0)
                move_status_counts[move_status] += 1
                status = move_status
                if move_status in ("moved", "exists"):
                    applied += 1
                    print(f"  ARCHIVE: {sid} — {move_status}", flush=True)
                else:
                    skipped += 1
                    if move_status == "protected":
                        skipped_user += 1
                    print(f"  SKIP(archive {move_status}): {sid}", flush=True)
            else:
                archive_pending += 1
                status = "not_attempted"
                print(f"  PROPOSE: {sid} — archive (dry-run, aucune op fs)", flush=True)
            _record(sid, action, reason, source, status, move_status)
            continue
        # merge / reference / pin / delete / recalibrate / fix remain proposals;
        # no filesystem mutation is performed for them, even with --apply.
        proposed += 1
        print(f"  PROPOSE: {sid} — {action} (aucune op fs)", flush=True)
        _record(sid, action, reason, source, "proposed", move_status)

    manifest = {
        "schema_version": 1,
        "mode": "apply" if apply else "dry-run",
        "dry_run": not apply,
        "date": date,
        "run_dir": str(out),
        "coherence_count": len(coherence),
        "catalog_count": len(catalog),
        "applied": applied,
        "proposed": proposed,
        "skipped": skipped,
        "archive_pending": archive_pending,
        "decisions": manifest_decisions,
        "move_status_counts": move_status_counts,
        # Alias retained for report consumers that call these move counters.
        "move_counts": dict(move_status_counts),
        "archive_candidates_ttl": archive_ids,
        "archive_candidates": archive_ids,
        "skipped_user": skipped_user,
    }
    out_path = out / f"skill-curate-{date}.json"
    try:
        from .writer import write_json_atomic

        write_json_atomic(out_path, manifest)
        print(
            f"  manifest written: {out_path} "
            f"(applied={applied} proposed={proposed} skipped={skipped} "
            f"moves={move_status_counts})",
            flush=True,
        )
    except OSError as exc:
        print(f"  manifest write FAILED: {exc}", flush=True)
        return 1
    return 0


def _cmd_doctor(args, cfg) -> int:
    return doctor(cfg, config_loaded=getattr(args, "config", None) is not None)


def _cmd_self_cost(args, cfg) -> int:
    return self_cost(cfg, anchor=args.anchor)


# ------------------------------------------------------------------ parser


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

    p_run = sub.add_parser(
        "run",
        parents=[global_parent],
        help="Aggregate OpenCode telemetry into weekly-summary-<date>.json (default)",
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

    p_watch = sub.add_parser(
        "watch-context",
        parents=[global_parent],
        help="Join weekly-ecosystem with project plugins/skills/commands/agents",
    )
    p_watch.add_argument(
        "--ecosystem",
        help="Override the anchor-derived weekly-ecosystem-<date>.json input path",
    )
    p_watch.set_defaults(func=_cmd_watch_context)

    p_watch_distill = sub.add_parser(
        "watch-distill",
        parents=[global_parent],
        help="Deterministic ecosystem distill: fuse, screen, score, quota top-N fiches (étape 2.2)",
    )
    p_watch_distill.set_defaults(func=_cmd_watch_distill)

    p_watch_validate = sub.add_parser(
        "watch-validate",
        parents=[global_parent],
        help="Validate raw watch findings against the local dated watch context",
    )
    p_watch_validate.set_defaults(func=_cmd_watch_validate)

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
        help="Step 5: scoped harness-eval lint → weekly-harness-digest-<date>.json",
    )
    p_harness.set_defaults(func=_cmd_harness)

    p_harness_remediate = sub.add_parser(
        "harness-remediate",
        parents=[global_parent],
        help="Deterministic, gated harness proposal dry-run or remediation",
    )
    p_harness_remediate.add_argument(
        "--proposal", required=True, help="JSON proposal file under output_dir"
    )
    p_harness_remediate.add_argument(
        "--mode",
        choices=("dry-run", "apply"),
        default="dry-run",
        help="dry-run is the safe default; apply requires every gate",
    )
    p_harness_remediate.set_defaults(func=_cmd_harness_remediate)

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
        help="Validate + commit an auto-drafted skill/command/agent (Partie 4 §7)",
    )
    p_cd.add_argument(
        "--file", required=True, help="Absolute path to the SKILL.md, command or agent .md"
    )
    p_cd.add_argument("--kind", choices=("skill", "command", "fix", "agent"), required=True)
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

    p_sc = sub.add_parser(
        "skill-curate",
        parents=[global_parent],
        help="Curation/décroissance skills (R4 curation/GC + R8 TTL) — dry-run par défaut",
    )
    p_sc.add_argument("--coherence", help="JSON: findings de cohérence (tag_action pertinents)")
    p_sc.add_argument(
        "--catalog",
        help="JSON: catalogue de skills (skill_id, metadata.origin/ttl_policy)",
    )
    p_sc.add_argument(
        "--usage",
        help="JSON: usage_records pour TTL (fallback inter-run .watch-memory.jsonl si absent)",
    )
    p_sc.add_argument(
        "--runs-seen", type=int, default=0, help="Nombre de runs consécutifs observés"
    )
    p_sc.add_argument(
        "--stale-days", type=int, default=90, help="Seuil d'obsolescence last_loaded (jours)"
    )
    p_sc.add_argument(
        "--apply", action="store_true", help="Exécute (sinon dry-run, imprime seulement)"
    )
    p_sc.set_defaults(func=_cmd_skill_curate)

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
