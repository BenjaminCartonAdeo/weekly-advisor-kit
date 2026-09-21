"""Insights: deltas, alerts and maintenance rules R1-R4 (Partie 6) — 100 % déterministe, zéro LLM.

`compute()` is pure (testable with fixture dicts); `run()` does I/O:
previous-run discovery by glob (spec §2, "règle de code, pas d'hypothèse"),
JSON loading, atomic write.

Trois des anciens détecteurs Python (``_daily_spike_alerts``,
``_agent_loop_findings``, ``_architecture_drift_finding``) ont été SUPPRIMÉS :
ce sont désormais les fichiers ``rules/*.md`` qui décident, via
:func:`rule_loader.load_rules` + :func:`rule_pipeline.evaluate_rule` +
:func:`rule_context.build_context`. Le câblage est dans
:func:`_production_rule_results` ; il ne reste en Python que le *routage*
(sink → ``alerts`` vs ``maintenance.findings``) et le rendu dans les formes que
``report.py`` consomme déjà. Voir :func:`_production_rule_results`.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import InsightsConfig, TelemetryConfig
from .curation import _skill_fields, is_reachable_skill
from .harness_scope import harness_digest_problems
from .main import EXIT_OK, EXIT_TOTAL_FAILURE
from .rule_loader import DEFAULT_SINK, load_rules

#: The single batch entry point for the declarative rules, and the producer of the
#: `rule-error` diagnostic (category :data:`rule_pipeline.RULE_ERROR_CATEGORY`,
#: raised per rule so a broken ``.md`` never aborts the step). Both live in
#: ``rule_pipeline`` next to the loop that owns them — re-exporting the constant
#: here would only reintroduce a second name for a value the engine emits.
from .rule_pipeline import evaluate_rules
from .run_state import RUNS_DIR, active_run_meta, resolve_active_run_dir
from .util import iso as _iso
from .util import iter_digest_findings, parse_iso_ts, period_hours, robust_z
from .util import load_json as _load
from .util import parse_anchor as _parse_anchor
from .writer import write_json_atomic

DEFAULT_CATALOG_COUNT = 0

#: plafond du z-score daily_spike — un MAD≈0 produit des z astronomiques
#: (ex. 99,19) qui sont un artefact, pas un signal plus fort (v5.31).
DAILY_SPIKE_Z_CAP = 10.0
ARCHITECTURE_DRIFT_RUNS_DEFAULT = 2
AGENT_LOOP_MIN_REPEATS_DEFAULT = 8
AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT = 3
#: D5 — part maximale de sessions « active » exclues avant alerte. Au-delà, les
#: totaux du run sont biaisés (télémétrie incomplète) : le run 2026-10-03 était à
#: 53/178 = 29,8 %. Constante module (config.py hors périmètre) ; surchargeable
#: via un attribut `exclusion_rate_max` sur InsightsConfig.
EXCLUSION_RATE_MAX_DEFAULT = 0.20


def _architecture_observation(summary: dict | None) -> dict | None:
    """Extract the optional read-only watch-context architecture projection."""
    if not isinstance(summary, dict):
        return None
    value = summary.get("architecture_observations")
    if isinstance(value, dict):
        return value
    context = summary.get("watch_context")
    if isinstance(context, dict) and isinstance(context.get("architecture_observations"), dict):
        return context["architecture_observations"]
    return None


def _architecture_drift(current: dict | None, previous: dict | None) -> list[str]:
    """Compare stable architecture facts; unknown/missing snapshots are ignored."""
    if not current or not previous:
        return []
    changed: list[str] = []
    for key in ("state_counts", "config", "inventory_counts", "harness_scope"):
        if current.get(key) != previous.get(key):
            changed.append(key)
    return changed


def _robust_z_scores(values: list[float]) -> list[float]:
    """Robust z (median + MAD, shared core in util); 2-decimal rounding kept."""
    return [round(z, 2) for z in robust_z(values)]


def _window_hours(period: dict) -> float | None:
    """Durée de fenêtre en heures (None si période invalide)."""
    return period_hours(str(period.get("start", "")), str(period.get("end", "")))


def _pct_delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / previous * 100, 1)


# ------------------------------------------------------------------ compute helpers


def _lint_delta_by_rule(
    current_digest: dict | None,
    previous_digest: dict | None,
    harness_ignored_rules: list[str] | None,
) -> dict | None:
    """Deltas de violations par règle (None si digest manquant — jamais d'échec)."""
    if current_digest is None or previous_digest is None:
        return None
    ignored = set(harness_ignored_rules or [])

    def _rule_counts(digest: dict) -> dict:
        counts: dict[str, int] = {}
        for f in flatten_harness_findings(digest):
            key = str(f.get("rule") or f.get("severity") or "unknown")
            if key in ignored:
                continue
            counts[key] = counts.get(key, 0) + 1
        return counts

    cur_counts = _rule_counts(current_digest)
    prev_counts = _rule_counts(previous_digest)
    return {
        r: cur_counts.get(r, 0) - prev_counts.get(r, 0)
        for r in sorted(set(cur_counts) | set(prev_counts))
    }


def _collect_cost_discrepancies(current_summary: dict) -> list[dict]:
    """Écarts cross-check structurés K7 (parts lifetime vs session_v2 lifetime)."""
    discrepancies: list[dict] = []
    for w in current_summary.get("warnings", []):
        msg = w.get("message", "")
        if "cross-check mismatch" not in msg:
            continue
        parts_cost = w.get("parts_cost")
        session_v2_cost = w.get("session_v2_cost")
        if parts_cost is None or session_v2_cost is None:
            continue
        discrepancies.append(
            {
                "session_id": w.get("session_id"),
                "parts_cost_usd": parts_cost,
                "session_v2_cost_usd": session_v2_cost,
            }
        )
    return discrepancies


def _month_cost(recent_summaries: list[dict], run_time: datetime) -> float:
    """Coût cumulé 30j glissants (inclus current en tête de liste)."""
    month_start = run_time - timedelta(days=30)
    total = 0.0
    for s in recent_summaries:
        gen = parse_iso_ts(s.get("generated_at")) or run_time
        if month_start <= gen <= run_time:
            total += s.get("totals", {}).get("total_cost_usd", 0.0)
    return total


def _budget_spike_alerts(
    *,
    current_cost: float | None,
    recent_summaries: list[dict],
    run_time: datetime,
    current_cache: float | None,
    wow: float | None,
    insights_cfg: InsightsConfig,
) -> list[dict]:
    """Alertes seuils budgets/cache/WoW (extrait de compute L262-318, CCN-16).

    Le pic quotidien n'est PLUS ici : c'est la règle `daily-spike`
    (``sink: alerts``), évaluée par :func:`_production_rule_results` avec sa cap
    d'affichage. Le compteur de jours de baseline, lui, vient de
    ``extra["baseline_days"]`` — même source, un seul parcours.
    """
    alerts: list[dict] = []
    if current_cost is not None and current_cost > insights_cfg.weekly_budget_usd:
        alerts.append(
            {
                "rule": "weekly_budget_usd",
                "threshold": insights_cfg.weekly_budget_usd,
                "observed": round(current_cost, 4),
                "over_by": round(current_cost - insights_cfg.weekly_budget_usd, 4),
                "severity": "high",
                "recommended_action": (
                    "budget hebdo dépassé — cibler les sessions top-coût "
                    "(context-bloat, loops swarm silent-empty)"
                ),
            }
        )

    month_cost = _month_cost(recent_summaries, run_time)
    if month_cost > insights_cfg.monthly_budget_usd:
        alerts.append(
            {
                "rule": "monthly_budget_usd",
                "threshold": insights_cfg.monthly_budget_usd,
                "observed": round(month_cost, 4),
                "over_by": round(month_cost - insights_cfg.monthly_budget_usd, 4),
                "severity": "high",
                "recommended_action": (
                    "budget mensuel dépassé — cibler les sessions top-coût "
                    "(context-bloat : relectures répétées ; loops swarm silent-empty)"
                ),
            }
        )

    if current_cache is not None and current_cache < insights_cfg.cache_hit_rate_min:
        alerts.append(
            {
                "rule": "cache_hit_rate_min",
                "threshold": insights_cfg.cache_hit_rate_min,
                "observed": current_cache,
                "severity": "medium",
            }
        )

    if wow is not None and wow > insights_cfg.cost_wow_pct_max:
        alerts.append(
            {
                "rule": "cost_wow_pct_max",
                "threshold": insights_cfg.cost_wow_pct_max,
                "observed": wow,
                "severity": "medium",
            }
        )
    return alerts


# ------------------------------------------------- moteur de règles déclaratif

#: Le rendu d'alerte (``sink: alerts``) est déclaré par la règle elle-même dans son
#: bloc frontmatter ``alert:`` — ``signal``/``threshold``/``cap``/``rows``/
#: ``row_signal``/``row_day`` —, publié dans ``details["alert"]`` par
#: ``rule_pipeline``. Le nom d'alerte affiché est l'id de la règle, souligné :
#: jamais un alias Python maintenu à part (l'ancienne entrée ``daily_spike_z_min``
#: prétendait un nom historique alors que le détecteur qui le portait est supprimé).
#: Métriques d'agrégat qui ne sont pas un signal (elles comptent, elles ne mesurent pas).
_NON_SIGNAL_METRICS = frozenset({"occurrences", "pct"})


@lru_cache(maxsize=8)
def _rule_set(
    daily_spike_z_min: float,
    loop_min_repeats: int = AGENT_LOOP_MIN_REPEATS_DEFAULT,
    loop_task_min_repeats: int = AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT,
    architecture_drift_runs: int = ARCHITECTURE_DRIFT_RUNS_DEFAULT,
) -> tuple[Any, ...]:
    """The shipped ``rules/*.md`` set, parsed once per threshold tuple.

    ``load_rules(overrides=...)`` patches *after* ``extends`` resolution, so these
    win over the versioned ``.md``; ``thresholds`` is deep-merged, so retuning one
    key leaves its siblings (``z_cap``, ``min_occurrences``) untouched. An override
    id matching no rule is ignored, so a stale knob can never break a run. The
    cached ``Rule`` objects are only read by the engine, hence safe to share.

    ``architecture-drift.drift_runs`` is overridden too, even though its ``match``
    reads the *scope field* ``drift_threshold``: the threshold is also interpolated
    in the rule's own description, and leaving it at the ``.md`` default would
    print "défaut : 2" while the run evaluates 3. Same knob, one truth.
    """
    return tuple(
        load_rules(
            overrides={
                "daily-spike": {"thresholds": {"z_min": float(daily_spike_z_min)}},
                "agent-loop": {
                    "thresholds": {
                        "loop_min_repeats": int(loop_min_repeats),
                        "loop_task_min_repeats": int(loop_task_min_repeats),
                    }
                },
                "architecture-drift": {"thresholds": {"drift_runs": int(architecture_drift_runs)}},
            }
        )
    )


def _signal_metric(metrics: Mapping[str, Any]) -> Any:
    """First aggregated scalar that is a *measurement*, not a count or a ratio.

    The ``aggregate`` block of each shipped rule declares its signal before any
    other non-count metric (``daily-spike``: ``max_raw_z`` before ``max_cost_usd`` ;
    ``agent-loop``: ``max_peak`` before ``task_loops``), so the first one is the
    signal by convention. Falls back to the strongest available number rather than
    to an invented ``0.0``.
    """
    for key, value in metrics.items():
        if key in _NON_SIGNAL_METRICS or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return value
    return 0.0


def _rule_alert(finding: Mapping[str, Any]) -> dict:
    """Render a ``sink: alerts`` finding as ONE legacy-shaped alert.

    The shape is frozen by ``report.py`` (``rule``/``severity``/``observed``/
    ``threshold``/``unit``/``note``) and by the ``[A:<rule>]`` report tags. The
    alert ``rule`` name is the rule id, underscored — the rule owns its rendering
    via its frontmatter ``alert:`` block (``details["alert"]``), from which
    ``signal``/``threshold``/``cap`` are read. No Python-side alias table.

    The z-cap is applied HERE, downstream, and only to the *displayed* value: the
    rule exposes ``raw_z`` uncapped so it can see the real strength of the
    signal, and ``note`` records that the cap actually bit.

    This is the single-alert path: it describes the *aggregate* of a finding. Use
    :func:`_rule_alerts` for the per-day fan-out (``daily-spike``) — calling this
    on a multi-day spike would attribute the max z to the first day.
    """
    details = finding.get("details") or {}
    metrics = details.get("metrics") or {}
    thresholds = details.get("thresholds") or {}
    rendering = details.get("alert") or {}
    raw = _as_float(metrics.get(str(rendering.get("signal"))))
    if rendering.get("signal") is None:
        raw = _signal_metric(metrics)
    cap = _as_float(thresholds.get(str(rendering.get("cap"))))
    if cap <= 0:
        cap = DAILY_SPIKE_Z_CAP
    days = [day for day in (metrics.get("days") or []) if isinstance(day, str)]
    return {
        "rule": str(finding.get("id") or "").replace("-", "_"),
        "threshold": _as_float(thresholds.get(str(rendering.get("threshold")))),
        "observed": round(min(raw, cap), 2),
        "severity": str(finding.get("severity") or "medium"),
        "day": days[0] if days else None,
        "note": "MAD≈0, z borné" if raw > cap else "",
    }


def _rule_alerts(finding: Mapping[str, Any]) -> list[dict]:
    """Every alert a ``sink: alerts`` finding yields — one per spiking day.

    A rule that publishes a ``rows`` metric (``daily-spike`` → ``spikes``, the
    untruncated ``(day, raw_z)`` lines) fans out: one alert per line, with that
    line's OWN ``day`` and that line's OWN ``raw_z`` (capped downstream, exactly
    like the single-alert path). That restores the ported semantics of the
    deleted ``_daily_spike_alerts``: N spiking days → N alerts.

    Combining an aggregate ``max_raw_z`` with a ``days[0]`` would publish a
    ``day`` that does not own the ``observed`` value and would drop the other
    spikes entirely — hence the fan-out, not a smarter single alert.

    Lines whose ``day`` is missing (a malformed ``date`` in ``daily_totals``) are
    dropped: an alert with ``day=None`` would be unattributable, and inventing an
    attribution is exactly the bug this replaces. Any other finding — no ``rows``
    metric, or an empty one — falls back to the single aggregated alert.
    """
    details = finding.get("details") or {}
    metrics = details.get("metrics") or {}
    thresholds = details.get("thresholds") or {}
    rendering = details.get("alert") or {}
    rows = metrics.get(str(rendering.get("rows"))) if rendering.get("rows") else None
    if not isinstance(rows, list):
        return [_rule_alert(finding)]
    cap = _as_float(thresholds.get(str(rendering.get("cap"))))
    if cap <= 0:
        cap = DAILY_SPIKE_Z_CAP
    signal_field = str(rendering.get("row_signal"))
    day_field = str(rendering.get("row_day"))
    alerts: list[dict] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        day = row.get(day_field)
        if not isinstance(day, str):
            continue
        raw = _as_float(row.get(signal_field))
        alerts.append(
            {
                "rule": str(finding.get("id") or "").replace("-", "_"),
                "threshold": _as_float(thresholds.get(str(rendering.get("threshold")))),
                "observed": round(min(raw, cap), 2),
                "severity": str(finding.get("severity") or "medium"),
                "day": day,
                "note": "MAD≈0, z borné" if raw > cap else "",
            }
        )
    return alerts or [_rule_alert(finding)]


def _rule_maintenance_finding(finding: Mapping[str, Any]) -> dict:
    """Render a ``sink: findings`` engine finding as a maintenance finding.

    The key set is exactly the audit finding shape (the one shared with
    ``rule_context._FINDING_DEFAULTS`` and read by ``report.py``), plus the rule's
    ``emit`` literals. For both migrated rules that is *identical* to the key set
    the hand-written detectors used to emit.
    """
    rendered: dict[str, Any] = {
        "session_id": None,
        "category": str(finding.get("id") or ""),
        "severity": str(finding.get("severity") or "medium"),
        "description": str(finding.get("description") or ""),
        "evidence_summary": _rule_evidence(finding),
        "recommendation": str(finding.get("suggestion") or ""),
        "recommendation_type": str(finding.get("id") or ""),
    }
    # `emit` is already shallow-merged onto the engine finding; carry the extras
    # through (impact_order_of_magnitude, action, observation_only) and never let
    # one clobber a shape key.
    for key, value in finding.items():
        if key in _FINDING_RESERVED or key in rendered:
            continue
        rendered[str(key)] = value
    return rendered


def _rule_evidence(finding: Mapping[str, Any]) -> str:
    """One-line, stable evidence: ``rule=<id>; k=v; k=v`` from the rule's own metrics.

    No free number is invented here — every value comes from the engine's
    ``details.metrics``, so the evidence cannot drift from what triggered. Counts
    and ratios are already reported by the finding itself, and a boolean is a
    yes/no rather than a measurement, so both are left out.
    """
    metrics = (finding.get("details") or {}).get("metrics") or {}
    parts = [f"rule={finding.get('id')}"] + [
        f"{key}={value}"
        for key, value in metrics.items()
        if key not in _NON_SIGNAL_METRICS and not isinstance(value, (list, bool))
    ]
    examples = finding.get("examples")
    if isinstance(examples, list) and examples:
        parts.append("examples=" + "|".join(str(item) for item in examples))
    return "; ".join(parts)


#: ``rule_pipeline`` reserves ``id``/``details``; never surface the engine's
#: plumbing (``group``, ``occurrences``, ``examples``, ``suggestion``, ``severity``)
#: as a maintenance key — ``report.py`` and the audit readers expect the audit shape.
_FINDING_RESERVED = frozenset(
    {"id", "details", "group", "occurrences", "examples", "suggestion", "severity"}
)


def _production_rule_results(
    *,
    current_summary: dict,
    previous_summary: dict | None,
    recent_summaries: list[dict],
    insights_cfg: InsightsConfig,
    ignored_findings: list[str],
    quality_findings: Mapping[str, Any] | Sequence[Any] | None = None,
    architecture_drift_runs: int = ARCHITECTURE_DRIFT_RUNS_DEFAULT,
    loop_min_repeats: int = AGENT_LOOP_MIN_REPEATS_DEFAULT,
    loop_task_min_repeats: int = AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT,
) -> tuple[list[dict], list[dict], dict[str, Any]]:
    """Evaluate the declarative rules for one run — the production wiring.

    Call sequence:

    1. ``build_context`` (:mod:`rule_context`) turns the telemetry artifacts into the
       six list scopes plus the ``extra`` options. ``recent_summaries[1:]`` because
       the adapter expects the current run already excluded — dropping a second one
       here would silently shrink the spike baseline.
    2. ``extra["ignored"] = ignored_findings`` is merged here, NOT in the adapter:
       the ignore list is a *run* concern, and ``evaluate_rules`` drops a
       suppressed rule before evaluating it.
    3. ``_rule_set(...)`` loads ``rules/*.md`` with the run's thresholds mapped onto
       ``thresholds``: ``InsightsConfig.daily_spike_z_min`` → ``daily-spike.z_min``,
       the ``loop_*_repeats`` knobs → ``agent-loop.loop_*_repeats``, and
       ``architecture_drift_runs`` → ``architecture-drift.drift_runs``. Mapping them
       as *overrides* (and not only publishing them in the scope as reference data,
       which ``agent-loop.md`` calls out) is what keeps those knobs working after the
       migration — the ``.md`` still owns the defaults.
    4. :func:`rule_pipeline.evaluate_rules` — the single batch entry point — returns
       findings sorted by rule id, each rule evaluated in isolation: a rule that
       raises degrades to one ``rule-error`` maintenance finding instead of
       aborting the step.
    5. Each finding is routed by its declared ``details["sink"]``: ``alerts`` →
       :func:`_rule_alerts` (which fans out to one alert per spiking day),
       anything else → :func:`_rule_maintenance_finding`.

    Returns ``(alerts, maintenance_findings, extra)``; ``extra`` is returned so
    ``compute()`` reuses the adapter's own baseline count instead of walking the
    summaries a second time.
    """
    # Imported here, not at module level: `rule_context` imports the shared cores
    # back from `insights`, so a top-level import would close a cycle.
    from .rule_context import build_context

    context, extra = build_context(
        current_summary,
        previous_summary=previous_summary,
        recent_summaries=recent_summaries[1:],
        quality_findings=quality_findings,
        loop_min_repeats=loop_min_repeats,
        loop_task_min_repeats=loop_task_min_repeats,
        architecture_drift_runs=architecture_drift_runs,
    )
    extra["ignored"] = list(ignored_findings or [])
    alerts: list[dict] = []
    findings: list[dict] = []
    rules = _rule_set(
        float(insights_cfg.daily_spike_z_min),
        int(loop_min_repeats),
        int(loop_task_min_repeats),
        int(architecture_drift_runs),
    )
    for finding in evaluate_rules(rules, context, extra=extra):
        sink = str((finding.get("details") or {}).get("sink") or DEFAULT_SINK)
        if sink == "alerts":
            alerts.extend(_rule_alerts(finding))
        else:
            findings.append(_rule_maintenance_finding(finding))
    return alerts, findings, extra


# ------------------------------------------------------------------ pure compute


def compute(
    *,
    run_time: datetime,
    current_summary: dict,
    previous_summary: dict | None,
    current_digest: dict | None,
    previous_digest: dict | None,
    recent_summaries: list[dict],
    insights_cfg: InsightsConfig,
    ignored_findings: list[str],
    harness_ignored_rules: list[str] | None = None,
    quality_findings: Mapping[str, Any] | Sequence[Any] | None = None,
    architecture_drift_runs: int = ARCHITECTURE_DRIFT_RUNS_DEFAULT,
    loop_min_repeats: int = AGENT_LOOP_MIN_REPEATS_DEFAULT,
    loop_task_min_repeats: int = AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT,
) -> dict:
    """Pure Partie 6 computation. `recent_summaries` = summaries sorted newest-first
    (must include `current_summary` as the first element), for R1 and monthly/spike rules.

    `quality_findings` is the already-loaded ``weekly-quality-findings-<date>.json``
    payload (``run()`` reads it from disk); it feeds the declarative rules' ``findings``
    scope and defaults to "no audit" so a run without one still evaluates its rules.
    """
    period = current_summary["period"]
    cur_totals = current_summary.get("totals", {})
    prev_totals = (previous_summary or {}).get("totals") if previous_summary else None

    current_cost = cur_totals.get("total_cost_usd")
    previous_cost = prev_totals.get("total_cost_usd") if prev_totals else None
    current_cache = cur_totals.get("cache_hit_rate")
    previous_cache = prev_totals.get("cache_hit_rate") if prev_totals else None
    current_tokens = cur_totals.get("total_tokens")
    previous_tokens = prev_totals.get("total_tokens") if prev_totals else None

    cur_skills = {x["skill"] for x in current_summary.get("skill_usage", [])}
    prev_skills = (
        {x["skill"] for x in (previous_summary or {}).get("skill_usage", [])}
        if previous_summary
        else set()
    )

    # ---- lint deltas (null if either digest missing — never fail insights) ----
    lint_delta = _lint_delta_by_rule(current_digest, previous_digest, harness_ignored_rules)

    previous_run_date = None
    if previous_summary:
        previous_run_date = previous_summary["generated_at"][:10]

    # K7: écarts cross-check structurés (parts lifetime vs session_v2 lifetime).
    cost_discrepancies = _collect_cost_discrepancies(current_summary)

    deltas = {
        "cost_wow_pct": _pct_delta(current_cost, previous_cost),
        "cache_hit_rate_delta": round(current_cache - previous_cache, 4)
        if (current_cache is not None and previous_cache is not None)
        else None,
        "total_tokens_delta_pct": _pct_delta(current_tokens, previous_tokens),
        "skills": {
            "newly_loaded": sorted(cur_skills - prev_skills),
            "newly_silent": sorted(prev_skills - cur_skills),
        },
        "lint_violations_delta_by_rule": lint_delta,
    }
    # v5.31 (gap run 15j) : fenêtres de durées différentes → deltas de volume non comparables
    cur_hours = _window_hours(period)
    prev_hours = (
        _window_hours((previous_summary or {}).get("period", {})) if previous_summary else None
    )
    if cur_hours and prev_hours and abs(cur_hours - prev_hours) > 1:
        deltas["cost_wow_pct"] = None
        deltas["total_tokens_delta_pct"] = None
        deltas.setdefault("_warnings", []).append(
            f"fenêtre précédente ({prev_hours:.0f}h) ≠ courante ({cur_hours:.0f}h) — "
            "deltas de volume non comparables (cost_wow/tokens sautés)"
        )
    if current_digest is None:
        deltas.setdefault("_warnings", []).append(
            "digest harness absent — lint_violations_delta_by_rule à null, règle sautée"
        )

    # ---- alerts (seuils budgets/cache/WoW) ----
    alerts = _budget_spike_alerts(
        current_cost=current_cost,
        recent_summaries=recent_summaries,
        run_time=run_time,
        current_cache=current_cache,
        wow=deltas["cost_wow_pct"],
        insights_cfg=insights_cfg,
    )

    lint_max = _lint_max_alert(current_digest, harness_ignored_rules, insights_cfg)
    if lint_max is not None:
        alerts.append(lint_max)

    # ---- couverture lint (v6.0.n) : surfaces .opencode/ hors allowlist ----
    lint_coverage = _lint_coverage_alert(current_digest, insights_cfg)
    if lint_coverage is not None:
        alerts.append(lint_coverage)

    # ---- D5 : part des sessions exclues (fenêtre biaisée) + messages répétés ----
    exclusion_rate = _exclusion_rate_alert(current_summary, insights_cfg)
    if exclusion_rate is not None:
        alerts.append(exclusion_rate)

    # ---- maintenance R1-R4 (findings initialisés avant l'alerte cache K8) ----
    # The declarative rules come first: `evaluate_rules` sorts by rule id, so
    # `agent-loop` then `architecture-drift` — the historical finding order.
    rule_alerts, findings, rule_extra = _production_rule_results(
        current_summary=current_summary,
        previous_summary=previous_summary,
        recent_summaries=recent_summaries,
        insights_cfg=insights_cfg,
        ignored_findings=ignored_findings,
        quality_findings=quality_findings,
        architecture_drift_runs=architecture_drift_runs,
        loop_min_repeats=loop_min_repeats,
        loop_task_min_repeats=loop_task_min_repeats,
    )
    alerts.extend(rule_alerts)
    zero_alert, zero_finding = _cache_write_zero_alerts(recent_summaries, insights_cfg)
    if zero_alert is not None and zero_finding is not None:
        alerts.append(zero_alert)
        findings.append(zero_finding)
    _retire, never_loaded_consecutive = _retire_candidates(
        current_summary, recent_summaries, insights_cfg, ignored_findings, current_digest
    )
    findings.extend(_retire)

    _sim_min = (
        insights_cfg.skill_similarity_min if hasattr(insights_cfg, "skill_similarity_min") else 0.8
    )
    findings.extend(_merge_candidates(current_summary, ignored_findings, _sim_min))

    findings.extend(_token_risk_findings(current_summary, insights_cfg, ignored_findings))

    findings.extend(_harness_fix_findings(current_digest, harness_ignored_rules, ignored_findings))

    stats = {
        "runs_scanned": len(recent_summaries),
        "skills_in_catalog": current_summary.get("skill_catalog_count", DEFAULT_CATALOG_COUNT),
        "never_loaded_consecutive": dict(sorted(never_loaded_consecutive.items())),
        "spike_baseline_days": rule_extra.get("baseline_days", 0),
    }

    return {
        "schema_version": 1,
        "period": period,
        "previous_run_date": previous_run_date,
        "cost_discrepancies": cost_discrepancies,
        "generated_at": _iso(run_time),
        "deltas": deltas,
        "alerts": sorted(alerts, key=lambda a: (a["severity"] != "high", a["rule"])),
        "maintenance": {"findings": findings, "stats": stats},
    }


def _lint_max_alert(
    current_digest: dict | None,
    harness_ignored_rules: list[str] | None,
    insights_cfg: InsightsConfig,
) -> dict | None:
    """Alerte lint_violations_max, ou None si sous le seuil."""
    lint_total = _digest_violations(current_digest, harness_ignored_rules or [])
    if (
        current_digest is not None
        and lint_total is not None
        and lint_total > insights_cfg.lint_violations_max
    ):
        return {
            "rule": "lint_violations_max",
            "threshold": insights_cfg.lint_violations_max,
            "observed": lint_total,
            "unit": "findings",
            "severity": "medium",
        }
    return None


def _lint_coverage_alert(current_digest: dict | None, insights_cfg: InsightsConfig) -> dict | None:
    """Alerte lint_coverage (surfaces .opencode/ hors allowlist), ou None."""
    digest_scope = (current_digest or {}).get("harness_scope") or {}
    unscoped = digest_scope.get("unscoped_file_count")
    inspected_total = (current_digest or {}).get("inspection", {}).get("summary", {}).get("total")
    if (
        current_digest is not None
        and isinstance(unscoped, int)
        and isinstance(inspected_total, int)
        and inspected_total + unscoped > 0
    ):
        coverage = inspected_total / (inspected_total + unscoped)
        if coverage < insights_cfg.lint_coverage_min:
            return {
                "rule": "lint_coverage",
                "threshold": insights_cfg.lint_coverage_min,
                "observed": round(coverage, 2),
                "unit": "surfaces scannées",
                "note": f"{inspected_total} scannées, {unscoped} hors allowlist",
                "severity": "low",
            }
    return None


def _repeated_warning_messages(warnings: object) -> list[dict]:
    """Messages de warning répétés, groupés par message et décompte décroissant.

    D5 — le regroupement de ``report._group_warnings`` est *remonté* au niveau
    des alertes (et non réécrit : ``report`` importe ``insights``, l'inverse
    créerait un cycle d'import). Différence : le compte cumule le multiplicateur
    ``count`` posé par l'agrégateur, donc une entrée agrégée vaut N occurrences
    et non 1.
    """
    if not isinstance(warnings, list):
        return []
    grouped: dict[str, dict] = {}
    for w in warnings:
        if not isinstance(w, dict):
            continue
        msg = w.get("message", "")
        entry = grouped.setdefault(msg, {"message": msg, "count": 0})
        entry["count"] += w.get("count") or 1
    return [
        {"message": e["message"], "count": e["count"]}
        for e in sorted(grouped.values(), key=lambda e: (-e["count"], e["message"]))
        if e["count"] > 1
    ]


def _exclusion_rate_alert(current_summary: dict, insights_cfg: InsightsConfig) -> dict | None:
    """Alerte `exclusion_rate`, ou None si la part d'exclusions est tolérable.

    ``excluded_active / window_touched`` : au-delà du seuil, les totaux publiés
    omettent une part sizeable des sessions de la fenêtre. Silencieuse sur une
    fenêtre vide (``window_touched == 0``) — pas de division par zéro, pas
    d'alerte sur un run sans session.
    """
    selection = (current_summary or {}).get("selection")
    if not isinstance(selection, dict):
        return None
    touched = selection.get("window_touched")
    if not isinstance(touched, int) or touched <= 0:
        return None
    active = selection.get("excluded_active")
    if not isinstance(active, int):
        return None
    threshold = getattr(insights_cfg, "exclusion_rate_max", EXCLUSION_RATE_MAX_DEFAULT)
    rate = active / touched
    if rate <= threshold:
        return None
    alert = {
        "rule": "exclusion_rate",
        "threshold": threshold,
        "observed": round(rate, 4),
        "unit": "sessions exclues (active) / sessions touchées",
        "note": (
            f"{active} active(s), {selection.get('excluded_no_activity', 0)} no-activity, "
            f"{selection.get('excluded_unflushed', 0)} unflushed sur {touched} touchée(s)"
        ),
        "severity": "medium",
    }
    repeated = _repeated_warning_messages((current_summary or {}).get("warnings"))
    if repeated:
        alert["repeated_warnings"] = repeated
    return alert


def _cache_write_zero_alerts(
    recent_summaries: list[dict], insights_cfg: InsightsConfig
) -> tuple[dict | None, dict | None]:
    """Couple (alerte, finding) cache_write_tokens=0, ou (None, None)."""
    consecutive_zero_write = 0
    for s in recent_summaries:
        if (s.get("totals", {}) or {}).get("cache_write_tokens", 0) == 0:
            consecutive_zero_write += 1
        else:
            break
    if consecutive_zero_write >= insights_cfg.cache_write_zero_runs:
        return (
            {
                "rule": "cache_write_zero_runs",
                "threshold": insights_cfg.cache_write_zero_runs,
                "observed": consecutive_zero_write,
                "severity": "medium",
            },
            {
                "category": "fix-candidate",
                "severity": "medium",
                "description": (
                    f"cache_write_tokens=0 sur {consecutive_zero_write} run(s) consécutif(s) — "
                    "trou de télémétrie probable côté client"
                ),
                "recommendation": (
                    "vérifier la persistance du cache du client OpenCode (config/checkpoint) "
                    "avant d'interpréter les coûts"
                ),
                "recommendation_type": "cache-write-zero",
                "target": None,
            },
        )
    return None, None


def _digest_violations(digest: dict | None, ignored_rules: list[str] | None = None) -> int | None:
    if digest is None:
        return None
    ignored = set(ignored_rules or [])
    return sum(
        1
        for f in flatten_harness_findings(digest)
        if str(f.get("rule") or f.get("severity") or "unknown") not in ignored
    )


def flatten_harness_findings(digest: dict | None) -> list[dict]:
    """Normalize a real harness-eval 7.9.0 digest into a flat findings list.

    Projection of util.iter_digest_findings.  Real digests carry findings per
    component under `inspection.{command,claude_md,uncategorized}[i].findings`
    ({rule, severity, message}) — the top-level `findings` list is empty in
    practice (v5.28, P4.1).  Deduplication is PER COMPONENT (rule+message), so
    a rule violated in N files counts N times.  Non-pass `rules[]` entries are
    added as fallback only when no detailed finding exists for that rule
    anywhere.
    """
    out: list[dict] = []
    finding_rules: set[str] = set()
    fallback_seen: set[str] = set()
    seen_top: set[tuple[str, str]] = set()
    seen_components: dict[tuple[str, int], set[tuple[str, str]]] = {}
    for rec in iter_digest_findings(digest):
        if not rec["detailed"]:
            rule = str(rec["rule"])
            if rule in finding_rules or rule in fallback_seen:
                continue
            fallback_seen.add(rule)
            out.append({"rule": rule, "severity": "", "message": ""})
            continue
        key = (str(rec["rule"]), str(rec["message"]))
        seen = (
            seen_top
            if rec["section"] == "top"
            else seen_components.setdefault(
                (str(rec["section"]), int(rec["component_index"])), set()
            )
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "rule": str(rec["rule"]),
                "severity": str(rec["severity"]),
                "message": str(rec["message"]),
            }
        )
        finding_rules.add(str(rec["rule"]))
    return out


def _catalog_index(current_summary: dict) -> dict[str, dict]:
    """Index ``skill_catalog_entries`` par ``skill_id`` — construit une fois (D3).

    Clé absente (anciens artefacts) → index vide **et** `has_catalog=False` :
    les filtres qui en dépendent doivent rester inertes pour ne pas casser un
    summary pré-catlogue.
    """
    raw = current_summary.get("skill_catalog_entries")
    if not isinstance(raw, list):
        return {}
    index: dict[str, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        skill_id = entry.get("skill_id")
        if isinstance(skill_id, str) and skill_id:
            index[skill_id] = entry
    return index


def _catalog_description(entry: dict) -> str:
    """Description déclarée d'un skill de catalogue (vide si absente).

    Les ``skill_catalog_entries`` actuels ne portent pas de description ; le champ
    est lu dès qu'un producteur l'expose, ce qui active la branche « never load
    standalone » de `curation.is_reachable_skill` sans réécrire la règle.
    """
    for source in (entry, entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}):
        text = source.get("description")
        if isinstance(text, str) and text.strip():
            return text
    return ""


def _retire_candidates(
    current_summary: dict,
    recent_summaries: list[dict],
    insights_cfg: InsightsConfig,
    ignored_findings: list[str],
    current_digest: dict | None,
) -> tuple[list[dict], dict[str, int]]:
    """R1 retire-candidates — seuil **discriminant** (D3).

    Le critère précédent était « jamais chargé sur N runs consécutifs » avec
    ``N == len(recent_summaries)`` : c'est un **plafond de mesure**, pas un
    seuil de qualité — avec 8 runs d'historique au maximum, tout skill jamais
    chargé atteint 8/8, donc la règle ne discrimine plus rien (34 findings sur le
    run du 03/10, dont 12 sur des skills `ttl_policy=pin` que la curation protège
    déjà). Nouveau critère, cumulatif :

      1. **plancher de preuve** — ``len(runs) >= never_loaded_runs_threshold`` ;
         la constante garde un sens (« combien de runs de preuve exiger avant de
         juger ») au lieu d'être une cible de série ;
      2. **saturation** — ``count == len(runs)`` : absent sur *toute* la fenêtre
         observable. Une série partielle (7/8) n'est plus un signal de qualité ;
      3. **portée catalogue** — le skill doit exister dans
         ``skill_catalog_entries``, sinon il n'y a rien à supprimer et aucune
         métadonnée pour prouver qu'il est atteignable. Inerte si la clé est
         absente (rétro-compat) ;
      4. **protection** — ``ttl_policy != "pin"`` et
         ``curation.is_reachable_skill(entry, description)`` : la règle de
         protection unique du dépôt (origin=user, « never load standalone », et
         `ttl_policy=pin` lu par le même `_skill_fields`) n'est pas re-implémentée
         ici — elle est appelée.

    Mesuré sur ``weekly-summary-2026-10-03.json`` : 34 → 20 findings.
    """
    out: list[dict] = []
    runs = [s.get("skills_never_loaded", []) for s in recent_summaries]
    catalog = _catalog_index(current_summary)
    has_catalog = "skill_catalog_entries" in current_summary
    # (1) plancher de preuve : sans fenêtre suffisante, on ne juge pas.
    evidence_floor_met = len(runs) >= max(0, insights_cfg.never_loaded_runs_threshold)
    consecutive: dict[str, int] = {}
    if evidence_floor_met and current_summary.get("skills_never_loaded"):
        for skill in sorted(current_summary["skills_never_loaded"]):
            count = 0
            for run_skills in runs:
                if skill in run_skills:
                    count += 1
                else:
                    break
            consecutive[skill] = count
            if not count:
                continue
            # (2) saturation : la série doit couvrir la fenêtre entière.
            if count != len(runs):
                continue
            # (3) portée catalogue.
            if has_catalog and skill not in catalog:
                continue
            entry = catalog.get(skill, {})
            # (4) protection — règles partagées avec la curation (`_skill_fields`
            # normalise origin/ttl, `is_reachable_skill` couvre origin=user et
            # « never load standalone ») : aucune seconde règle ici.
            if _skill_fields(entry)[2] == "pin":
                continue
            if not is_reachable_skill(entry, _catalog_description(entry)):
                continue
            if not _ignored(ignored_findings, "skill-maintenance", skill):
                overlap = bool(
                    current_digest and current_digest.get("triggers", {}).get("overlaps")
                )
                severity = "high" if overlap else "medium"
                targets = (current_summary.get("skills_targets") or {}).get(skill, [])
                cible = f" (cible déclarée : {', '.join(targets)})" if targets else ""
                out.append(
                    {
                        "session_id": None,
                        "category": "retire-candidate",
                        "severity": severity,
                        "description": f"skill '{skill}' jamais chargé sur l'ensemble "
                        f"des {count} runs observés"
                        + cible
                        + (" + chevauchement de déclencheurs" if overlap else ""),
                        "evidence_summary": f"skills_never_loaded: {count}/{len(runs)} runs "
                        "(fenêtre saturée)"
                        + (" ; lint trigger-overlap présent" if overlap else ""),
                        "recommendation": f"Retirer .opencode/skills/{skill}/SKILL.md après revue",
                        "recommendation_type": "skill-maintenance",
                        "impact_order_of_magnitude": "small",
                    }
                )
    return out, consecutive


def _merge_candidates(
    current_summary: dict, ignored_findings: list[str], min_similarity: float = 0.8
) -> list[dict]:
    """R2 merge-candidates : paires de skills probablement redondantes."""
    out: list[dict] = []
    for pair in current_summary.get("skill_similar_pairs", []):
        skills = list(pair.get("skills", []))
        if not skills:
            continue
        target = skills[0]
        if _ignored(ignored_findings, "skill-maintenance", target):
            continue
        out.append(
            {
                "session_id": None,
                "category": "merge-candidate",
                "severity": "medium",
                "description": f"skills '{skills[0]}' et '{skills[1]}' probablement redondants",
                "evidence_summary": f"similarité difflib {pair.get('similarity', 0.0):.2f} ≥ {min_similarity}",
                "recommendation": "Fusion manuelle des deux SKILL.md après revue",
                "recommendation_type": "skill-maintenance",
                "impact_order_of_magnitude": "small",
            }
        )
    return out


def _token_risk_findings(
    current_summary: dict, insights_cfg: InsightsConfig, ignored_findings: list[str]
) -> list[dict]:
    """R3 token-risk : sessions dont le **coût** dépasse ``session_cost_cap``.

    D2 : le déclencheur est passé de ``total_tokens > session_token_cap`` à
    ``cost_usd > session_cost_cap``. Les tokens par dollar varient d'un modèle à
    l'autre (36 M tokens sur un modèle bon marché coûtent 0,61 $, pas 20 $), donc
    un cap en tokens ne dit rien du budget — il ne fait que reclasser les mêmes
    sessions selon le modèle qui les a produites. ``cost_usd`` est déjà lu dans la
    boucle pour l'affichage : le passage en coût **n'ajoute aucune I/O**.
    ``total_tokens`` reste dans la description/preuve comme contexte, plus comme
    seuil.
    """
    out: list[dict] = []
    cost_cap = insights_cfg.session_cost_cap
    for s in current_summary.get("top_sessions_by_cost", []):
        cost = _as_float(s.get("cost_usd"))
        if cost <= cost_cap:
            continue
        sid = s.get("session_id")
        if _ignored(ignored_findings, "token-risk", sid or ""):
            continue
        total = s.get("total_tokens") or 0
        out.append(
            {
                "session_id": sid,
                "category": "token-risk",
                "severity": "medium",
                "description": (
                    f"session {sid} : coût ${cost:.2f} > cap ${cost_cap:.2f} ({total:,} tokens)"
                ),
                "evidence_summary": f"top_sessions_by_cost: ${cost:.4f} ({total} tokens)",
                "recommendation": (
                    "réduire le context-bloat (lectures répétées de gros fichiers) "
                    "et les loops swarm silent-empty (worker task_result vide)"
                ),
                "recommendation_type": "token-budget",
                "impact_order_of_magnitude": "medium",
            }
        )
    return out


def _harness_fix_findings(
    current_digest: dict | None,
    harness_ignored_rules: list[str] | None,
    ignored_findings: list[str],
) -> list[dict]:
    """R4/R5 harness-fix : violations digest, report-only jamais automatique."""
    out: list[dict] = []
    trivial_kw = ("frontmatter", "description", "missing", "invalid")
    seen_rules: set[str] = set()
    if isinstance(current_digest, dict):
        for f in flatten_harness_findings(current_digest):
            if str(f.get("rule") or "") in set(harness_ignored_rules or []):
                continue
            if not isinstance(f, dict):
                continue
            rule = str(f.get("rule") or f.get("id") or "unknown")
            message = str(f.get("message") or f.get("detail") or "")
            if rule in seen_rules:
                continue
            seen_rules.add(rule)
            low = any(k in rule.lower() or k in message.lower() for k in trivial_kw)
            if _ignored(ignored_findings, "harness-fix", rule):
                continue
            out.append(
                {
                    "session_id": None,
                    "category": "fix-candidate",
                    "severity": "low" if low else "medium",
                    "description": f"violation harness '{rule}'"
                    + ("" if not low else " (format triviale)"),
                    "evidence_summary": message[:200] or f"{rule}: {f.get('severity', '')}",
                    "recommendation": "Correction manuelle (R4: corrigeable en auto-fix v2 ; R5: jamais automatique)",
                    "recommendation_type": "harness-fix",
                    "impact_order_of_magnitude": "small",
                }
            )
    return out


def _ignored(ignored: list[str], category: str, target: str) -> bool:
    ident = f"{category}:{target}"
    return ident in ignored or target in ignored


def _as_float(value: object) -> float:
    """Float défensif — un champ absent, ``null`` ou non numérique vaut 0.0."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------------------ I/O wrapper


def _state_previous(output_dir: Path, current_date: str) -> tuple[dict | None, dict | None]:
    """Previous summary+digest from the `previous_run.json` state (v5.28, P1.2).

    Strictly older than the current run; falls back to None when absent/stale.
    """
    state = _load(output_dir / "previous_run.json")
    if not state or str(state.get("run_date", "")) >= current_date:
        return None, None
    # v6.0.k (F1): previous_run.json may point into a runs/<id>/ directory.
    base = output_dir
    run_dir = state.get("run_dir")
    if isinstance(run_dir, str) and run_dir:
        candidate = output_dir / run_dir
        if candidate.is_dir():
            base = candidate
    prev_sum = _load(base / state["summary_file"]) if state.get("summary_file") else None
    prev_dig = _load(base / state["digest_file"]) if state.get("digest_file") else None
    if prev_sum is None and prev_dig is None:
        return None, None
    return prev_sum, prev_dig


def _pattern_paths(output_dir: Path, pattern: str) -> list[Path]:
    """Sorted candidates for a dated artifact: root, run dirs, migrated legacy dirs.

    v6.0.l (E4) migrates pre-v6.0.k root artifacts into ``runs/<id>/legacy/`` —
    without this third glob a migrated baseline summary/digest becomes invisible
    to insights and the WoW delta is silently lost on the first run after
    migration (C2).
    """
    return sorted(
        [
            *output_dir.glob(pattern),
            *output_dir.glob(f"{RUNS_DIR}/*/{pattern}"),
            *output_dir.glob(f"{RUNS_DIR}/*/legacy/{pattern}"),
        ]
    )


def _artifacts_before(output_dir: Path, pattern: str, current_date: str) -> list[tuple[str, Path]]:
    """Sorted (date, path) artefacts matching <pattern>, strictly older than <date>.

    Date extracted via regex so the pattern works for summary AND harness
    digests (v5.28: the previous prefix-strip only matched `weekly-summary-`,
    silently degrading lint deltas when only a digest was present).
    """
    found = []
    for path in _pattern_paths(output_dir, pattern):
        m = re.search(r"(\d{4}-\d{2}-\d{2})\.json$", path.name)
        if m and m.group(1) < current_date:
            found.append((m.group(1), path))
    return sorted(found)


def _fallback_previous_artifact(
    output_dir: Path, pattern: str, exclude_dir: Path | None = None
) -> dict | None:
    """Repli même-date hors run courant (back-to-back runs, tests, reruns — extrait de _discover_previous, CCN-3)."""
    eligible = []
    for path in _pattern_paths(output_dir, pattern):
        if exclude_dir is not None and path.parent == exclude_dir:
            continue
        m = re.search(r"(\d{4}-\d{2}-\d{2})\.json$", path.name)
        if m:
            eligible.append((m.group(1), path))
    if not eligible:
        return None
    return _load(sorted(eligible)[-1][1])


def _discover_previous(
    pattern: str, current_date: str, output_dir: Path, exclude_dir: Path | None = None
) -> dict | None:
    """Most recent artefact usable as previous run (spec §2).

    Primary: artefacts strictly older than the current run date. Fallback
    (v6.0.n): most recent same-date artefact outside the current run dir —
    covers back-to-back runs sharing the same anchor day (tests, reruns),
    which the date-strict search silently skipped.
    """
    found = _artifacts_before(output_dir, pattern, current_date)
    if found:
        return _load(found[-1][1])
    return _fallback_previous_artifact(output_dir, pattern, exclude_dir)


def _validated_digest(out: Path, date: str) -> dict | None:
    """Charge le digest harness du run et le dégrade à None si invalide (warnings stderr)."""
    current_digest = _load(out / f"weekly-harness-digest-{date}.json")
    for digest_problem in harness_digest_problems(current_digest):
        print(
            f"insights: WARNING: {digest_problem} — volet harness dégradé",
            file=sys.stderr,
            flush=True,
        )
        current_digest = None
    return current_digest


def _baseline_fallback(
    previous: dict | None,
    baseline_summary_path: str | None,
    cfg: TelemetryConfig,
    date: str,
) -> tuple[dict | None, str | None]:
    """Repli baseline explicite (P1.1) quand aucun previous découvert, ou (previous, None)."""
    baseline_used: str | None = None
    if previous is None and (baseline_summary_path or cfg.baseline_summary_path):
        bp = Path(baseline_summary_path or cfg.baseline_summary_path or "").expanduser()
        if bp.is_file():
            loaded = _load(bp)
            if loaded and str(loaded.get("generated_at", ""))[:10] < date:
                previous, baseline_used = loaded, str(bp)
    return previous, baseline_used


def _collect_recent(root: Path, current: dict, date: str, limit: int = 8) -> list[dict]:
    """Fenêtre recent_summaries (current + précédents, newest-first, cap limit)."""
    recent = [current]
    for _d, p in _artifacts_before(root, "weekly-summary-*.json", date)[::-1]:
        if len(recent) >= limit:
            break
        loaded = _load(p)
        if loaded is not None:
            recent.append(loaded)
    return recent


def _persist_baseline(
    out: Path,
    date: str,
    current_path: Path,
    current_digest: dict | None,
    previous: dict | None,
    baseline_used: str | None,
    data: dict,
) -> None:
    """Auto-baseline K11 (premier run) ou rattachement du baseline explicite."""
    if baseline_used:
        data["baseline_summary_file"] = baseline_used
    elif previous is None:
        data["baseline"] = "first-run"
        write_json_atomic(
            out / f"weekly-baseline-{date}.json",
            {
                "schema_version": 1,
                "run_date": date,
                "summary_file": current_path.name,
                "digest_file": f"weekly-harness-digest-{date}.json"
                if current_digest is not None
                else None,
            },
        )


def run(
    cfg: TelemetryConfig,
    *,
    anchor: str | None = None,
    baseline_summary_path: str | None = None,
) -> int:
    run_time = _parse_anchor(anchor)
    date = run_time.strftime("%Y-%m-%d")
    root = cfg.output_dir
    out = resolve_active_run_dir(root, date)

    current_path = out / f"weekly-summary-{date}.json"
    current = _load(current_path)
    if current is None:
        print(
            f"insights: FATAL: summary inexistante {current_path} — lancer run d'abord",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_TOTAL_FAILURE

    # Previous discovery: state file first (P1.2), glob fallback, then explicit
    # baseline summary (P1.1) — first run with real trends.
    state_summary, state_digest = _state_previous(root, date)
    previous = (
        state_summary
        if state_summary is not None
        else _discover_previous("weekly-summary-*.json", date, root, exclude_dir=out)
    )
    current_digest = _validated_digest(out, date)
    previous_digest = (
        state_digest
        if state_digest is not None
        else _discover_previous("weekly-harness-digest-*.json", date, root, exclude_dir=out)
    )
    previous, baseline_used = _baseline_fallback(previous, baseline_summary_path, cfg, date)

    recent = _collect_recent(root, current, date)

    # The `findings` rule scope needs the audit payload; absent or unreadable is
    # an empty scope (never an exception), so a run without an audit still works.
    quality_findings = _load(out / f"weekly-quality-findings-{date}.json")

    data = compute(
        run_time=run_time,
        current_summary=current,
        previous_summary=previous,
        current_digest=current_digest,
        previous_digest=previous_digest,
        recent_summaries=recent,
        insights_cfg=cfg.insights,
        ignored_findings=cfg.ignored_findings,
        harness_ignored_rules=cfg.harness_ignored_rules,
        quality_findings=quality_findings,
    )
    if previous_digest is None and current_digest is None:
        data["deltas"]["_warnings"] = [
            "aucun digest harness disponible (deltas lint à null, règle sautée)"
        ]

    _persist_baseline(out, date, current_path, current_digest, previous, baseline_used, data)

    out_path = out / f"weekly-insights-{date}.json"
    write_json_atomic(out_path, data)
    # P1.2: persist discovery state for the next run (survives file renames/purges).
    previous_state: dict = {
        "schema_version": 2,
        "run_date": date,
        "summary_file": current_path.name,
        "digest_file": f"weekly-harness-digest-{date}.json" if current_digest is not None else None,
        "insights_file": out_path.name,
    }
    run_meta = active_run_meta(root, date)
    if run_meta:
        previous_state["run_id"] = run_meta["run_id"]
        previous_state["run_dir"] = run_meta["run_dir"]
    write_json_atomic(root / "previous_run.json", previous_state)
    print(
        f"insights: alerts={len(data['alerts'])} maintenance={len(data['maintenance']['findings'])} "
        f"previous={data['previous_run_date']} baseline={baseline_used or 'none'} file={out_path}",
        flush=True,
    )
    return EXIT_OK
