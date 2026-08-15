"""Candidate selection for the LLM stages (Partie 3 §2 / Partie 4 §3) — pur.

The agent never picks sessions by judgment: audit candidates come from the
Part 1 JSON signals (top cost, outliers, loop, cache gap, repeated prompts),
deduplicated and priority-ordered; draft candidates come from the Part 3
findings archive (skill-candidate / command-candidate), capped by
`max_candidates_per_run`. Everything here is deterministic and unit-testable.
"""

from __future__ import annotations

#: types transmis à la Partie 4 pour drafting — création OU amélioration d'une
#: commande existante (v5.30, E : pattern coûteux lancé par une commande).
_DRAFT_TYPES = {"skill-candidate", "command-candidate", "command-improvement"}
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def select_audit_candidates(
    summary: dict,
    *,
    top_sessions_limit: int = 5,
    cost_per_active_minute_min: float = 0.5,
    cache_efficiency_gap: float = 0.2,
) -> list[dict]:
    """Ordered audit candidates (Partie 3 §2), priority: top cost, outliers,
    loop, cache gap, repeated prompts. One entry per session, reasons merged,
    order = priority of the first signal. NOT capped here — the caller slices
    by `audit_max_sessions` (audited vs unaudited).
    """
    ordered: list[dict] = []
    index: dict[str, int] = {}

    def _add(session_id: str, reason: str) -> None:
        if not session_id:
            return
        if session_id in index:
            entry = ordered[index[session_id]]
            if reason not in entry["reasons"]:
                entry["reasons"].append(reason)
            return
        index[session_id] = len(ordered)
        ordered.append({"session_id": session_id, "reasons": [reason]})

    top = summary.get("top_sessions_by_cost", [])
    for s in top[: max(0, top_sessions_limit)]:
        _add(str(s.get("session_id") or ""), "top-cost")
    for o in summary.get("cost_outliers", []):
        _add(str(o.get("session_id") or ""), "cost-outlier")
    weekly_cache = (summary.get("totals") or {}).get("cache_hit_rate")
    for s in top:
        cpm = s.get("cost_per_active_minute")
        if cpm is not None and cpm >= cost_per_active_minute_min:
            _add(str(s.get("session_id") or ""), "loop")
        ce = s.get("cache_efficiency")
        if weekly_cache is not None and ce is not None and ce < weekly_cache - cache_efficiency_gap:
            _add(str(s.get("session_id") or ""), "cache-gap")
    for r in summary.get("user_prompt_repeats", []):
        _add(str(r.get("session_id") or ""), "repeated-prompts")
    return ordered


def select_draft_candidates(findings: dict | None, *, max_candidates: int = 3) -> list[dict]:
    """skill/command-candidate/command-improvement findings, severity DESC (tiebreak session_id),
    capped at `max_candidates` (Partie 4 §3 — plafond combiné skills+commands).
    """
    if not findings:
        return []
    candidates = [
        f for f in findings.get("findings", []) if f.get("recommendation_type") in _DRAFT_TYPES
    ]
    candidates.sort(
        key=lambda f: (_SEVERITY_ORDER.get(f.get("severity", "low"), 3), f.get("session_id") or "")
    )
    return candidates[: max(0, max_candidates)]
