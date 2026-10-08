"""Construction des `SessionUsage` d'une session — fenêtre, exclusions, cross-checks.

Le bloc vit ici (extrait de `main`) parce que `build_usage` est le cœur de la
collecte : il porte la fenêtre temporelle, les exclusions (session active,
auto-pollution advisor) et les cross-checks de coût. `main` ré-exporte toute
la surface déplacée pour préserver son API (`main.build_usage`, `main._truncate`, …).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from datetime import datetime, timedelta

from .classifiers import _EDIT_WRITE_TOOLS
from .config import TelemetryConfig
from .models import (
    Period,
    SessionUsage,
    WarningEntry,
    round6,
    split_canonical_session_id,
)
from .providers.base import HARNESS_OPENCODE, HarnessSession
from .sqlite_reader import SessionMeta, _to_ms

#: Sessions updated within this many minutes of run_time are still active (v5.18: < 10 min).
ACTIVE_CUTOFF_MINUTES = 10
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
}


def _truncate(text: str, limit: int = 80) -> str | None:
    text = " ".join(str(text).split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- session usage


def _audit_record(meta, status: str) -> dict:
    """Trace why a window-touched session was or wasn't counted (v5.28 audit)."""
    title = " ".join((meta.title or "").split()).replace("|", "¦")
    if len(title) > 60:
        title = title[:60] + "..."
    record = {
        "session_id": meta.session_id,
        "title": title or None,
        "agent": meta.agent,
        "parent_id": meta.parent_id,
        "cost": meta.cost,
        "updated": str(meta.time_updated or ""),
        "status": status,
    }
    # Providers may attach worker outcome metadata to session descriptors. Keep
    # it in the audit trace; downstream artifacts must expose partial results.
    for key in ("rc", "truncated", "worker_status"):
        value = getattr(meta, key, None)
        if value is not None:
            record[key] = value
    return record


@dataclasses.dataclass(slots=True)
class _SessionReads:
    """Lectures brutes d'une session sur la fenêtre (build_usage)."""

    steps: list
    tool_calls: dict[str, int]
    tool_arg_chars: dict[str, int]
    skills: dict[str, int]
    tool_arg_fps: dict[str, dict[str, int]]
    tool_result_fps: dict[str, dict[str, int]]
    turns: list[str]
    context_chars: dict[str, int]
    aggregates: dict | None


def _usage_active_excluded(
    meta,
    run_time: datetime,
    cfg: TelemetryConfig,
    warnings: list[WarningEntry],
    audit: list[dict] | None,
) -> bool:
    """Exclusion session active (télémétrie incomplète) — True = exclu."""
    if not (
        cfg.exclude_active_sessions
        and meta.time_updated is not None
        and meta.time_updated >= run_time - timedelta(minutes=ACTIVE_CUTOFF_MINUTES)
    ):
        return False
    warnings.append(
        WarningEntry(
            session_id=meta.session_id,
            message="session active exclue des totaux (télémétrie incomplète)",
        )
    )
    if audit is not None:
        audit.append(_audit_record(meta, "active"))
    return True


def _usage_advisor_excluded(meta, cfg: TelemetryConfig, audit: list[dict] | None) -> bool:
    """Exclusion anti auto-pollution par titre (v5.12) — True = exclu, silencieux."""
    if not (cfg.advisor_run_title and meta.title == cfg.advisor_run_title):
        return False
    if audit is not None:
        audit.append(_audit_record(meta, "advisor"))
    return True


def _fetch_session_reads(
    adapter,
    meta,
    start_ms: int,
    end_ms: int,
    warnings: list[WarningEntry],
    audit: list[dict] | None,
) -> _SessionReads | None:
    """Reads brutes sur la fenêtre — None + warning/audit si lecture impossible."""
    try:
        steps = adapter.session_steps(meta.session_id, start_ms, end_ms)
        tool_calls, tool_arg_chars, skills = adapter.session_tools(
            meta.session_id, start_ms, end_ms
        )
        tool_arg_fps, tool_result_fps = adapter.session_tool_fingerprints(
            meta.session_id, start_ms, end_ms
        )
        return _SessionReads(
            steps=steps,
            tool_calls=tool_calls,
            tool_arg_chars=tool_arg_chars,
            skills=skills,
            tool_arg_fps=tool_arg_fps,
            tool_result_fps=tool_result_fps,
            turns=adapter.session_user_turns(meta.session_id, start_ms, end_ms),
            context_chars=adapter.session_context_chars(meta.session_id, start_ms, end_ms),
            aggregates=adapter.session_aggregates(meta.session_id),
        )
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
        return None


def _usage_no_steps_status(
    adapter, meta, audit: list[dict] | None, warnings: list[WarningEntry]
) -> None:
    """Session sans steps : audit no-activity/unflushed + warning si unflushed."""
    if audit is None:
        return
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


def _session_part_timestamps(adapter, meta) -> tuple[list, list, list]:
    """Timestamps (user, edit/write) + parts — parts vides si provider sans parts."""
    try:
        parts = adapter.session_parts(meta.session_id)
    except Exception:  # noqa: BLE001 - parts are optional per provider
        parts = []
    user_turn_timestamps = sorted(p.ts for p in parts if p.kind == "user")
    edit_write_timestamps = sorted(
        p.ts for p in parts if p.kind == "tool" and (p.tool_name or "").lower() in _EDIT_WRITE_TOOLS
    )
    return user_turn_timestamps, edit_write_timestamps, parts


def _usage_cost_warnings(
    meta,
    cfg: TelemetryConfig,
    steps: list,
    aggregates: dict | None,
    parts: list,
    reported_cost: float | None,
    warnings: list[WarningEntry],
) -> None:
    """Cross-checks coût (missing-pricing, parts-lifetime, windowed-vs-lifetime)."""
    missing = sorted({s.model for s in steps if s.cost is None})
    for model in missing:
        warnings.append(
            WarningEntry(session_id=meta.session_id, message=f"missing-pricing:{model}")
        )
    if not reported_cost:
        return
    try:
        lifetime = 0.0
        for rec in parts:
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
    if window_cost > reported_cost * (1.0 + cfg.cross_check_tolerance_pct) + CROSS_CHECK_ABS:
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

    if _usage_active_excluded(meta, run_time, cfg, warnings, audit):
        return None, False
    if _usage_advisor_excluded(meta, cfg, audit):
        return None, False  # silent: excluded by design (v5.12)

    reads = _fetch_session_reads(adapter, meta, start_ms, end_ms, warnings, audit)
    if reads is None:
        return None, True
    steps = reads.steps
    aggregates = reads.aggregates

    if not steps:
        _usage_no_steps_status(adapter, meta, audit, warnings)
        return None, False

    user_turn_timestamps, edit_write_timestamps, parts = _session_part_timestamps(adapter, meta)

    # Cross-check: lifetime step-finish costs vs session_v2 aggregate (spec §8).
    reported_cost = (
        round6(aggregates["cost"]) if aggregates and aggregates.get("cost") is not None else None
    )
    _usage_cost_warnings(meta, cfg, steps, aggregates, parts, reported_cost, warnings)
    first_user = next((_truncate(t) for t in reads.turns if t.strip()), None)
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
            tool_calls=reads.tool_calls,
            tool_arg_chars=reads.tool_arg_chars,
            tool_arg_fingerprints=reads.tool_arg_fps,
            tool_result_fingerprints=reads.tool_result_fps,
            skills_loaded=reads.skills,
            user_turns=reads.turns,
            context_chars=reads.context_chars,
            first_user_text=first_user,
            edit_write_timestamps=edit_write_timestamps,
            user_turn_timestamps=user_turn_timestamps,
            reported_cost_usd_lifetime=reported_cost,
            harness=getattr(meta, "harness", "") or getattr(adapter, "harness", "") or "",
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
