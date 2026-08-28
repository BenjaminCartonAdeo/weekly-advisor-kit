"""Candidate selection for the LLM stages (Partie 3 §2 / Partie 4 §3) — pur.

The agent never picks sessions by judgment: audit candidates come from the
Part 1 JSON signals (top cost, outliers, loop, cache gap, repeated prompts),
deduplicated and priority-ordered; draft candidates come from the Part 3
findings archive (skill-candidate / command-candidate), capped by
`max_candidates_per_run`. Everything here is deterministic and unit-testable.
"""

from __future__ import annotations

import hashlib

#: types transmis à la Partie 4 pour drafting — création OU amélioration d'une
#: commande existante (v5.30, E : pattern coûteux lancé par une commande).
_DRAFT_TYPES = {"skill-candidate", "command-candidate", "command-improvement"}
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# Mots-clés heuristiques d'exclusion (R2 anti-learning) — DROP si présents.
_ANTI_LEARNING_SECRET = ("secret", "password", "token", "api key", "credential")
_ANTI_LEARNING_REF = ("pr #", "ticket", "jira", "gh-")
_ANTI_LEARNING_TRANSIENT = (
    "transient",
    "transitory",
    "flaky",
    "intermittent",
    "sporadic",
    "temporary failure",
)
_ANTI_LEARNING_ENV = (
    "environment-specific",
    "env-specific",
    "environment specific",
    "only in staging",
    "only in dev",
    "dev-only",
    "staging-only",
)
_ANTI_LEARNING_ONEOFF = (
    "one-off",
    "one off",
    "one-time",
    "one time",
    "single occurrence",
    "rare occurrence",
)


def generate_skill_id(name: str) -> str:
    """ID déterministe d'un skill : ``skill_`` + 8 hex de sha256(nom normalisé).

    Normalisation = ``strip().lower()`` (stable, idempotent, multi-plateforme).
    """
    digest = hashlib.sha256(name.strip().lower().encode("utf-8")).hexdigest()
    return f"skill_{digest[:8]}"


def is_anti_learning(finding: dict) -> bool:
    """Retourne True si le finding décrit un pattern à NE PAS capturer (DROP).

    Patterns exclus :
      - secret (secret, password, token, api key, credential)
      - référence PR/ticket (``PR #``, ``ticket``, ``JIRA``, ``GH-``)
      - échec transitoire / flaky / intermittent
      - prohibition spécifique à l'environnement
      - récit one-off (occurence unique, anecdotique)

    Inspecte ``description``, ``evidence`` et ``recommendation_type``.
    Heuristique simple et déterministe.
    """
    text = " ".join(
        str(finding.get(k, "")) for k in ("description", "evidence", "recommendation_type")
    ).lower()

    if any(w in text for w in _ANTI_LEARNING_SECRET):
        return True
    if any(w in text for w in _ANTI_LEARNING_REF):
        return True
    if any(w in text for w in _ANTI_LEARNING_TRANSIENT):
        return True
    if any(w in text for w in _ANTI_LEARNING_ENV):
        return True
    if any(w in text for w in _ANTI_LEARNING_ONEOFF):
        return True
    return False


def _candidate_name(cand: dict) -> str:
    """Nom normalisé pour génération d'ID (fallback par pertinence)."""
    return str(
        cand.get("name")
        or cand.get("skill_name")
        or cand.get("recommendation")
        or cand.get("category")
        or cand.get("recommendation_type")
        or ""
    ).strip()


def consolidate_candidates(candidates: list[dict]) -> list[dict]:
    """Enrichit chaque candidat : create vs patch, attache skill_id/origin.

    - ``overlaps_with`` non vide OU ``skill_id`` existant → ``action='patch'``,
      ``target_skill_id`` = ``overlaps_with[0]`` (sinon l'id existant).
    - Sinon ``action='create'``.
    Toujours : ``skill_id=generate_skill_id(name)`` et ``origin='weekly-background'``.

    Fonction pure : retourne de nouveaux dicts (copie superficielle), ne mute
    pas l'entrée. Le tri/sévérité hérité des appelants est préservé.
    """
    result: list[dict] = []
    for cand in candidates:
        enriched = dict(cand)
        name = _candidate_name(enriched)
        existing_skill_id = enriched.get("skill_id")
        overlaps = enriched.get("overlaps_with") or []

        if overlaps or existing_skill_id:
            enriched["action"] = "patch"
            enriched["target_skill_id"] = overlaps[0] if overlaps else existing_skill_id
        else:
            enriched["action"] = "create"

        enriched["skill_id"] = generate_skill_id(name) if name else (existing_skill_id or "")
        enriched["origin"] = "weekly-background"
        result.append(enriched)
    return result


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
        f
        for f in findings.get("findings", [])
        if not is_anti_learning(f) and f.get("recommendation_type") in _DRAFT_TYPES  # R2: drop anti-learning
    ]
    candidates.sort(
        key=lambda f: (_SEVERITY_ORDER.get(f.get("severity", "low"), 3), f.get("session_id") or "")
    )
    selected = candidates[: max(0, max_candidates)]
    return consolidate_candidates(selected)  # R1 provenance + R3 consolidation umbrella
