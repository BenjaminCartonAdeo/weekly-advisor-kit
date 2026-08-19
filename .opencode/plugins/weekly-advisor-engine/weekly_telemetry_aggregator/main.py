"""Orchestration of Part 1 (spec flow steps 1-6) + doctor & self-cost (Part 1 §12).

The pipeline reads OpenCode telemetry directly from the local SQLite DB via the
SchemaAdapter (sqlite_reader.py, v5.16/v5.24) — no SDK, no server, no pricing chain:
`step-finish.cost` is read as-is (spec §2, "une seule règle").
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .aggregator import _cap_warnings, aggregate
from .config import TelemetryConfig, apply_lookback_override
from .harness_scope import (
    copy_scope_to_projection,
    enrich_harness_digest,
    resolve_harness_scope,
)
from .models import Period, SessionUsage, SkillCatalogEntry, WarningEntry, round6
from .run_state import activate_run, resolve_active_run_dir
from .sqlite_reader import DataSourceError, SessionMeta, _to_ms, detect_db
from .util import load_json, parse_iso_ts, root_and_orphan_ids
from .util import parse_anchor as _parse_anchor
from .writer import write_json_atomic, write_summary

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_TOTAL_FAILURE = 2

#: Sessions updated within this many minutes of run_time are still active (v5.18: < 10 min).
ACTIVE_CUTOFF_MINUTES = 10
#: Skill universe dirs (spec §2): project + global, .opencode/.claude/.agents.
SKILL_LAYOUTS = ((".opencode", "skills"), (".claude", "skills"), (".agents", "skills"))
#: Cross-check tolerance for lifetime parts-cost vs session_v2 aggregate.
CROSS_CHECK_ABS = 0.01


def _truncate(text: str, limit: int = 80) -> str | None:
    text = " ".join(str(text).split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- skills catalog


def _skill_dirs(project_root: Path | None) -> list[Path]:
    dirs: list[Path] = []
    root = project_root or Path.cwd()
    for sub, folder in SKILL_LAYOUTS:
        dirs.append(root / sub / folder)
    dirs.append(Path.home() / ".config" / "opencode" / "skills")
    return dirs


def _parse_skill_md(path: Path) -> tuple[str, str, list[str]]:
    """Minimal YAML-free frontmatter parse: (description, body[:2000], target_agents).

    Réutilise le parseur unique `frontmatter_blocks` (safe_git_write) qui gère
    l'imbrication metadata + listes YAML — plus de parseur dupliqué (v5.30 audit).
    """
    from .safe_git_write import frontmatter_blocks

    meta, body, err = frontmatter_blocks(path)
    if err:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "", "", []
        return "", text[:2000], []
    description = (meta.get("description") or "").strip()
    targets = [
        x.strip().strip("\"'[] ") for x in (meta.get("target_agents") or "").split(",") if x.strip()
    ]
    return description, body[:2000], targets


def scan_skill_catalog(
    project_root: Path | None, extra_dirs: tuple[Path, ...] = ()
) -> tuple[list[str], int, list[SkillCatalogEntry]]:
    """Scan the skill universe: (names, count, entries). Each SKILL.md once, name deduped."""
    entries: dict[str, SkillCatalogEntry] = {}
    for root in [*_skill_dirs(project_root), *extra_dirs]:
        try:
            for skill_md in sorted(root.glob("**/SKILL.md")):
                if not skill_md.is_file():
                    continue
                name = skill_md.parent.name
                if name in entries:
                    continue
                description, body, targets = _parse_skill_md(skill_md)
                entries[name] = SkillCatalogEntry(
                    name=name, description=description, body=body, target_agents=targets
                )
        except OSError:
            continue
    names = sorted(entries)
    return names, len(names), [entries[n] for n in names]


# --------------------------------------------------------------------------- session usage


def _audit_record(meta, status: str) -> dict:
    """Trace why a window-touched session was or wasn't counted (v5.28 audit)."""
    title = " ".join((meta.title or "").split()).replace("|", "¦")
    if len(title) > 60:
        title = title[:60] + "..."
    return {
        "session_id": meta.session_id,
        "title": title or None,
        "agent": meta.agent,
        "parent_id": meta.parent_id,
        "cost": meta.cost,
        "updated": str(meta.time_updated or ""),
        "status": status,
    }


def build_usage(
    meta: SessionMeta,
    adapter,
    *,
    period: Period,
    run_time: datetime,
    cfg: TelemetryConfig,
    warnings: list[WarningEntry],
    audit: list[dict] | None = None,
) -> tuple[SessionUsage | None, bool]:
    """Window-limited SessionUsage from a session. Returns (usage|None, read_failed).

    Active-session exclusion (updated < 10 min before run_time) and
    advisor-run-title exclusion (anti auto-pollution, v5.12) are applied here.
    """
    start_ms = _to_ms(period.start)
    end_ms = _to_ms(period.end)

    if (
        cfg.exclude_active_sessions
        and meta.time_updated is not None
        and meta.time_updated >= run_time - timedelta(minutes=ACTIVE_CUTOFF_MINUTES)
    ):
        warnings.append(
            WarningEntry(
                session_id=meta.session_id,
                message="session active exclue des totaux (télémétrie incomplète)",
            )
        )
        if audit is not None:
            audit.append(_audit_record(meta, "active"))
        return None, False
    if cfg.advisor_run_title and meta.title == cfg.advisor_run_title:
        if audit is not None:
            audit.append(_audit_record(meta, "advisor"))
        return None, False  # silent: excluded by design (v5.12)

    try:
        steps = adapter.session_steps(meta.session_id, start_ms, end_ms)
        tool_calls, tool_arg_chars, skills = adapter.session_tools(
            meta.session_id, start_ms, end_ms
        )
        turns = adapter.session_user_turns(meta.session_id, start_ms, end_ms)
        context_chars = adapter.session_context_chars(meta.session_id, start_ms, end_ms)
        aggregates = adapter.session_aggregates(meta.session_id)
    except Exception as exc:  # noqa: BLE001 - one session must never kill the run (spec §8)
        warnings.append(
            WarningEntry(
                session_id=meta.session_id,
                message=f"session read failed: {exc}",
                partial=True,  # telemetry gap → run is partial, not ok
            )
        )
        if audit is not None:
            audit.append(_audit_record(meta, "error"))
        return None, True

    if not steps:
        if audit is not None:
            status = (
                "no-activity"
                if adapter.has_telemetry_rows(meta.session_id)
                else "unflushed"  # aucune ligne message/part en DB — client actif (K1)
            )
            audit.append(_audit_record(meta, status))
            if status == "unflushed":
                warnings.append(
                    WarningEntry(
                        session_id=meta.session_id,
                        message="session sans télémétrie persistée en DB (0 message/part — client actif ?)",
                    )
                )
        return None, False

    missing = sorted({s.model for s in steps if s.cost is None})
    for model in missing:
        warnings.append(
            WarningEntry(session_id=meta.session_id, message=f"missing-pricing:{model}")
        )

    # Cross-check: lifetime step-finish costs vs session_v2 aggregate (spec §8).
    reported_cost = (
        round6(aggregates["cost"]) if aggregates and aggregates.get("cost") is not None else None
    )
    if reported_cost:
        try:
            lifetime = 0.0
            for rec in adapter.session_parts(meta.session_id):
                if rec.kind == "step-finish" and rec.cost is not None:
                    lifetime += rec.cost
            tolerance = cfg.cross_check_tolerance_pct
            if abs(lifetime - reported_cost) > max(CROSS_CHECK_ABS, tolerance * reported_cost):
                warnings.append(
                    WarningEntry(
                        session_id=meta.session_id,
                        message=f"cross-check mismatch: parts cost ${lifetime:.4f} vs session_v2 ${aggregates['cost']:.4f}",
                        parts_cost=round6(lifetime),
                        session_v2_cost=round6(aggregates["cost"]),
                    )
                )
        except Exception:  # noqa: BLE001 - cross-check is best-effort
            pass

    window_cost = sum(st.cost for st in steps if st.cost is not None)
    if (
        reported_cost
        and window_cost > reported_cost * (1.0 + cfg.cross_check_tolerance_pct) + CROSS_CHECK_ABS
    ):
        # v5.30 (4) : le coût FENÊTRÉ dépasse le lifetime session (enfants au coût non
        # répercuté dans session.cost, ou compaction) — le cross-check parts-lifetime est
        # aveugle à ce cas.
        warnings.append(
            WarningEntry(
                session_id=meta.session_id,
                message=(
                    f"windowed cost ${window_cost:.4f} > lifetime ${reported_cost:.4f} "
                    "(enfants/compaction non couverts par session.cost)"
                ),
                parts_cost=round6(window_cost),
                session_v2_cost=round6(reported_cost),
            )
        )
    first_user = next((_truncate(t) for t in turns if t.strip()), None)
    if audit is not None:
        audit.append(_audit_record(meta, "included"))
    return (
        SessionUsage(
            session_id=meta.session_id,
            title=meta.title or None,
            project_path=meta.directory,
            agent_type=meta.agent,
            parent_id=meta.parent_id,
            steps=steps,
            tool_calls=tool_calls,
            tool_arg_chars=tool_arg_chars,
            skills_loaded=skills,
            user_turns=turns,
            context_chars=context_chars,
            first_user_text=first_user,
            reported_cost_usd_lifetime=reported_cost,
        ),
        False,
    )


def _build_selection(
    audit: list[dict],
    limit: int,
    known_parent_ids: set[str] | None = None,
    period: dict | None = None,
) -> dict:
    """Aggregate the per-session disposition trace into the summary's selection audit.

    v6.0.l (E2) : ``counted`` = **racines comptées**, même convention que
    ``aggregate()`` — les enfants dont le parent est dans la fenêtre sont
    fusionnés dans leur racine, jamais comptés individuellement (fini le
    28 vs 29 : le rapport compte 28 racines et expose les enfants fusionnés
    séparément). ``counted_all`` garde le décompte brut des enregistrements
    inclus, ``merged_children`` la différence.
    """
    counts: dict[str, int] = {}
    for rec in audit:
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    included = [rec for rec in audit if rec["status"] == "included"]
    # Même logique d'orphelins que aggregate() (util.root_and_orphan_ids): un
    # enfant dont le parent n'est ni dans la fenêtre ni connu en base n'est
    # fusionné nulle part.
    orphan_ids, root_ids = root_and_orphan_ids(
        ((r["session_id"], r.get("parent_id")) for r in included),
        known_parent_ids=known_parent_ids,
    )
    cores = [rec for rec in included if rec["session_id"] in root_ids]
    recent = sorted(audit, key=lambda r: r["updated"], reverse=True)[: max(0, limit)]
    # v6.0.n : marquage fenêtre — les sessions actives post-fenêtre (runs récents
    # hors période) restent listées mais séparées dans le rapport (§1).
    # period peut être un dict (tests/JSON) ou un objet Period du summary.
    if isinstance(period, dict):
        period_start, period_end = period.get("start"), period.get("end")
    else:
        period_start, period_end = getattr(period, "start", None), getattr(period, "end", None)
    window_start = parse_iso_ts(period_start)
    window_end = parse_iso_ts(period_end)
    for rec in recent:
        ts = parse_iso_ts(rec.get("updated"))
        rec["in_window"] = bool(
            ts is not None
            and (window_start is None or ts >= window_start)
            and (window_end is None or ts <= window_end)
        )
    return {
        "window_touched": len(audit),
        "counted": len(cores),
        "counted_all": len(included),
        "merged_children": len(included) - len(cores),
        "excluded_active": counts.get("active", 0),
        "excluded_advisor": counts.get("advisor", 0),
        "excluded_no_activity": counts.get("no-activity", 0),
        "excluded_unflushed": counts.get("unflushed", 0),
        "excluded_error": counts.get("error", 0),
        "recent": recent,
    }


def _existing_period(path: Path) -> tuple[str | None, str | None]:
    """(start, end) ISO strings of an existing summary JSON; (None, None) on garbage."""
    data = load_json(path) or {}
    period = data.get("period")
    if not isinstance(period, dict):
        return None, None
    return period.get("start"), period.get("end")


# --------------------------------------------------------------------------- run


def run(
    cfg: TelemetryConfig,
    *,
    anchor: str | None = None,
    force: bool = False,
    top_sessions_limit: int | None = None,
    include_subagents: bool | None = None,
    fail_on_missing_telemetry: bool = False,
    lookback_days: int | None = None,
) -> int:
    """Run the aggregation pipeline. Returns the process exit code (0/1/2)."""
    run_time = _parse_anchor(anchor)
    if top_sessions_limit is not None:
        cfg.top_sessions_limit = max(0, top_sessions_limit)
    if include_subagents is not None:
        cfg.include_subagents = include_subagents
    if fail_on_missing_telemetry:
        cfg.fail_on_missing_telemetry = True
    apply_lookback_override(cfg, lookback_days)

    period = Period(start=run_time - timedelta(hours=cfg.window_hours()), end=run_time)
    warnings: list[WarningEntry] = []
    audit: list[dict] = []
    print(
        f"telemetry-aggregator: fenêtre {cfg.lookback_days} j [{period.start.isoformat()} → {period.end.isoformat()}]",
        flush=True,
    )

    try:
        _path, adapter = detect_db(cfg.opencode_db_path)
    except DataSourceError as exc:
        print(f"telemetry-aggregator: FATAL: {exc} — lancer doctor", file=sys.stderr, flush=True)
        return EXIT_TOTAL_FAILURE

    read_failed = False
    usages: list[SessionUsage] = []
    try:
        try:
            metas = adapter.list_sessions(_to_ms(period.start))
            all_ids = {m.session_id for m in adapter.list_sessions(0)}
        except Exception as exc:  # noqa: BLE001
            print(
                f"telemetry-aggregator: FATAL: session listing failed: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return EXIT_TOTAL_FAILURE
        print(
            f"telemetry-aggregator: {len(metas)} session(s) touchée(s) — lecture télémétrie…",
            flush=True,
        )
        for meta in metas:
            if meta.time_updated is not None and meta.time_updated < period.start:
                continue
            usage, failed = build_usage(
                meta,
                adapter,
                period=period,
                run_time=run_time,
                cfg=cfg,
                warnings=warnings,
                audit=audit,
            )
            if failed:
                read_failed = True
            if usage is not None:
                usages.append(usage)
        if read_failed and cfg.fail_on_missing_telemetry:
            return EXIT_PARTIAL
    finally:
        adapter.conn.close()

    print("telemetry-aggregator: agrégation…", flush=True)
    catalog_names, catalog_count, catalog_entries = scan_skill_catalog(cfg.project_root)
    summary = aggregate(
        usages,
        period=period,
        generated_at=run_time,
        top_sessions_limit=cfg.top_sessions_limit,
        include_subagents=cfg.include_subagents,
        skill_catalog=catalog_names,
        skill_catalog_entries=catalog_entries,
        warnings=warnings,
        known_parent_ids=all_ids,
        session_outlier_z=cfg.session_outlier_z,
        session_outlier_min_cost_usd=cfg.session_outlier_min_cost_usd,
        outlier_min_sessions=cfg.outlier_min_sessions,
        user_prompt_repeat_min=cfg.user_prompt_repeat_min,
        user_prompt_repeat_similarity=cfg.user_prompt_repeat_similarity,
        user_prompt_repeat_min_chars=cfg.user_prompt_repeat_min_chars,
        skill_similarity_min=cfg.skill_similarity_min,
    )
    summary.selection = _build_selection(
        audit, cfg.audit_max_sessions, all_ids, period=getattr(summary, "period", None)
    )
    extra_warnings: list[WarningEntry] = []
    if summary.selection["window_touched"] == 0:
        extra_warnings.append(
            WarningEntry(
                session_id=None,
                message="aucune session mise à jour dans la fenêtre — vérifier la période/ancre",
            )
        )
    elif summary.selection["counted"] == 0:
        extra_warnings.append(
            WarningEntry(
                session_id=None,
                message=(
                    f"0 session comptée sur {summary.selection['window_touched']} touchée(s) — "
                    "télémétrie absente ou fenêtre vide (audit §1)"
                ),
            )
        )
    if extra_warnings:
        summary.warnings = _cap_warnings([*summary.warnings, *extra_warnings])

    # v6.0.k (F1): every run gets its own UUID-scoped directory — artifacts of
    # different runs (same anchor or not) can never collide or overwrite each
    # other; `--force` has no destructive meaning anymore.
    date = run_time.strftime("%Y-%m-%d")
    active = activate_run(cfg.output_dir, date, run_time)
    same_date_runs = sorted(
        d.name for d in (cfg.output_dir / "runs").glob(f"{date}-*") if d.is_dir()
    )
    if len(same_date_runs) > 1:
        print(
            f"telemetry-aggregator: re-run fenêtre {date} — nouveaux artefacts isolés "
            f"dans runs/{active.run_id} (le run précédent est conservé)",
            flush=True,
        )
    if force:
        print(
            "telemetry-aggregator: --force sans objet depuis v6.0.k (chaque run écrit "
            "dans son propre répertoire UUID)",
            flush=True,
        )
    out_path = active.run_dir / f"weekly-summary-{date}.json"
    print("telemetry-aggregator: écriture du summary…", flush=True)
    write_summary(out_path, summary)

    print(
        f"telemetry-aggregator: sessions={summary.totals.session_count} "
        f"tokens={summary.totals.total_tokens} cost=${summary.totals.total_cost_usd:.6f} "
        f"warnings={len(summary.warnings)} run_dir={active.run_dir} file={out_path}",
        flush=True,
    )
    return EXIT_PARTIAL if any(w.partial for w in summary.warnings) else EXIT_OK


# --------------------------------------------------------------------------- doctor / self-cost


def _version_tuple(version: str) -> tuple[int, ...] | None:
    nums = [int(x) for x in re.findall(r"\d+", version)]
    if not nums:
        return None
    return tuple(nums[:3]) + (0,) * (3 - len(nums[:3]))


def _check_migrations(adapter) -> int | None:
    for table in ("migration", "data_migration"):
        try:
            row = adapter.conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
            if row is not None:
                return int(row["n"])
        except Exception:  # noqa: BLE001
            continue
    return None


def doctor(
    cfg: TelemetryConfig,
    *,
    cwd: Path | None = None,
    opencode_bin: str = "opencode",
    config_loaded: bool = False,
) -> int:
    """Diagnose the installation — reads/writes nothing but a probe file in output_dir."""
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    problems: list[str] = []
    warnings: list[str] = []

    if cfg.project_root is None:
        problems.append("project_root manquant dans la config")
    else:
        if not (cfg.project_root / ".opencode").is_dir():
            problems.append(
                f"project_root {cfg.project_root} ne contient pas .opencode/ — "
                "adapter la config (clone : project_root = chemin absolu de votre repo)"
            )
        if not config_loaded:
            try:
                # layout kit : cwd = moteur (config lue au cwd) ≠ project_root (repo audité) —
                # le vrai défaut est une config introuvable (cron lancé d'un dossier quelconque)
                _cfg_nearby = (cwd / "weekly-telemetry-config.json").is_file()
                _cfg_at_root = (cfg.project_root / "weekly-telemetry-config.json").is_file()
                if not _cfg_nearby and not _cfg_at_root:
                    warnings.append(
                        f"config introuvable au cwd ({cwd}) ni au project_root — vérifier --dir du cron"
                    )
            except OSError:
                warnings.append("project_root non résoluble — chemins à vérifier")

    try:
        proc = subprocess.run(
            [opencode_bin, "--version"], capture_output=True, text=True, timeout=15
        )
        version = (proc.stdout or proc.stderr).strip()
    except (OSError, subprocess.TimeoutExpired):
        # Non fatal (v6.0.f) : le run est lancé PAR opencode (binaire absolu du cron) et
        # le pipeline lit opencode.db directement — un PATH étroit (cron) rend le binaire
        # invisible au sous-processus sans casser la revue. Le version-pin devient une
        # note, comme harness-eval.
        warnings.append(f"opencode introuvable ou non exécutable ({opencode_bin})")
        version = "?"

    if version and version != "?":
        cur = _version_tuple(version)
        minv = _version_tuple(cfg.opencode_version_min or "0")
        if cur is not None and minv is not None and cur < minv:
            problems.append(
                f"opencode {version} < {cfg.opencode_version_min} — épinglage du schéma non garanti"
            )

    try:
        path, adapter = detect_db(cfg.opencode_db_path)
        migrations = _check_migrations(adapter)
        n = migrations if migrations is not None else 0
        if migrations is None:
            warnings.append("compteur de migrations introuvable — schéma non standard")
        elif (adapter.name == "v1" and n < 42) or (adapter.name == "v2" and n < 1):
            warnings.append(f"compteur de migrations faible ({n}) — vérifier la version d'OpenCode")
        adapter.conn.close()
        print(f"doctor: base OpenCode détectée: {path} (adaptateur {adapter.name}, migrations={n})")
    except DataSourceError as exc:
        problems.append(f"base OpenCode non détectée: {exc}")
        print(f"doctor: base OpenCode: KO ({exc})")

    try:
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.output_dir / ".doctor-write-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        print(f"doctor: output_dir accessible en écriture: {cfg.output_dir}")
    except OSError as exc:
        problems.append(f"output_dir non accessible en écriture: {exc}")

    for tool in ("harness-eval", "git"):
        if shutil.which(tool) is None:
            warnings.append(
                f"{tool} absent du PATH (rien n'est lancé, mais l'étape correspondante sera dégradée)"
            )

    # Version pin check (spec §7) — drift makes lint deltas non-comparable.
    if shutil.which("harness-eval") is not None and cfg.harness_eval_version:
        try:
            proc = subprocess.run(
                ["harness-eval", "--version"], capture_output=True, text=True, timeout=15
            )
            version = (proc.stdout or proc.stderr).strip()
            if version and cfg.harness_eval_version not in version:
                warnings.append(
                    f"harness-eval {version} ≠ pin {cfg.harness_eval_version} — dérive de version (spec §7)"
                )
        except (OSError, subprocess.TimeoutExpired):
            warnings.append("harness-eval --version indisponible")

    if cfg.watch_repos:
        if shutil.which("gh") is None:
            warnings.append(
                "watch_repos configuré mais gh absent du PATH — repos privés/renommés non suivis"
            )
        else:
            try:
                proc = subprocess.run(
                    ["gh", "auth", "status", "--active"], capture_output=True, text=True, timeout=10
                )
                if proc.returncode != 0:
                    warnings.append(
                        "watch_repos configuré mais gh non authentifié — repos privés indisponibles (gh auth login)"
                    )
            except (OSError, subprocess.TimeoutExpired):
                warnings.append("gh auth status indisponible — vérifier l'authentification gh")

    for msg in warnings:
        print(f"doctor: WARNING: {msg}")
    for msg in problems:
        print(f"doctor: PROBLEM: {msg}")

    if problems:
        return EXIT_TOTAL_FAILURE
    # Warnings (harness-eval absent, cwd hint, migrations bas) sont des notes
    # d'opération — un setup sain retourne 0 avec les notes imprimées.
    return EXIT_OK


def harness(cfg: TelemetryConfig, *, anchor: str | None = None, timeout: int = 900) -> int:
    """Run ``harness-eval`` against the configured temporary projection.

    Exit 0/1 of the tool = success (violations live in the digest, spec Partie 0 §4);
    any other code or a missing binary = real step failure.  The worktree is
    never modified by projection creation; when the tool emits a digest, its
    temporary paths are remapped before the scope and normalized counts are
    merged into the output.
    """
    run_time = _parse_anchor(anchor)
    date = run_time.strftime("%Y-%m-%d")
    binary = shutil.which("harness-eval")
    if binary is None:
        print(
            "harness: FATAL: harness-eval absent du PATH — lancer doctor",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_TOTAL_FAILURE
    if cfg.project_root is None:
        print("harness: FATAL: project_root manquant dans la config", file=sys.stderr, flush=True)
        return EXIT_TOTAL_FAILURE
    try:
        vp = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15)
        version = (vp.stdout or vp.stderr).strip()
        if version and cfg.harness_eval_version and cfg.harness_eval_version not in version:
            print(
                f"harness: WARNING: version {version} ≠ pin {cfg.harness_eval_version}", flush=True
            )
    except (OSError, subprocess.TimeoutExpired):
        print("harness: WARNING: --version indisponible", flush=True)
    out_dir = resolve_active_run_dir(cfg.output_dir, date)
    out_path = out_dir / f"weekly-harness-digest-{date}.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        scope = resolve_harness_scope(cfg.project_root, cfg.harness_include)
        for warning in scope.warnings:
            print(f"harness: WARNING: {warning}", flush=True)

        with tempfile.TemporaryDirectory(prefix="weekly-harness-") as temporary:
            projection_root = Path(temporary)
            copy_scope_to_projection(cfg.project_root, scope, projection_root)
            proc = subprocess.run(
                [
                    binary,
                    "harness-lint",
                    str(projection_root),
                    "--format",
                    "json",
                    "--output",
                    str(out_path),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode not in (0, 1):
                print(
                    f"harness: FATAL: rc={proc.returncode} inattendu — échec réel d'étape",
                    file=sys.stderr,
                    flush=True,
                )
                return EXIT_TOTAL_FAILURE

            # Keep compatibility with mocked/older wrappers that do not leave a
            # digest behind.  A real harness-eval run normally takes this path.
            if out_path.is_file():
                try:
                    digest = json.loads(out_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    print(
                        f"harness: FATAL: digest JSON illisible: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return EXIT_TOTAL_FAILURE
                if not isinstance(digest, dict):
                    print(
                        "harness: FATAL: digest JSON invalide (objet attendu)",
                        file=sys.stderr,
                        flush=True,
                    )
                    return EXIT_TOTAL_FAILURE
                enriched = enrich_harness_digest(digest, scope, projection_root)
                write_json_atomic(out_path, enriched)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"harness: FATAL: exécution impossible: {exc}", file=sys.stderr, flush=True)
        return EXIT_TOTAL_FAILURE
    print(f"harness: digest {out_path} (rc={proc.returncode})", flush=True)
    return EXIT_OK


def _advisor_cost(cfg: TelemetryConfig) -> dict | None:
    """Advisor session info: cost, session_id, tokens; None when undetectable.

    Shared by `self_cost` (CLI) and the report's self-cost line: title lookup
    first, then the most recent weekly-advisor agent session (v5.30 E).
    Raises DataSourceError when no DB is found.
    """
    _path, adapter = detect_db(cfg.opencode_db_path)
    try:
        meta = adapter.find_session_by_title(cfg.advisor_run_title)
        if meta is None:
            # v5.30 (E) : fallback — session la plus récente de l'agent weekly-advisor
            # (le titre du run peut différer du advisor_run_title si le prompt cron change).
            best = None
            for m in adapter.list_sessions(0):
                if (
                    m.agent
                    and "weekly-advisor" in m.agent
                    and (
                        best is None
                        or (m.time_updated or datetime.min.replace(tzinfo=UTC))
                        > (best.time_updated or datetime.min.replace(tzinfo=UTC))
                    )
                ):
                    best = m
            meta = best
        if meta is None:
            return None
        agg = adapter.session_aggregates(meta.session_id)
        if agg is None:
            return None
        tokens = sum(
            float(agg.get(key) or 0.0)
            for key in (
                "tokens_input",
                "tokens_output",
                "tokens_reasoning",
                "tokens_cache_read",
                "tokens_cache_write",
            )
        )
        return {"cost": agg["cost"], "session_id": meta.session_id, "tokens": tokens}
    finally:
        adapter.conn.close()


def self_cost(cfg: TelemetryConfig, *, anchor: str | None = None) -> int:  # noqa: ARG001
    """Cost of the pipeline's own run session (Part 1 §12) — the one place the pipeline sees itself."""
    try:
        found = _advisor_cost(cfg)
    except DataSourceError as exc:
        print(f"self-cost: FATAL: {exc}", file=sys.stderr, flush=True)
        return EXIT_TOTAL_FAILURE
    if found is None:
        print("self-cost: session du pipeline introuvable — coût propre non mesurable (0 $)")
        return EXIT_PARTIAL
    cost = float(found["cost"])
    session_id = str(found["session_id"])
    tokens = int(found.get("tokens") or 0)
    detail = f"session {session_id[:12]}"
    if tokens:
        detail += f", {tokens:,} tokens"
    print(f"self-cost: coût propre du pipeline: ${cost:.4f} ({detail})")
    return EXIT_OK
