"""Insights: deltas, alerts and maintenance rules R1-R4 (Partie 6) — 100 % déterministe, zéro LLM.

`compute()` is pure (testable with fixture dicts); `run()` does I/O:
previous-run discovery by glob (spec §2, "règle de code, pas d'hypothèse"),
JSON loading, atomic write.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .config import InsightsConfig, TelemetryConfig
from .harness_scope import harness_digest_problems
from .main import EXIT_OK, EXIT_TOTAL_FAILURE
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


def _spike_baseline(recent_summaries: list[dict]) -> tuple[list[float], int]:
    """Baseline quotidienne hors run courant (zéro ignoré)."""
    costs: list[float] = []
    days = 0
    for s in recent_summaries[1:]:
        for d in s.get("daily_totals", []):
            cost = float(d.get("cost_usd", 0.0))
            if cost == 0:
                continue
            costs.append(cost)
            days += 1
    return costs, days


def _daily_spike_alerts(
    current_summary: dict,
    baseline: list[float],
    z_min: float,
) -> list[dict]:
    """Un alert par jour courant anormal vs baseline (z robuste, borné)."""
    alerts: list[dict] = []
    if not baseline:
        return alerts
    for day in current_summary.get("daily_totals", []):
        cost = float(day.get("cost_usd", 0.0))
        if cost == 0:
            continue
        combined = baseline + [cost]
        zmap = {
            round(c, 4): z for c, z in zip(combined, _robust_z_scores(combined), strict=False)
        }
        raw_z = zmap.get(round(cost, 4), 0.0)
        if raw_z >= z_min:
            z = min(raw_z, DAILY_SPIKE_Z_CAP)
            alerts.append(
                {
                    "rule": "daily_spike_z_min",
                    "threshold": z_min,
                    "observed": round(z, 2),
                    "severity": "medium",
                    "day": day.get("date"),
                    "note": "MAD≈0, z borné" if raw_z > DAILY_SPIKE_Z_CAP else "",
                }
            )
    return alerts


def _budget_spike_alerts(
    *,
    current_cost: float | None,
    recent_summaries: list[dict],
    run_time: datetime,
    current_summary: dict,
    current_cache: float | None,
    wow: float | None,
    insights_cfg: InsightsConfig,
) -> tuple[list[dict], int]:
    """Alertes seuils budgets/spike/cache/WoW (extrait de compute L262-318, CCN-16).

    Renvoie (alertes, spike_baseline_days) — les jours de baseline restent
    exposés dans la sortie compute.
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

    baseline_costs, baseline_days = _spike_baseline(recent_summaries)
    alerts.extend(
        _daily_spike_alerts(current_summary, baseline_costs, insights_cfg.daily_spike_z_min)
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
    return alerts, baseline_days


# ------------------------------------------------------------------ pure compute


def _agent_loop_findings(
    current_summary: dict,
    *,
    loop_min_repeats: int,
    loop_task_min_repeats: int,
    ignored_findings: list[str],
) -> list[dict]:
    """Findings agent-loop : outil répété avec la même empreinte (extrait de compute, CCN-4)."""
    findings: list[dict] = []
    # F5: fingerprints are additive, so old summaries simply produce no finding.
    arg_fingerprints = current_summary.get("tool_argument_fingerprints", {})
    result_fingerprints = current_summary.get("tool_result_fingerprints", {})
    for tool in sorted(set(arg_fingerprints) | set(result_fingerprints)):
        arg_repeats = max((int(v) for v in (arg_fingerprints.get(tool) or {}).values()), default=0)
        result_repeats = max(
            (int(v) for v in (result_fingerprints.get(tool) or {}).values()), default=0
        )
        threshold = loop_task_min_repeats if tool == "task" else loop_min_repeats
        repeats = max(arg_repeats, result_repeats)
        if repeats < threshold or _ignored(ignored_findings, "agent-loop", tool):
            continue
        findings.append(
            {
                "session_id": None,
                "category": "agent-loop",
                "severity": "medium",
                "description": f"outil '{tool}' répété avec la même empreinte ({repeats} occurrences)",
                "evidence_summary": f"tool={tool}; repeats={repeats}; threshold={threshold}",
                "recommendation": "inspecter les résultats et borner les re-spawns/lectures répétées",
                "recommendation_type": "agent-loop",
                "impact_order_of_magnitude": "medium",
            }
        )
    return findings


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
    architecture_drift_runs: int = ARCHITECTURE_DRIFT_RUNS_DEFAULT,
    loop_min_repeats: int = AGENT_LOOP_MIN_REPEATS_DEFAULT,
    loop_task_min_repeats: int = AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT,
) -> dict:
    """Pure Partie 6 computation. `recent_summaries` = summaries sorted newest-first
    (must include `current_summary` as the first element), for R1 and monthly/spike rules.
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

    # ---- alerts (seuils budgets/spike/cache/WoW) ----
    alerts, baseline_days = _budget_spike_alerts(
        current_cost=current_cost,
        recent_summaries=recent_summaries,
        run_time=run_time,
        current_summary=current_summary,
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

    # ---- maintenance R1-R4 (findings initialisés avant l'alerte cache K8) ----
    findings = _agent_loop_findings(
        current_summary,
        loop_min_repeats=loop_min_repeats,
        loop_task_min_repeats=loop_task_min_repeats,
        ignored_findings=ignored_findings,
    )
    # Observation-only architecture/config drift.  This intentionally emits a
    # report finding, not an alert that can block CI or trigger curation/apply.
    drift_finding = _architecture_drift_finding(
        current_summary, previous_summary, recent_summaries, architecture_drift_runs
    )
    if drift_finding is not None:
        findings.append(drift_finding)
    zero_alert, zero_finding = _cache_write_zero_alerts(recent_summaries, insights_cfg)
    if zero_alert is not None and zero_finding is not None:
        alerts.append(zero_alert)
        findings.append(zero_finding)
    _retire, never_loaded_consecutive = _retire_candidates(
        current_summary, recent_summaries, insights_cfg, ignored_findings, current_digest
    )
    findings.extend(_retire)

    _sim_min = (
        insights_cfg.skill_similarity_min
        if hasattr(insights_cfg, "skill_similarity_min")
        else 0.8
    )
    findings.extend(_merge_candidates(current_summary, ignored_findings, _sim_min))

    findings.extend(_token_risk_findings(current_summary, insights_cfg, ignored_findings))

    findings.extend(
        _harness_fix_findings(current_digest, harness_ignored_rules, ignored_findings)
    )

    stats = {
        "runs_scanned": len(recent_summaries),
        "skills_in_catalog": current_summary.get("skill_catalog_count", DEFAULT_CATALOG_COUNT),
        "never_loaded_consecutive": dict(sorted(never_loaded_consecutive.items())),
        "spike_baseline_days": baseline_days,
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


def _lint_coverage_alert(
    current_digest: dict | None, insights_cfg: InsightsConfig
) -> dict | None:
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


def _architecture_drift_finding(
    current_summary: dict,
    previous_summary: dict | None,
    recent_summaries: list[dict],
    architecture_drift_runs: int,
) -> dict | None:
    """Finding architecture-drift observation-only, ou None si pas de dérive persistante."""
    current_arch = _architecture_observation(current_summary)
    previous_arch = _architecture_observation(previous_summary)
    drift_fields = _architecture_drift(current_arch, previous_arch)
    drift_runs = 1 if drift_fields else 0
    for summary in recent_summaries[1:]:
        earlier = _architecture_observation(summary)
        if not drift_fields or not _architecture_drift(current_arch, earlier):
            break
        drift_runs += 1
    drift_threshold = max(1, int(architecture_drift_runs))
    if drift_fields and drift_runs >= drift_threshold:
        return {
            "session_id": None,
            "category": "architecture-drift",
            "severity": "low",
            "description": "configuration/architecture observations changed: "
            + ", ".join(drift_fields),
            "evidence_summary": (
                f"watch-context fields={','.join(drift_fields)}; "
                f"consecutive_runs={drift_runs}/{drift_threshold}"
            ),
            "recommendation": "Review declared/observed/absent state and harness scope manually",
            "recommendation_type": "architecture-drift",
            "action": "recalibrate",
            "observation_only": True,
        }
    return None


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


def _retire_candidates(
    current_summary: dict,
    recent_summaries: list[dict],
    insights_cfg: InsightsConfig,
    ignored_findings: list[str],
    current_digest: dict | None,
) -> tuple[list[dict], dict[str, int]]:
    """R1 retire-candidates : skills jamais chargés N runs consécutifs."""
    out: list[dict] = []
    runs = [s.get("skills_never_loaded", []) for s in recent_summaries]
    consecutive: dict[str, int] = {}
    if current_summary.get("skills_never_loaded"):
        for skill in sorted(current_summary["skills_never_loaded"]):
            count = 0
            for run_skills in runs:
                if skill in run_skills:
                    count += 1
                else:
                    break
            consecutive[skill] = count
            if count >= insights_cfg.never_loaded_runs_threshold and not _ignored(
                ignored_findings, "skill-maintenance", skill
            ):
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
                        "description": f"skill '{skill}' jamais chargé sur {count} runs consécutifs"
                        + cible
                        + (" + chevauchement de déclencheurs" if overlap else ""),
                        "evidence_summary": f"skills_never_loaded: {count}/{len(runs)} runs"
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
    """R3 token-risk : sessions top-coût au-delà du cap de tokens."""
    out: list[dict] = []
    token_cap = insights_cfg.session_token_cap
    for s in current_summary.get("top_sessions_by_cost", []):
        total = s.get("total_tokens") or 0
        if total > token_cap:
            sid = s.get("session_id")
            if _ignored(ignored_findings, "token-risk", sid or ""):
                continue
            out.append(
                {
                    "session_id": sid,
                    "category": "token-risk",
                    "severity": "medium",
                    "description": (
                        f"session {sid} : {total:,} tokens > cap {token_cap:,} "
                        f"(coût ${s.get('cost_usd', 0.0):.2f})"
                    ),
                    "evidence_summary": f"top_sessions_by_cost: {total} tokens",
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
