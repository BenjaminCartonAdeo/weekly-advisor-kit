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
from .main import EXIT_OK, EXIT_TOTAL_FAILURE
from .run_state import RUNS_DIR, active_run_meta, resolve_active_run_dir
from .util import iso as _iso
from .util import load_json as _load
from .util import parse_anchor as _parse_anchor
from .util import period_hours, robust_z
from .writer import write_json_atomic

DEFAULT_CATALOG_COUNT = 0

#: plafond du z-score daily_spike — un MAD≈0 produit des z astronomiques
#: (ex. 99,19) qui sont un artefact, pas un signal plus fort (v5.31).
DAILY_SPIKE_Z_CAP = 10.0


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
    lint_delta: dict | None = None
    if current_digest is not None and previous_digest is not None:
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
        lint_delta = {
            r: cur_counts.get(r, 0) - prev_counts.get(r, 0)
            for r in sorted(set(cur_counts) | set(prev_counts))
        }

    previous_run_date = None
    if previous_summary:
        previous_run_date = previous_summary["generated_at"][:10]

    # K7: écarts cross-check structurés (parts lifetime vs session_v2 lifetime).
    cost_discrepancies: list[dict] = []
    for w in current_summary.get("warnings", []):
        msg = w.get("message", "")
        if "cross-check mismatch" not in msg:
            continue
        parts_cost = w.get("parts_cost")
        session_v2_cost = w.get("session_v2_cost")
        if parts_cost is None or session_v2_cost is None:
            continue
        cost_discrepancies.append(
            {
                "session_id": w.get("session_id"),
                "parts_cost_usd": parts_cost,
                "session_v2_cost_usd": session_v2_cost,
            }
        )

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

    # ---- alerts ----
    alerts: list[dict] = []
    if current_cost is not None and current_cost > insights_cfg.weekly_budget_usd:
        alerts.append(
            {
                "rule": "weekly_budget_usd",
                "threshold": insights_cfg.weekly_budget_usd,
                "observed": round(current_cost, 4),
                "severity": "high",
            }
        )

    month_start = run_time - timedelta(days=30)
    month_cost = 0.0
    for s in recent_summaries:
        try:
            gen = datetime.fromisoformat(s["generated_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            gen = run_time
        if month_start <= gen <= run_time:
            month_cost += s.get("totals", {}).get("total_cost_usd", 0.0)
    if month_cost > insights_cfg.monthly_budget_usd:
        alerts.append(
            {
                "rule": "monthly_budget_usd",
                "threshold": insights_cfg.monthly_budget_usd,
                "observed": round(month_cost, 4),
                "severity": "high",
            }
        )

    baseline_costs, baseline_days = [], 0
    for s in recent_summaries[1:]:
        for d in s.get("daily_totals", []):
            baseline_costs.append(float(d.get("cost_usd", 0.0)))
            baseline_days += 1
    if baseline_costs:
        baseline_list = baseline_costs
        # z of each current day vs the baseline distribution:
        for day in current_summary.get("daily_totals", []):
            cost = float(day.get("cost_usd", 0.0))
            if cost == 0:
                continue
            combined = baseline_list + [cost]
            zmap = {
                round(c, 4): z for c, z in zip(combined, _robust_z_scores(combined), strict=False)
            }
            raw_z = zmap.get(round(cost, 4), 0.0)
            if raw_z >= insights_cfg.daily_spike_z_min:
                z = min(raw_z, DAILY_SPIKE_Z_CAP)
                alerts.append(
                    {
                        "rule": "daily_spike_z_min",
                        "threshold": insights_cfg.daily_spike_z_min,
                        "observed": round(z, 2),
                        "severity": "medium",
                        "day": day.get("date"),
                        "note": "MAD≈0, z borné" if raw_z > DAILY_SPIKE_Z_CAP else "",
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

    wow = deltas["cost_wow_pct"]
    if wow is not None and wow > insights_cfg.cost_wow_pct_max:
        alerts.append(
            {
                "rule": "cost_wow_pct_max",
                "threshold": insights_cfg.cost_wow_pct_max,
                "observed": wow,
                "severity": "medium",
            }
        )

    lint_total = _digest_violations(current_digest, harness_ignored_rules or [])
    if (
        current_digest is not None
        and lint_total is not None
        and lint_total > insights_cfg.lint_violations_max
    ):
        alerts.append(
            {
                "rule": "lint_violations_max",
                "threshold": insights_cfg.lint_violations_max,
                "observed": lint_total,
                "severity": "medium",
            }
        )

    # ---- maintenance R1-R4 (findings initialisés avant l'alerte cache K8) ----
    findings: list[dict] = []
    consecutive_zero_write = 0
    for s in recent_summaries:
        if (s.get("totals", {}) or {}).get("cache_write_tokens", 0) == 0:
            consecutive_zero_write += 1
        else:
            break
    if consecutive_zero_write >= insights_cfg.cache_write_zero_runs:
        alerts.append(
            {
                "rule": "cache_write_zero_runs",
                "threshold": insights_cfg.cache_write_zero_runs,
                "observed": consecutive_zero_write,
                "severity": "medium",
            }
        )
        findings.append(
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
            }
        )
    runs = [s.get("skills_never_loaded", []) for s in recent_summaries]
    never_loaded_consecutive: dict[str, int] = {}
    if current_summary.get("skills_never_loaded"):
        for skill in sorted(current_summary["skills_never_loaded"]):
            count = 0
            for run_skills in runs:
                if skill in run_skills:
                    count += 1
                else:
                    break
            never_loaded_consecutive[skill] = count
            if count >= insights_cfg.never_loaded_runs_threshold and not _ignored(
                ignored_findings, "skill-maintenance", skill
            ):
                overlap = bool(
                    current_digest and current_digest.get("triggers", {}).get("overlaps")
                )
                severity = "high" if overlap else "medium"
                targets = (current_summary.get("skills_targets") or {}).get(skill, [])
                cible = f" (cible déclarée : {', '.join(targets)})" if targets else ""
                findings.append(
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

    for pair in current_summary.get("skill_similar_pairs", []):
        skills = list(pair.get("skills", []))
        if not skills:
            continue
        target = skills[0]
        if _ignored(ignored_findings, "skill-maintenance", target):
            continue
        findings.append(
            {
                "session_id": None,
                "category": "merge-candidate",
                "severity": "medium",
                "description": f"skills '{skills[0]}' et '{skills[1]}' probablement redondants",
                "evidence_summary": f"similarité difflib {pair.get('similarity', 0.0):.2f} ≥ {insights_cfg.skill_similarity_min if hasattr(insights_cfg, 'skill_similarity_min') else 0.8}",
                "recommendation": "Fusion manuelle des deux SKILL.md après revue",
                "recommendation_type": "skill-maintenance",
                "impact_order_of_magnitude": "small",
            }
        )

    # R4/R5 from lint digest findings (v1: report only, never automatic)
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
            findings.append(
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

    stats = {
        "runs_scanned": len(runs),
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

    Real digests carry findings per component under
    `inspection.{command,claude_md,uncategorized}[i].findings` ({rule, severity,
    message}) — the top-level `findings` list is empty in practice (v5.28, P4.1).
    Deduplication is PER COMPONENT (rule+message), so a rule violated in N files
    counts N times. Non-pass `rules[]` entries are added as fallback only when no
    detailed finding exists for that rule anywhere.
    """
    if not isinstance(digest, dict):
        return []
    out: list[dict] = []
    finding_rules: set[str] = set()
    fallback_seen: set[str] = set()

    def _from_finding(f: dict, seen: set[tuple[str, str]]) -> None:
        rule = str(f.get("rule") or f.get("id") or "unknown")
        key = (rule, str(f.get("message") or f.get("detail") or ""))
        if key in seen:
            return
        seen.add(key)
        out.append(
            {
                "rule": rule,
                "severity": str(f.get("severity") or ""),
                "message": str(f.get("message") or f.get("detail") or ""),
            }
        )
        finding_rules.add(rule)

    seen_top: set[tuple[str, str]] = set()
    for f in digest.get("findings", []) if isinstance(digest.get("findings"), list) else []:
        if isinstance(f, dict):
            _from_finding(f, seen_top)
    for section in ("command", "claude_md", "uncategorized"):
        comps = digest.get("inspection", {}).get(section)
        if not isinstance(comps, list):
            continue
        for comp in comps:
            if not isinstance(comp, dict):
                continue
            seen_comp: set[tuple[str, str]] = set()
            for f in comp.get("findings", []) if isinstance(comp.get("findings"), list) else []:
                if isinstance(f, dict):
                    _from_finding(f, seen_comp)
            for r in comp.get("rules", []) if isinstance(comp.get("rules"), list) else []:
                if isinstance(r, dict) and r.get("result") not in (None, "pass"):
                    rule = str(r.get("rule") or "unknown")
                    if rule in finding_rules or rule in fallback_seen:
                        continue
                    fallback_seen.add(rule)
                    out.append({"rule": rule, "severity": "", "message": ""})
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


def _artifacts_before(output_dir: Path, pattern: str, current_date: str) -> list[tuple[str, Path]]:
    """Sorted (date, path) artefacts matching <pattern>, strictly older than <date>.

    Date extracted via regex so the pattern works for summary AND harness
    digests (v5.28: the previous prefix-strip only matched `weekly-summary-`,
    silently degrading lint deltas when only a digest was present).
    """
    found = []
    candidates = [*output_dir.glob(pattern), *output_dir.glob(f"{RUNS_DIR}/*/{pattern}")]
    for path in sorted(candidates):
        m = re.search(r"(\d{4}-\d{2}-\d{2})\.json$", path.name)
        if m and m.group(1) < current_date:
            found.append((m.group(1), path))
    return sorted(found)


def _discover_previous(pattern: str, current_date: str, output_dir: Path) -> dict | None:
    """Most recent artefact strictly older than the current run (spec §2)."""
    found = _artifacts_before(output_dir, pattern, current_date)
    if not found:
        return None
    return _load(found[-1][1])


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
        else _discover_previous("weekly-summary-*.json", date, root)
    )
    current_digest = _load(out / f"weekly-harness-digest-{date}.json")
    previous_digest = (
        state_digest
        if state_digest is not None
        else _discover_previous("weekly-harness-digest-*.json", date, root)
    )
    baseline_used: str | None = None
    if previous is None and (baseline_summary_path or cfg.baseline_summary_path):
        bp = Path(baseline_summary_path or cfg.baseline_summary_path or "").expanduser()
        if bp.is_file():
            loaded = _load(bp)
            if loaded and str(loaded.get("generated_at", ""))[:10] < date:
                previous, baseline_used = loaded, str(bp)

    recent = [current]
    for _d, p in _artifacts_before(root, "weekly-summary-*.json", date)[::-1]:
        if len(recent) >= 8:
            break
        loaded = _load(p)
        if loaded is not None:
            recent.append(loaded)

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

    if baseline_used:
        data["baseline_summary_file"] = baseline_used
    elif previous is None:
        # K11: auto-baseline — premier run tracé (pas de tendance possible).
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
