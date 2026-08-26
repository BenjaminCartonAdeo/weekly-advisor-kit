"""Orchestration of Part 1 (spec flow steps 1-6) + doctor & self-cost (Part 1 §12).

The pipeline reads OpenCode telemetry directly from the local SQLite DB via the
SchemaAdapter (sqlite_reader.py, v5.16/v5.24) — no SDK, no server, no pricing chain:
`step-finish.cost` is read as-is (spec §2, "une seule règle").
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from warnings import warn as _warn_user

from .aggregator import _cap_warnings, aggregate, dedup_resumed_usages
from .config import TelemetryConfig, apply_lookback_override
from .draft_targets import DRAFT_HARNESS_TARGETS, describe_draft_target, resolve_draft_targets
from .harness_scope import (
    copy_scope_to_projection,
    enrich_harness_digest,
    harness_extra_roots,
    harness_rules_fingerprint,
    inject_engine_content,
    resolve_harness_scope,
    resolve_remediation_surface,
)
from .models import (
    Period,
    SessionUsage,
    SkillCatalogEntry,
    WarningEntry,
    canonical_session_id,
    round6,
    split_canonical_session_id,
)
from .providers import SessionProvider, build_providers
from .providers.base import HARNESS_OPENCODE, HarnessSession
from .run_state import RUNS_DIR, activate_run, resolve_active_run_dir
from .sqlite_reader import MIGRATION_MIN_V1, DataSourceError, SessionMeta, _to_ms, detect_db
from .util import (
    HARNESS_BASELINE_FILE,
    _abs,
    iter_digest_findings,
    load_json,
    parse_iso_ts,
    root_and_orphan_ids,
)
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

# ---- coûts estimés multi-harnais (cost_estimates optionnels) -----------------
#
# Quand un harnais n'enregistre pas de prix (steps `cost=None` → warnings
# `missing-pricing`), un coût ESTIMÉ est calculé : total_tokens × taux du
# harnais. Taux en USD par million de tokens — ordres de grandeur blended
# (input+output) des grilles publiques, jamais des montants facturés.
# Surcharge par source : clé extra "cost_rate_usd_per_mtok" dans l'entrée
# correspondante de `cfg.session_sources` (clés extra conservées au parsing).
DEFAULT_HARNESS_COST_RATE_USD_PER_MTOK = 5.0
HARNESS_COST_RATES_USD_PER_MTOK: dict[str, float] = {
    HARNESS_OPENCODE: 9.0,  # blend modèles premium (claude/gpt class)
    "copilot-vscode": 2.5,  # blend modèles Copilot (HARNESS_COPILOT_VSCODE)
}


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
    meta: SessionMeta | HarnessSession,
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
    `meta` peut être une SessionMeta brute ou une HarnessSession multi-harnais
    (ids canoniques) ; `adapter` est toute source exposant le protocol
    `SessionProvider` (un provider ou l'adaptateur SQLite historique).
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


def _harness_cost_rates(cfg: TelemetryConfig) -> dict[str, float]:
    """Taux $/Mtok par harnais : défauts documentés + surcharges par source.

    Une entrée `session_sources` peut porter la clé extra
    "cost_rate_usd_per_mtok" (valeur numérique) ; illisible → défaut conservé.
    """
    rates = dict(HARNESS_COST_RATES_USD_PER_MTOK)
    for source in cfg.session_sources:
        if not isinstance(source, dict) or source.get("cost_rate_usd_per_mtok") is None:
            continue
        try:
            rates[str(source.get("type"))] = float(source["cost_rate_usd_per_mtok"])
        except (TypeError, ValueError):
            continue  # taux illisible → défaut conservé
    return rates


def estimate_costs(
    usages: Iterable[SessionUsage],
    *,
    rates: dict[str, float] | None = None,
    default_rate: float = DEFAULT_HARNESS_COST_RATE_USD_PER_MTOK,
) -> dict[str, float]:
    """Coûts estimés ($, round6) des sessions sans AUCUN coût enregistré.

    Cible : `usage.cost_usd` null au sens télémétrique — tous les steps ont
    `cost=None` (harnais sans grille de prix). Estimation = total_tokens ×
    taux du harnais / 1e6 ; session avec au moins un coût enregistré ou sans
    tokens → absente du résultat (champ optionnel : absent = rien à estimer).
    """
    if rates is None:
        rates = HARNESS_COST_RATES_USD_PER_MTOK
    estimates: dict[str, float] = {}
    for usage in usages:
        if not usage.steps or any(s.cost is not None for s in usage.steps):
            continue
        tokens = sum(s.total_tokens for s in usage.steps)
        if tokens <= 0:
            continue
        harness = usage.harness or split_canonical_session_id(usage.session_id)[0] or ""
        estimates[usage.session_id] = round6(tokens * rates.get(harness, default_rate) / 1e6)
    return estimates


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
        "resumed_duplicates": counts.get("resumed-duplicate", 0),
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


def _warn_fallback_local_db(cfg: TelemetryConfig) -> None:
    """Warning de repli (module-level : run() shadowe `warnings` avec sa liste)."""
    warnings.warn(
        "aucune source de sessions active — repli sur la base OpenCode locale "
        f"({cfg.opencode_db_path})",
        stacklevel=3,
    )


def run(
    cfg: TelemetryConfig,
    *,
    anchor: str | None = None,
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

    # #12 : la sentinelle placeholders n'est plus réservée au doctor — la chaîne
    # hebdo standard (run→…→assemble) n'y passe jamais. Warning visible (UserWarning
    # + summary), non fatal : le run continue comme pour toute config exotique.
    placeholder_fields = _placeholder_fields(cfg)
    if placeholder_fields:
        message = _placeholder_message(placeholder_fields)
        _warn_user(message, stacklevel=2)
        warnings.append(WarningEntry(session_id=None, message=message))

    print(
        f"telemetry-aggregator: fenêtre {cfg.lookback_days} j [{period.start.isoformat()} → {period.end.isoformat()}]",
        flush=True,
    )

    providers = build_providers(cfg)
    if not providers:
        # Repli rétrocompatible : aucune source active (sources désactivées,
        # harnais absents) → comportement historique sur la base OpenCode locale.
        _warn_fallback_local_db(cfg)
        try:
            _path, adapter = detect_db(cfg.opencode_db_path)
        except DataSourceError as exc:
            print(
                f"telemetry-aggregator: FATAL: {exc} — lancer doctor", file=sys.stderr, flush=True
            )
            return EXIT_TOTAL_FAILURE
        from .providers.implementations.opencode import OpenCodeSessionProvider

        providers = [OpenCodeSessionProvider(_path, adapter)]

    read_failed = False
    usages: list[SessionUsage] = []
    all_ids: set[str] = set()
    try:
        try:
            # Fusion multi-sources : listing fenêtré + univers d'ids par provider.
            windowed: list[tuple[SessionProvider, list]] = []
            for provider in providers:
                windowed.append((provider, provider.list_sessions(_to_ms(period.start))))
                all_ids.update(m.session_id for m in provider.list_sessions(0))
        except Exception as exc:  # noqa: BLE001
            print(
                f"telemetry-aggregator: FATAL: session listing failed: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return EXIT_TOTAL_FAILURE
        # Dédup déterministe des ids canoniques entre sources du même harnais :
        # deux entrées session_sources pointant la même base listent les mêmes
        # ids → sinon double comptage sessions/coûts/tokens. La PREMIÈRE source
        # (ordre cfg.session_sources) gagne ; `all_ids` étant un set, l'univers
        # ne peut de toute façon pas doubler.
        dup_by_source: dict[int, int] = {}
        seen_canonical: set[str] = set()
        for idx, (provider, metas) in enumerate(windowed):
            kept: list = []
            for meta in metas:
                if meta.session_id in seen_canonical:
                    dup_by_source[idx] = dup_by_source.get(idx, 0) + 1
                    continue
                seen_canonical.add(meta.session_id)
                kept.append(meta)
            windowed[idx] = (provider, kept)
        if dup_by_source:
            message = " ; ".join(
                f"{n} session(s) en doublon ignorée(s) depuis {windowed[i][0].harness} source #{i + 1}"
                for i, n in sorted(dup_by_source.items())
            )
            _warn_user(message, stacklevel=2)
            warnings.append(WarningEntry(session_id=None, message=message))
        touched = sum(len(metas) for _, metas in windowed)
        print(
            f"telemetry-aggregator: {touched} session(s) touchée(s) — lecture télémétrie…",
            flush=True,
        )
        for provider, metas in windowed:
            for meta in metas:
                if meta.time_updated is not None and meta.time_updated < period.start:
                    continue
                if meta.parent_id:
                    # parent_id brut → canonique sur une COPIE (dataclasses.replace) :
                    # les metas du provider restent intactes (#10). La fusion
                    # racine/enfants (aggregate + selection audit) reste valable
                    # multi-source ; tolérant à un parent déjà préfixé (jamais de
                    # double préfixe).
                    raw_parent = str(meta.parent_id)
                    prefix = f"{provider.harness}:"
                    canonical_parent = (
                        raw_parent
                        if raw_parent.startswith(prefix)
                        else canonical_session_id(provider.harness, raw_parent)
                    )
                    meta = dataclasses.replace(meta, parent_id=canonical_parent)
                usage, failed = build_usage(
                    meta,
                    provider,
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
    finally:
        for provider in providers:
            provider.close()

    if read_failed and cfg.fail_on_missing_telemetry:
        return EXIT_PARTIAL

    # R3 (v6.1) : dédup des sessions reprises — une resume-fork copie le transcript
    # sous un nouvel id avec timestamps d'origine → sinon double comptage sessions/
    # coûts/tokens et 2 candidats d'audit pour une seule session logique.
    usages, resumed_merges = dedup_resumed_usages(usages)
    for merge in resumed_merges:
        dropped_id = merge["dropped_session_id"]
        for rec in audit:
            if rec["session_id"] == dropped_id and rec["status"] == "included":
                rec["status"] = "resumed-duplicate"
                rec["merged_into"] = merge["kept_session_id"]
        warnings.append(
            WarningEntry(
                session_id=dropped_id,
                message=f"session reprise fusionnée dans {merge['kept_session_id']} (dédup resume)",
            )
        )

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

    # coûts estimés (champ first-class) : sessions sans aucun coût enregistré →
    # estimation tokens × taux du harnais ; champ laissé à None (clé absente
    # à la sérialisation) si rien à estimer.
    estimates = estimate_costs(usages, rates=_harness_cost_rates(cfg))
    if estimates:
        summary.cost_estimates = estimates

    # v6.0.k (F1): every run gets its own UUID-scoped directory — artifacts of
    # different runs (same anchor or not) can never collide or overwrite each
    # other; the legacy --force flag (v6.0.p D2) had no meaning anymore.
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
    # X4 : chemin ABSOLU dans la réponse — l'agent enchaîne run → étapes
    # suivantes en recopiant ce chemin (un relatif dépend du cwd : incident 07:41).
    out_path = _abs(active.run_dir / f"weekly-summary-{date}.json")
    print("telemetry-aggregator: écriture du summary…", flush=True)
    write_summary(out_path, summary)

    print(
        f"telemetry-aggregator: sessions={summary.totals.session_count} "
        f"tokens={summary.totals.total_tokens} cost=${summary.totals.total_cost_usd:.6f} "
        f"warnings={len(summary.warnings)} run_dir={_abs(active.run_dir)} file={out_path}",
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


def _placeholder_fields(cfg: TelemetryConfig) -> list[str]:
    """Champs de config restés en placeholder « /path/to/... » jamais substitué."""
    return [
        field
        for field in ("project_root", "output_dir")
        # Normalisation séparateurs : str(Path) sous Windows rend des « \ » —
        # sans elle, « path/to » ne matche plus et la garde devient muette (CI).
        if "path/to" in str(getattr(cfg, field, "") or "").replace("\\", "/")
    ]


def _placeholder_message(fields: list[str]) -> str:
    """Message d'installation partagé doctor/run pour les champs placeholder."""
    return (
        "config jamais adaptée à cette installation — substituer "
        + "/".join(fields)
        + " dans weekly-telemetry-config.json (placeholders /path/to/ détectés)"
    )


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

    # Sentinelle d'installation : placeholders « /path/to/... » jamais substitués
    # dans weekly-telemetry-config.json — le fatal générique ci-dessus n'est pas
    # actionnable, on nomme le vrai défaut et les champs exacts à corriger.
    _fields = _placeholder_fields(cfg)
    if _fields:
        problems.append(_placeholder_message(_fields))

    # Garde-fou : un output_dir résolu sous .opencode/plugins/ signale un run
    # lancé avec cwd = moteur du plugin — les artefacts (reports/, baselines)
    # finissent dans le dépôt du plugin au lieu du projet audité (observé 24/08).
    resolved_out = Path(_abs(cfg.output_dir)).resolve()
    parts = resolved_out.parts
    if (
        ".opencode" in parts
        and "plugins" in parts
        and parts.index(".opencode") + 1 == parts.index("plugins")
    ):
        warnings.append(
            f"output_dir résout sous l'arbre plugins ({resolved_out}) — run "
            "probablement lancé depuis le dossier du moteur ; déplacer reports/ "
            "hors du plugin et relancer les étapes depuis la racine du projet"
        )

    try:
        # Windows : shutil.which résout "opencode" → "opencode.cmd"/".exe" (un
        # argv nu n'est pas exécutable tel quel via subprocess sans shell).
        resolved = shutil.which(opencode_bin) or opencode_bin
        proc = subprocess.run(
            [resolved, "--version"], capture_output=True, encoding="utf-8", timeout=15
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

    # Sources de sessions : itération générique sur les providers actifs du
    # registre — aucun harnais connu en dur du doctor (un nouveau provider
    # s'affiche ici sans modification de ce bloc). close() est garanti pour
    # chaque provider (try/finally), même si check_schema() lève (#9).
    providers = build_providers(cfg)
    usable = 0
    for provider in providers:
        name = getattr(provider, "harness", type(provider).__name__)
        try:
            try:
                provider.check_schema()
            except Exception as exc:  # noqa: BLE001 — diagnostic fail-soft par source
                warnings.append(f"[{name}] schéma illisible ({exc})")
                print(f"doctor: [{name}] KO ({exc})")
                continue
            usable += 1
            details: list[str] = []
            src = getattr(provider, "db_path", None)
            if src is not None:
                details.append(str(src))
            adapter = getattr(provider, "_adapter", None)
            if adapter is not None:
                # Check migrations conservé pour les providers SQLite qui exposent
                # leur adapter ; les autres (non-SQLite) sautent proprement.
                migrations = _check_migrations(adapter)
                n = migrations if migrations is not None else 0
                if migrations is None:
                    warnings.append(
                        f"[{name}] compteur de migrations introuvable — schéma non standard"
                    )
                elif n < MIGRATION_MIN_V1:
                    warnings.append(
                        f"[{name}] compteur de migrations faible ({n}) — vérifier la version du harnais"
                    )
                details.append(f"migrations={n}")
            suffix = f" ({', '.join(details)})" if details else ""
            print(f"doctor: [{name}] OK{suffix}")
        finally:
            provider.close()
    if not usable:
        problems.append(
            "aucune source de sessions disponible — vérifier session_sources / bases locales"
        )
        print("doctor: sources de sessions: aucune disponible")
    # #13 : ≥1 source utilisable ET ≥1 source KO → dégradation partielle réelle,
    # signalée par EXIT_PARTIAL au lieu d'un 0 muet.
    partial_sources = bool(providers) and 0 < usable < len(providers)

    # Cibles de drafting (cellule 2.1) : LE harnais cible effectif — override
    # config > détection par marqueurs > défaut opencode ; [] = legacy. Un
    # défaut faute de marqueur est signalé en warning (affiché en fin de run).
    resolved = resolve_draft_targets(cfg.project_root, cfg.draft_targets)
    print(f"doctor: cibles de drafting: {describe_draft_target(resolved)}")
    if resolved.warning:
        warnings.append(resolved.warning)
    # Matrice de décision 5.5 (cellule 2.2) : surface de remédiation déduite
    # du harnais résolu — affichée seule ; la règle portability.yaml = cellule 3.1.
    surface = resolve_remediation_surface(resolved.harnesses, resolved.mode)
    print(f"doctor: surface de remédiation 5.5: {surface.decision} — {surface.reason}")

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

    # Version minimum (v6.1.a — plancher, pas un pin : les versions supérieures
    # sont acceptées ; la compatibilité du format est garantie par la validation
    # de structure du digest au chargement, spec §7).
    if shutil.which("harness-eval") is not None and cfg.harness_eval_version:
        try:
            proc = subprocess.run(
                ["harness-eval", "--version"], capture_output=True, encoding="utf-8", timeout=15
            )
            version = (proc.stdout or proc.stderr).strip()
            installed = _version_tuple(version)
            required = _version_tuple(cfg.harness_eval_version)
            if installed is not None and required is not None:
                if installed < required:
                    warnings.append(
                        f"harness-eval {version} < minimum requis {cfg.harness_eval_version}"
                        " — mettre à jour : uv tool install --upgrade harness-eval"
                    )
            elif version and cfg.harness_eval_version not in version:
                warnings.append(
                    f"harness-eval --version illisible ({version!r})"
                    f" — attendu ≥ {cfg.harness_eval_version}"
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
                    ["gh", "auth", "status", "--active"],
                    capture_output=True,
                    encoding="utf-8",
                    timeout=10,
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
    if partial_sources:
        # #13 : sources mixtes OK/KO — setup dégradé, pas un échec total (2)
        # ni un setup sain (0).
        return EXIT_PARTIAL
    # Warnings (harness-eval absent, cwd hint, migrations bas) sont des notes
    # d'opération — un setup sain retourne 0 avec les notes imprimées.
    return EXIT_OK


# ---- cellule 2.2 : kit root + baseline findings -------------------------------

# v2 (faiblesse #14) : ajout de `rules_version` — empreinte du jeu de règles
# (fichiers .harness-eval/rules + version harness-eval). Une baseline sans
# empreinte (v1) est rafraîchie une fois puis réutilisée.
HARNESS_BASELINE_SCHEMA_VERSION = 2


def _engine_kit_root(cfg: TelemetryConfig) -> Path | None:
    """Racine du kit portant le contenu engine (`.opencode/{skills,commands}`).

    ``cfg.kit_root`` d'abord (distribution), puis dérivation depuis le paquet
    moteur (`<kit>/.opencode/plugins/weekly-advisor-engine/...`). None si
    aucune racine valide — l'injection engine est alors silencieusement absente.
    """
    candidates: list[Path] = []
    if cfg.kit_root is not None:
        candidates.append(Path(cfg.kit_root))
    # main.py = <kit>/.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/
    candidates.append(Path(__file__).resolve().parents[4])
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:  # pragma: no cover - chemin illisible
            continue
        if (resolved / ".opencode" / "skills").is_dir():
            return resolved
    return None


def _baseline_finding_keys(digest: Mapping) -> list[tuple[str, str]]:
    """Paires (rule, path) localisées d'un digest, dédupliquées et triées."""
    return sorted(
        {
            (str(record["rule"]), str(record["path"]))
            for record in iter_digest_findings(digest)
            if record.get("path") is not None
        }
    )


def _valid_baseline(payload: object) -> dict | None:
    """Forme attendue d'une baseline stockée : dict + findings list + captured_on str."""
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("findings"), list)
        and isinstance(payload.get("captured_on"), str)
    ):
        return payload
    return None


def _capture_or_reuse_baseline(
    output_dir: Path,
    date: str,
    enriched_digest: Mapping,
    *,
    project_root: Path,
    tool_version: str | None,
) -> dict:
    """Baseline findings : capturée au premier run, réutilisée à empreinte égale.

    Ancrage : racine ``output_dir`` (stable entre runs, comme run_state.json).
    Premier run → le snapshot courant devient la baseline (`status=created`).
    Runs suivants → réutilisation (`reused`) uniquement si l'empreinte du jeu
    de règles (``rules_version``, faiblesse #14) est identique ; sinon la
    baseline est recapturée (`refreshed`) avec note stdout + WarningEntry —
    un upgrade harness-eval/portability.yaml ne produit plus des faux
    ``new_findings`` éternels. Une baseline illisible est remplacée
    (auto-réparation conservée) mais désormais tracée (warning + note), plus
    aucun reset silencieux.
    """
    current_keys = _baseline_finding_keys(enriched_digest)
    rules_version = harness_rules_fingerprint(project_root, tool_version)
    path = output_dir / HARNESS_BASELINE_FILE
    stored: dict | None = None
    corrupted = False
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            loaded = None
        stored = _valid_baseline(loaded)
        if stored is None:
            # Faiblesse #14 : JSON cassé OU forme invalide = corruption — la
            # recapture reste automatique mais n'est plus silencieuse.
            corrupted = True

    # Restauration best-effort depuis les copies legacy (bug de migration
    # v6.0.l→v6.0.p ayant déplacé la baseline racine vers runs/<id>/legacy/).
    if stored is None and output_dir.is_dir():
        for legacy_path in sorted(
            output_dir.glob(f"{RUNS_DIR}/*/legacy/{HARNESS_BASELINE_FILE}"),
            reverse=True,
        ):
            try:
                candidate = json.loads(legacy_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            stored = _valid_baseline(candidate)
            if stored is None:
                continue
            corrupted = False
            print(
                f"harness: baseline restaurée depuis "
                f"{legacy_path.relative_to(output_dir)}",
                flush=True,
            )
            with contextlib.suppress(OSError):
                # logique intacte ce run ; réécriture retentée au suivant
                write_json_atomic(path, stored)
            break
    baseline_keys = sorted(
        {
            (str(item.get("rule")), str(item.get("path")))
            for item in (stored or {}).get("findings", [])
            if isinstance(item, Mapping) and item.get("path") is not None
        }
    )
    new_keys = sorted(set(current_keys) - set(baseline_keys)) if stored else []
    if (
        stored is not None
        and stored.get("rules_version") == rules_version
        and rules_version != "unknown"
    ):
        return {
            "schema_version": HARNESS_BASELINE_SCHEMA_VERSION,
            "status": "reused",
            "captured_on": str(stored["captured_on"]),
            "finding_count": len(baseline_keys),
            "new_findings": [{"rule": rule, "path": path_} for rule, path_ in new_keys],
            "rules_version": rules_version,
        }

    # Faiblesse #14 : toute capture non-réutilisée est tracée (jamais muette).
    entries: list[WarningEntry] = []
    status = "created"
    if stored is not None:
        # Empreinte absente (baseline legacy) ou différente (règles/outil
        # changés) → recapture unique : le prochain run retrouvera l'empreinte.
        status = "refreshed"
        entries.append(
            WarningEntry(session_id=None, message="baseline harness rafraîchie : règles changées")
        )
    elif corrupted:
        entries.append(WarningEntry(session_id=None, message="baseline illisible — recapture"))
    for entry in entries:
        print(f"harness: WARNING: {entry.message}", flush=True)

    snapshot = [
        {"rule": rule, "path": path_}
        for rule, path_ in (current_keys if stored is None else baseline_keys)
    ]
    payload = {
        "schema_version": HARNESS_BASELINE_SCHEMA_VERSION,
        "captured_on": date,
        "finding_count": len(snapshot),
        "findings": snapshot,
        "rules_version": rules_version,
    }
    try:
        write_json_atomic(path, payload)
    except OSError as exc:
        print(f"harness: WARNING: baseline non écrite ({exc})", flush=True)
        return {
            "schema_version": HARNESS_BASELINE_SCHEMA_VERSION,
            "status": status,
            "captured_on": date,
            "finding_count": len(snapshot),
            "new_findings": [],
            "error": str(exc),
            "rules_version": rules_version,
            "warnings": [dataclasses.asdict(entry) for entry in entries],
        }
    verb = "capturée" if status == "created" else "rafraîchie"
    print(f"harness: baseline findings {verb} ({len(snapshot)} finding(s))", flush=True)
    return {
        "schema_version": HARNESS_BASELINE_SCHEMA_VERSION,
        "status": status,
        "captured_on": date,
        "finding_count": len(snapshot),
        "new_findings": [],
        "rules_version": rules_version,
        "warnings": [dataclasses.asdict(entry) for entry in entries],
    }


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
    tool_version: str | None = None
    try:
        vp = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15)
        version = (vp.stdout or vp.stderr).strip()
        if version:
            tool_version = version
        installed = _version_tuple(version)
        required = _version_tuple(cfg.harness_eval_version) if cfg.harness_eval_version else None
        if installed is not None and required is not None and installed < required:
            print(
                f"harness: WARNING: harness-eval {version} < minimum requis "
                f"{cfg.harness_eval_version} — résultat non garanti",
                flush=True,
            )
    except (OSError, subprocess.TimeoutExpired):
        print("harness: WARNING: --version indisponible", flush=True)
    # X4 : chemin ABSOLU dès la résolution — l'écriture du digest, l'argument
    # --output et la réponse finale portent le même chemin non ambigu.
    out_path = _abs(
        resolve_active_run_dir(cfg.output_dir, date) / f"weekly-harness-digest-{date}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Cellule 2.2 : le harnais résolu étend la projection au-delà de
        # `.opencode/` (DRAFT_HARNESS_TARGETS) et reçoit le contenu engine.
        resolved_draft = resolve_draft_targets(cfg.project_root, cfg.draft_targets)
        extra_roots = harness_extra_roots(resolved_draft)
        inject_dirs = tuple(
            sorted(
                {
                    target
                    for harness in resolved_draft.harnesses
                    for target in DRAFT_HARNESS_TARGETS.get(harness, ())
                }
            )
        )
        scope = resolve_harness_scope(
            cfg.project_root, cfg.harness_include, extra_roots=extra_roots
        )
        for warning in scope.warnings:
            print(f"harness: WARNING: {warning}", flush=True)

        with tempfile.TemporaryDirectory(prefix="weekly-harness-") as temporary:
            projection_root = Path(temporary)
            copy_scope_to_projection(cfg.project_root, scope, projection_root)
            orphans = inject_engine_content(
                cfg.project_root,
                inject_dirs,
                projection_root,
                kit_root=_engine_kit_root(cfg),
            )
            if orphans:
                print(
                    f"harness: contenu engine projeté ({len(orphans)} fichier(s) orphelin(s))",
                    flush=True,
                )
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
                # Cellule 2.2 : signal de projection (harnais, orphelins) +
                # baseline findings capturée au premier run, réutilisée ensuite.
                enriched["draft_targets"] = {
                    "mode": resolved_draft.mode,
                    "harnesses": list(resolved_draft.harnesses),
                    "warning": resolved_draft.warning,
                    "extra_projection_roots": list(extra_roots),
                    "injected_engine_files": len(orphans),
                    "orphan_files": sorted(orphans),
                    "surface_decision": resolve_remediation_surface(
                        resolved_draft.harnesses, resolved_draft.mode
                    ).to_dict(),
                }
                # Cellule 2.2 : baseline ancrée à la racine output_dir (comme
                # run_state.json) — un run dir UUID neuf ne doit pas casser
                # la réutilisation aux runs suivants. Faiblesse #14 : réutilisation
                # conditionnée à l'empreinte du jeu de règles (rules_version).
                baseline = _capture_or_reuse_baseline(
                    cfg.output_dir,
                    date,
                    enriched,
                    project_root=cfg.project_root,
                    tool_version=tool_version,
                )
                enriched["harness_baseline"] = baseline
                write_json_atomic(out_path, enriched)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"harness: FATAL: exécution impossible: {exc}", file=sys.stderr, flush=True)
        return EXIT_TOTAL_FAILURE
    print(f"harness: digest {out_path} (rc={proc.returncode})", flush=True)
    return EXIT_OK
