"""Curation/décroissance des skills — extrait de `cli.py` (split cli, Task 3).

Cluster skill-curation : helpers de chemins (`_skill_dirs_for`,
`_global_skill_roots`, `_path_within`, `_path_is_global_or_unresolvable`,
`_normalize_skill_relative`), l'archiveur `_archive_skill` et le handler
`_cmd_skill_curate`. Déplacés verbatim — la décomposition (CCN E/D) est Task 8.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from .curation import (
    _skill_fields,
    build_catalog_from_skills,
    catalog_entry_is_complete,
    decide_actions,
    manifest_metadata,
    normalize_curation_findings,
    read_carry,
    select_catalog_entry,
    ttl_archive_candidates,
)
from .draft_targets import resolve_draft_targets
from .main import _run_provenance
from .skill_surface import resolve_skill_surface


# ------------------------------------------------------------------ auto-load helpers
#
# `_read_json_arg` / `_auto_load` / `_auto_load_catalog` sont lus par le seul
# `_cmd_skill_curate` (Phase 1, auto-discovery des entrées). Ils vivaient dans
# `cli.py` ; ils migrent ici avec leur unique consommateur, ce qui garde le
# graphe d'imports acyclique : `cli` ré-importe ce module (façade), donc ce
# module ne peut pas, même paresseusement, ré-importer `cli`.
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


_CATALOG_LIFECYCLE_KEYS = (
    "skill_id",
    "origin",
    "ttl_policy",
    "usage",
    "last_verified_at",
    "verification",
)


def _merge_lifecycle_metadata(metadata: dict, entry: dict) -> None:
    """Copy lifecycle fields onto ``metadata``, never weakening protection.

    Lifecycle protection is monotonic when producers disagree: ``user``/``pin``
    may never be weakened by a nested/flattened row.
    """
    for key in _CATALOG_LIFECYCLE_KEYS:
        if key not in entry:
            continue
        value = entry[key]
        current = metadata.get(key)
        if (
            (key == "origin" and str(value).strip().casefold() == "user")
            or (key == "ttl_policy" and str(value).strip().casefold() == "pin")
            or key not in metadata
            or current in (None, "")
        ):
            metadata[key] = value


def _normalize_catalog_entry(raw_entry: dict) -> dict | None:
    """Flatten one ``skill_catalog_entries`` row; None when it has no skill id."""
    entry = dict(raw_entry)
    raw_metadata = entry.get("metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    _merge_lifecycle_metadata(metadata, entry)
    skill_id = entry.get("skill_id") or entry.get("name") or metadata.get("skill_id")
    if not isinstance(skill_id, str) or not skill_id.strip():
        return None
    entry["skill_id"] = skill_id.strip()
    entry["metadata"] = metadata
    return entry


def _normalize_catalog_entries(entries: list) -> list[dict]:
    """Normalize every dict row of ``skill_catalog_entries``; drop the rest."""
    catalog: list[dict] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            continue
        normalized = _normalize_catalog_entry(raw_entry)
        if normalized is not None:
            catalog.append(normalized)
    return catalog


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
            normalized = _normalize_catalog_entries(entries)
            if normalized:
                return normalized
    return build_catalog_from_skills(cfg.project_root)


def _skill_dirs_for(cfg) -> list[Path]:
    """Project-local skill roots eligible for future apply moves.

    Source unique : `skill_surface.resolve_skill_surface`. Les racines apply sont les
    cibles skills des harnais **résolus** (table unique A1) — donc exactement la
    surface que le catalogue a lue. Un harnais non résolu n'est plus éligible :
    `.claude/skills` sous le défaut opencode, ou `.agents/skills` (qui n'est
    même pas une racine codex : codex projette `.agents`), ne contiennent aucun
    skill catalogué, il n'y a donc rien à y déplacer.

    Le filtre `_path_is_global_or_unresolvable` est conservé tel quel : une
    racine globale alimente le catalogue mais ne doit structurellement jamais
    pouvoir être mutée par ``--apply``.
    """
    try:
        project_root = (cfg.project_root or Path.cwd()).expanduser().resolve()
    except (OSError, RuntimeError):
        # Apply must fail closed when even the project root cannot be resolved.
        return []
    resolved = resolve_draft_targets(project_root, cfg.draft_targets)
    surface = resolve_skill_surface(project_root, resolved, _global_skill_roots())
    return [
        root
        for root in surface.project_roots
        if not _path_is_global_or_unresolvable(root, surface.global_roots)
    ]


def _global_skill_roots() -> tuple[Path, ...]:
    """All supported user-level skill roots, resolved for symlink protection."""

    return (
        Path.home() / ".config" / "opencode" / "skills",
        Path.home() / ".claude" / "skills",
        Path.home() / ".agents" / "skills",
    )


def _path_within(path: Path, root: Path, *, on_error: bool) -> bool:
    """Containment predicate; resolution errors return ``on_error``."""
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError):
        return on_error
    except ValueError:
        return False
    return True


def _path_is_global_or_unresolvable(path: Path, global_roots: tuple[Path, ...]) -> bool:
    """Deny a path inside a global root, or one whose resolution is uncertain."""

    return any(_path_within(path, global_root, on_error=True) for global_root in global_roots)


def _normalize_skill_relative(skill_id: str) -> Path | None:
    """Normalize one relative skill id without changing its identity."""

    requested = str(skill_id or "").strip()
    raw_parts = requested.split("/")
    if not _skill_id_is_safe_relative(requested, raw_parts):
        return None
    normalized = Path(*raw_parts)
    if normalized.is_absolute() or normalized.parts != tuple(raw_parts):
        return None
    if "_archive" in normalized.parts:
        return None
    return normalized


def _skill_id_is_safe_relative(requested: str, raw_parts: list[str]) -> bool:
    """Reject an empty, absolute, home-relative or traversing skill id."""
    if not requested or "\\" in requested or requested.startswith("/") or requested.startswith("~"):
        return False
    return not any(part in {"", ".", ".."} for part in raw_parts)


def _archive_normalize_request(
    skill_id: str, date: str
) -> tuple[Path | None, tuple[str, str | None] | None]:
    """Validate the skill id and archive date; return ``(path, error)``."""
    requested_path = _normalize_skill_relative(skill_id)
    if requested_path is None:
        return None, ("missing", "skill_id absent or unsafe")
    if not isinstance(date, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) is None:
        return None, ("missing", "date archive absente ou unsafe")
    return requested_path, None


def _archive_within_skills_dir(src: Path, dst: Path, skills_dir: Path) -> bool:
    """All three source/destination paths must stay inside ``skills_dir``."""
    return (
        _path_within(src, skills_dir, on_error=False)
        and _path_within(src / "SKILL.md", skills_dir, on_error=False)
        and _path_within(dst.parent, skills_dir, on_error=False)
    )


def _archive_collect_candidates(
    skills_dirs: list[Path],
    requested_path: Path,
    date: str,
    global_roots: tuple[Path, ...],
) -> tuple[list[tuple[Path, Path, Path]], tuple[str, str | None] | None]:
    """Resolve project-local ``(skills_dir, src, dst)`` candidates, fail closed."""
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
                return [], ("missing", "skill source/destination hors périmètre projet")
            if not _archive_within_skills_dir(src, dst, skills_dir):
                return [], ("missing", "skill source/destination hors périmètre projet")
            destination_exists = dst.exists()
            source_exists = src.is_dir()
        except (OSError, RuntimeError) as exc:
            return [], ("rejected", f"chemin source illisible: {exc}")
        if destination_exists or source_exists:
            candidates.append((skills_dir, src, dst))
    return candidates, None


def _archive_is_protected(metadata: Mapping[str, object]) -> bool:
    """Whether a source frontmatter declares user origin or pin TTL."""
    return metadata.get("origin") == "user" or metadata.get("ttl_policy") == "pin"


def _archive_catalog_inconsistent(
    skill_id: str,
    catalog_sid: str | None,
    catalog_origin: str | None,
    catalog_ttl: str | None,
    metadata: Mapping[str, object],
) -> bool:
    """Whether the catalogue row and the source frontmatter disagree."""
    return (
        catalog_sid != skill_id
        or metadata.get("origin") != catalog_origin
        or metadata.get("ttl_policy") != catalog_ttl
    )


def _archive_catalog_gate(
    skill_id: str, src: Path, catalog_entry: Mapping[str, object] | None
) -> tuple[str, str | None] | None:
    """Authorize the move from the catalogue plus a fresh frontmatter read."""
    catalog_sid, catalog_origin, catalog_ttl = _skill_fields(catalog_entry)
    if catalog_origin == "user" or catalog_ttl == "pin":
        return "protected", "catalogue protège cette skill (origin=user ou ttl_policy=pin)"
    if catalog_entry is None or not catalog_entry_is_complete(catalog_entry):
        # A name-only or absent catalogue entry is sufficient for a dry-run
        # proposal, never for an apply move.  Still inspect the source so a
        # stale proposal cannot hide user/pinned protection behind missing data.
        from .safe_git_write import validate_skill_source

        _valid, metadata, detail = validate_skill_source(src / "SKILL.md")
        if _archive_is_protected(metadata):
            return "protected", detail
        return "unverified", "catalogue incomplet ou absent; archive apply refusé"
    # Re-read frontmatter on every apply. A stale catalogue must never weaken
    # source validation or protection metadata.
    from .safe_git_write import validate_skill_source

    valid, metadata, detail = validate_skill_source(src / "SKILL.md")
    if _archive_is_protected(metadata):
        return "protected", detail
    if not valid:
        return "unverified", detail
    if _archive_catalog_inconsistent(skill_id, catalog_sid, catalog_origin, catalog_ttl, metadata):
        return "unverified", "catalogue et frontmatter source incohérents"
    return None


def _archive_move_out_of_scope(
    skills_dir: Path,
    src: Path,
    dst: Path,
    global_roots: tuple[Path, ...],
    project_root: Path,
) -> bool:
    """Any involved path outside the project root, or globally owned, denies move."""
    return any(
        _path_is_global_or_unresolvable(candidate, global_roots)
        or not _path_within(candidate, project_root, on_error=False)
        for candidate in (skills_dir, src, src / "SKILL.md", dst, dst.parent)
    )


def _archive_move(
    skills_dir: Path,
    src: Path,
    dst: Path,
    global_roots: tuple[Path, ...],
    mover,
) -> tuple[str, str | None]:
    """Revalidate immediately before the move, then delegate to ``mover``.

    Symlink/race changes after the discovery checks must fail closed, including
    project-root containment.
    """
    project_root = skills_dir.parents[2]
    try:
        if _archive_move_out_of_scope(skills_dir, src, dst, global_roots, project_root):
            return "rejected", "skill source/destination hors périmètre projet"
        if not src.is_dir() or dst.exists():
            return "rejected", "état source/destination modifié avant déplacement"
    except (OSError, RuntimeError) as exc:
        return "rejected", f"chemin source illisible: {exc}"
    try:
        return mover(src, dst)
    except (OSError, ValueError, RuntimeError) as exc:
        return "rejected", str(exc)


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
    requested_path, failure = _archive_normalize_request(skill_id, date)
    if failure is not None:
        return failure
    global_roots = _global_skill_roots()
    candidates, failure = _archive_collect_candidates(
        skills_dirs, requested_path, date, global_roots
    )
    if failure is not None:
        return failure
    if not candidates:
        return "missing", None
    if len(candidates) > 1:
        return "ambiguous", "skill_id existe dans plusieurs racines projet"
    skills_dir, src, dst = candidates[0]
    # A previous successful run leaves the source absent but destination present.
    # Report ``exists`` rather than ``missing`` and never attempt a second move.
    if dst.exists():
        return "exists", f"{dst} déjà présent (idempotent)"
    if not src.is_dir():
        return "missing", "skill source absent ou non-répertoire"
    gate = _archive_catalog_gate(skill_id, src, catalog_entry)
    if gate is not None:
        return gate
    return _archive_move(skills_dir, src, dst, global_roots, mover)


def _ttl_archive_findings(archive_ids: list[str]) -> list[dict]:
    """Synthesize the R8 TTL archive findings consumed by ``decide_actions``."""
    return [
        {
            "tag_action": "archive",
            "target_skill_id": sid,
            "reason": "TTL stale/zero-load",
            "_source": "ttl",
        }
        for sid in archive_ids
    ]


def _usage_from_carry(cfg, engine_dir: Path, runs_seen: int) -> tuple[list, int]:
    """Inter-run fallback: rebuild usage records from the engine carry file."""
    watch_cfg = getattr(cfg, "watch_distill", None)
    configured_memory = getattr(watch_cfg, "memory_file", None)
    carry = read_carry(engine_dir, output_dir=cfg.output_dir, memory_file=configured_memory)
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
    return usage, runs_seen


def _load_curate_inputs(args, cfg, engine_dir: Path, out: Path, date: str) -> tuple:
    """Phase 1 — resolve coherence/catalog/usage, falling back to run artefacts.

    ``--coherence``/``--catalog``/``--usage`` overrides win; otherwise the run's
    dated artefacts (and the inter-run carry) are read — no LLM piping required.
    """
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
        usage, runs_seen = _usage_from_carry(cfg, engine_dir, runs_seen)

    usage = usage if isinstance(usage, list) else []
    return coherence, catalog, usage, runs_seen


def _print_curate_plan(apply, out, coherence, catalog, decisions, archive_ids) -> None:
    """Print the DRY-RUN/APPLY plan and every final decision before executing."""
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


def _new_curate_state() -> dict:
    """Mutable counters + manifest decision log threaded through Phase 2."""
    return {
        "applied": 0,
        "proposed": 0,
        "skipped": 0,
        "skipped_user": 0,
        "archive_pending": 0,
        "move_status_counts": {
            "moved": 0,
            "exists": 0,
            "missing": 0,
            "error": 0,
            "rejected": 0,
            "ambiguous": 0,
            "protected": 0,
            "unverified": 0,
            "not_attempted": 0,
        },
        "manifest_decisions": [],
    }


def _record_curate_decision(state, sid, action, reason, source, status, move_status) -> None:
    state["manifest_decisions"].append(
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


def _execute_curate_archive(
    sid, action, reason, source, state, *, apply, skills_dirs, date, catalog, mover
) -> None:
    """Dry-run proposal or gated apply move for one ``archive`` decision."""
    move_status = "not_attempted"
    if not apply:
        state["archive_pending"] += 1
        print(f"  PROPOSE: {sid} — archive (dry-run, aucune op fs)", flush=True)
        _record_curate_decision(state, sid, action, reason, source, "not_attempted", move_status)
        return
    catalog_entry = select_catalog_entry(catalog, sid)
    move_status, _ = _archive_skill(sid, skills_dirs, date, mover, catalog_entry=catalog_entry)
    state["move_status_counts"]["not_attempted"] -= 1
    state["move_status_counts"].setdefault(move_status, 0)
    state["move_status_counts"][move_status] += 1
    if move_status in ("moved", "exists"):
        state["applied"] += 1
        print(f"  ARCHIVE: {sid} — {move_status}", flush=True)
    else:
        state["skipped"] += 1
        if move_status == "protected":
            state["skipped_user"] += 1
        print(f"  SKIP(archive {move_status}): {sid}", flush=True)
    _record_curate_decision(state, sid, action, reason, source, move_status, move_status)


def _execute_curate_decision(decision, state, *, apply, skills_dirs, date, catalog, mover) -> None:
    """Route one final decision to skip / archive / proposal handling."""
    sid = decision["target_skill_id"]
    action = decision["action"]
    reason = decision["reason"]
    source = decision.get("source") or "coherence"
    move_status = "not_attempted"
    state["move_status_counts"][move_status] += 1
    if action == "skip":
        print(f"  SKIP: {sid} — {reason}", flush=True)
        state["skipped"] += 1
        if "user-origin" in reason:
            state["skipped_user"] += 1
        _record_curate_decision(state, sid, action, reason, source, "skipped", move_status)
        return
    if action == "archive":
        _execute_curate_archive(
            sid,
            action,
            reason,
            source,
            state,
            apply=apply,
            skills_dirs=skills_dirs,
            date=date,
            catalog=catalog,
            mover=mover,
        )
        return
    # merge / reference / pin / delete / recalibrate / fix remain proposals;
    # no filesystem mutation is performed for them, even with --apply.
    state["proposed"] += 1
    print(f"  PROPOSE: {sid} — {action} (aucune op fs)", flush=True)
    _record_curate_decision(state, sid, action, reason, source, "proposed", move_status)


def _execute_curate_decisions(decisions, *, apply, skills_dirs, date, catalog, mover) -> dict:
    """Phase 2 — execute every decision, mutating one ``state`` dict."""
    state = _new_curate_state()
    # One final decision per skill is guaranteed by decide_actions.  Archive
    # operations are the only filesystem mutation and only happen in apply mode.
    for decision in decisions:
        _execute_curate_decision(
            decision,
            state,
            apply=apply,
            skills_dirs=skills_dirs,
            date=date,
            catalog=catalog,
            mover=mover,
        )
    return state


def _curate_rc(apply: bool, move_status_counts: dict) -> int:
    """1 when an apply attempted an archive that must reach the orchestrator."""
    if not apply:
        return 0
    failures = ("error", "rejected", "ambiguous", "unverified")
    return 1 if any(move_status_counts.get(status, 0) > 0 for status in failures) else 0


def _build_curate_manifest(
    *,
    apply,
    date,
    out,
    coherence,
    catalog,
    archive_ids,
    state,
    run_time,
    project_root,
) -> dict:
    """Assemble the skill-curate manifest from the Phase 2 state."""
    move_status_counts = state["move_status_counts"]
    manifest_decisions = state["manifest_decisions"]
    manifest = {
        "schema_version": 1,
        "rc": 0,
        "mode": "apply" if apply else "dry-run",
        "dry_run": not apply,
        "date": date,
        "run_dir": str(out),
        "coherence_count": len(coherence),
        "catalog_count": len(catalog),
        "applied": state["applied"],
        "proposed": state["proposed"],
        "skipped": state["skipped"],
        "archive_pending": state["archive_pending"],
        "decisions": manifest_decisions,
        "move_status_counts": move_status_counts,
        # Alias retained for report consumers that call these move counters.
        "move_counts": dict(move_status_counts),
        "archive_candidates_ttl": archive_ids,
        "archive_candidates": archive_ids,
        "skipped_user": state["skipped_user"],
        "run_provenance": _run_provenance(project_root, run_time),
    }
    manifest["rc"] = _curate_rc(apply, move_status_counts)
    manifest.update(
        manifest_metadata(
            manifest_decisions,
            generated_at=run_time.isoformat().replace("+00:00", "Z"),
            anchor=date,
        )
    )
    return manifest


def _write_curate_manifest(out_path: Path, manifest: dict, state: dict) -> int:
    """Write the manifest atomically; a write failure is itself rc=1."""
    try:
        from .writer import write_json_atomic

        write_json_atomic(out_path, manifest)
        print(
            f"  manifest written: {out_path} "
            f"(applied={state['applied']} proposed={state['proposed']} "
            f"skipped={state['skipped']} moves={state['move_status_counts']})",
            flush=True,
        )
    except OSError as exc:
        print(f"  manifest write FAILED: {exc}", flush=True)
        return 1
    return manifest["rc"]


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
    coherence, catalog, usage, runs_seen = _load_curate_inputs(args, cfg, engine_dir, out, date)
    archive_ids = ttl_archive_candidates(usage, runs_seen, stale_days=stale_days)
    decisions = decide_actions([*coherence, *_ttl_archive_findings(archive_ids)], catalog)

    _print_curate_plan(apply, out, coherence, catalog, decisions, archive_ids)

    # --- Phase 2 : dry-run or gated apply execution ---
    state = _execute_curate_decisions(
        decisions,
        apply=apply,
        skills_dirs=_skill_dirs_for(cfg),
        date=date,
        catalog=catalog,
        mover=safe_git_move,
    )
    manifest = _build_curate_manifest(
        apply=apply,
        date=date,
        out=out,
        coherence=coherence,
        catalog=catalog,
        archive_ids=archive_ids,
        state=state,
        run_time=run_time,
        project_root=cfg.project_root,
    )
    return _write_curate_manifest(out / f"skill-curate-{date}.json", manifest, state)
